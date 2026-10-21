"""Verification module: KPI recovery, alarm clearance and the complaint quiet period.

All three must pass before an incident may close, and the baseline must come from before
the incident window — a baseline measured afterwards is the degradation.

PRD section 15 edge cases named here:
  * "flapping alarms and alarm storms" — an incident that recovers and re-breaks inside the
    quiet period fails verification and is reopened.
  * "outage that suppresses complaints (silence as signal)" — silence alone does not pass;
    the KPI check still has to demonstrate recovery.
  * "UTC vs Africa/Nairobi" — every window is computed on UTC instants from an injected
    clock, and the verdict does not move when the display timezone does.
  * "clock skew" — a quiet period that has not elapsed yet is a refusal, not a pass.
  * "duplicate and out-of-order events" — verifying twice yields the same verdict and
    distinct, deterministic event payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tazama.core.clock import FixedClock
from tazama.core.errors import NotFound
from tazama.core.policy import Policies, load_policies
from tazama.domain.events import DomainEvent, EventType
from tazama.domain.state_machine import CaseState, IllegalTransition, Trigger
from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.modules.verification.models import CheckName, IncidentView
from tazama.modules.verification.service import (
    BASELINE_GUARD,
    BASELINE_SPAN,
    VerificationService,
    breached_from,
)

NOW = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)
STARTED = NOW - timedelta(hours=6)
RESOLVED = NOW - timedelta(hours=2)

INCIDENT = IncidentView(
    id="INC_V1",
    status="verifying",
    region="Nairobi",
    detected_at=STARTED + timedelta(minutes=10),
    started_at=STARTED,
    resolved_at=RESOLVED,
    affected_elements=("CELL_1",),
    breached_kpis=(str(KpiName.AVAILABILITY),),
)


# ------------------------------------------------------------------- fakes


@dataclass(slots=True)
class FakeIncidents:
    rows: dict[str, IncidentView] = field(default_factory=dict)

    def get(self, incident_id: str) -> IncidentView | None:
        return self.rows.get(incident_id)


@dataclass(slots=True)
class FakeKpis:
    """Baseline and current values keyed by `(element, kpi)`; None means "no data"."""

    baselines: dict[tuple[str, str], float | None] = field(default_factory=dict)
    currents: dict[tuple[str, str], float | None] = field(default_factory=dict)
    baseline_windows: list[TimeWindow] = field(default_factory=list)
    current_windows: list[TimeWindow] = field(default_factory=list)

    def baseline(self, *, element_id: str, kpi: str, window: TimeWindow) -> float | None:
        self.baseline_windows.append(window)
        return self.baselines.get((element_id, kpi))

    def current(self, *, element_id: str, kpi: str, window: TimeWindow) -> float | None:
        self.current_windows.append(window)
        return self.currents.get((element_id, kpi))


@dataclass(slots=True)
class FakeAlarms:
    open_ids: tuple[str, ...] = ()

    def open_alarms(self, *, element_ids: Any, at: datetime) -> tuple[str, ...]:
        _ = (element_ids, at)
        return self.open_ids


@dataclass(slots=True)
class FakeComplaints:
    count: int = 0
    seen_since: list[datetime] = field(default_factory=list)

    def count_since(self, *, element_ids: Any, since: datetime) -> int:
        _ = element_ids
        self.seen_since.append(since)
        return self.count


@dataclass(slots=True)
class RecordingPublisher:
    events: list[DomainEvent] = field(default_factory=list)

    def publish(self, event: DomainEvent) -> bool:
        self.events.append(event)
        return True

    def types(self) -> list[EventType]:
        return [e.event_type for e in self.events]

    def of(self, event_type: EventType) -> list[DomainEvent]:
        return [e for e in self.events if e.event_type is event_type]


@dataclass(slots=True)
class RecordingDecisions:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Any = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None:
        self.entries.append(
            {
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "outcome": outcome,
                "inputs": inputs,
                "evidence": tuple(evidence),
                "rule_version": rule_version,
            },
        )


@dataclass(slots=True)
class Harness:
    service: VerificationService
    incidents: FakeIncidents
    kpis: FakeKpis
    alarms: FakeAlarms
    complaints: FakeComplaints
    publisher: RecordingPublisher
    decisions: RecordingDecisions
    clock: FixedClock


@pytest.fixture
def policies() -> Policies:
    return load_policies()


def build(
    *,
    policies: Policies,
    baseline: float | None = 99.5,
    current: float | None = 99.4,
    element: str = "CELL_1",
    kpi: str = str(KpiName.AVAILABILITY),
    open_alarms: tuple[str, ...] = (),
    complaints: int = 0,
    now: datetime = NOW,
    incident: IncidentView = INCIDENT,
) -> Harness:
    incidents = FakeIncidents({incident.id: incident})
    kpis = FakeKpis(
        baselines={(element, kpi): baseline},
        currents={(element, kpi): current},
    )
    alarms = FakeAlarms(open_ids=open_alarms)
    counter = FakeComplaints(count=complaints)
    publisher = RecordingPublisher()
    decisions = RecordingDecisions()
    clock = FixedClock(now)
    service = VerificationService(
        incidents=incidents,
        kpis=kpis,
        alarms=alarms,
        complaints=counter,
        publisher=publisher,
        decisions=decisions,
        policies=policies,
        clock=clock,
    )
    return Harness(service, incidents, kpis, alarms, counter, publisher, decisions, clock)


def with_verification(policies: Policies, **kwargs: Any) -> Policies:
    return replace(policies, verification=replace(policies.verification, **kwargs))


# ============================================================== happy path


def test_all_three_checks_passing_closes_the_incident(policies):
    h = build(policies=policies)
    result = h.service.verify(INCIDENT)
    assert result.passed is True
    assert result.kpi_recovered and result.alarms_cleared and result.quiet_period_ok
    assert result.failed_checks == ()


def test_passing_publishes_verification_completed(policies):
    h = build(policies=policies)
    h.service.verify(INCIDENT)
    assert h.publisher.types() == [EventType.VERIFICATION_COMPLETED]
    payload = h.publisher.of(EventType.VERIFICATION_COMPLETED)[0].payload
    assert payload["kpi_recovered"] is True
    assert payload["quiet_period_minutes"] == policies.verification.quiet_period_minutes


def test_passing_records_a_decision_with_evidence(policies):
    h = build(policies=policies)
    h.service.verify(INCIDENT)
    entry = h.decisions.entries[0]
    assert entry["action"] == "incident_verify"
    assert entry["outcome"] == "passed"
    assert entry["rule_version"] == policies.rule_version
    assert entry["evidence"]


def test_the_result_renders_a_verification_row(policies):
    h = build(policies=policies)
    row = h.service.verify(INCIDENT).to_row()
    assert row["incident_id"] == INCIDENT.id
    assert row["passed"] is True
    assert row["kpi_recovered"] is True
    assert row["alarms_cleared"] is True
    assert row["quiet_period_ok"] is True
    assert row["complaints_after"] == 0
    assert row["detail"]["checks"]


def test_verify_accepts_an_incident_id_and_reads_it_through_the_port(policies):
    h = build(policies=policies)
    result = h.service.verify(INCIDENT.id)
    assert result.incident_id == INCIDENT.id


def test_verify_of_an_unknown_incident_is_not_found(policies):
    h = build(policies=policies)
    with pytest.raises(NotFound):
        h.service.verify("INC_absent")


# =========================================================== KPI recovery


def test_the_baseline_window_sits_before_the_incident_started(policies):
    """The whole point: a baseline read after the event would be the degraded value."""
    h = build(policies=policies)
    h.service.verify(INCIDENT)
    window = h.kpis.baseline_windows[0]
    assert window.end == INCIDENT.window_start - BASELINE_GUARD
    assert window.start == INCIDENT.window_start - BASELINE_GUARD - BASELINE_SPAN
    assert window.end < INCIDENT.window_start
    assert window.end < (INCIDENT.resolved_at or NOW)


def test_the_current_window_is_the_most_recent_two_hours(policies):
    h = build(policies=policies)
    h.service.verify(INCIDENT)
    window = h.kpis.current_windows[0]
    assert window.end == NOW
    assert window.start == NOW - timedelta(hours=2)


def test_a_kpi_still_below_the_recovery_ratio_fails(policies):
    # 90 / 99.5 = 0.90, under the 0.95 policy floor.
    h = build(policies=policies, baseline=99.5, current=90.0)
    result = h.service.verify(INCIDENT)
    assert result.passed is False
    assert CheckName.KPI_RECOVERY in result.failed_checks
    assert "below 95% of baseline" in result.reason


def test_a_kpi_exactly_at_the_ratio_passes(policies):
    tuned = with_verification(policies, kpi_recovery_ratio=0.95)
    h = build(policies=tuned, baseline=100.0, current=95.0)
    assert h.service.verify(INCIDENT).kpi_recovered is True


def test_a_kpi_just_under_the_ratio_fails(policies):
    tuned = with_verification(policies, kpi_recovery_ratio=0.95)
    h = build(policies=tuned, baseline=100.0, current=94.9)
    assert h.service.verify(INCIDENT).kpi_recovered is False


def test_polarity_is_respected_for_a_higher_is_worse_kpi(policies):
    """Drop rate recovering from 8% to 0.6% against a 0.5% baseline is a recovery."""
    incident = replace(INCIDENT, breached_kpis=(str(KpiName.DROP_RATE),))
    h = build(
        policies=policies,
        baseline=0.5,
        current=0.6,
        kpi=str(KpiName.DROP_RATE),
        incident=incident,
    )
    result = h.service.verify(incident)
    detail = result.kpi_details[0]
    assert detail.lower_is_worse is False
    assert detail.ratio == pytest.approx(0.5 / 0.6)
    assert result.kpi_recovered is False


def test_a_higher_is_worse_kpi_back_to_baseline_passes(policies):
    incident = replace(INCIDENT, breached_kpis=(str(KpiName.DROP_RATE),))
    h = build(
        policies=policies,
        baseline=0.5,
        current=0.5,
        kpi=str(KpiName.DROP_RATE),
        incident=incident,
    )
    assert h.service.verify(incident).kpi_recovered is True


def test_a_zero_baseline_on_a_higher_is_worse_kpi_requires_zero_now(policies):
    incident = replace(INCIDENT, breached_kpis=(str(KpiName.DROP_RATE),))
    worse = build(
        policies=policies,
        baseline=0.0,
        current=0.4,
        kpi=str(KpiName.DROP_RATE),
        incident=incident,
    )
    assert worse.service.verify(incident).kpi_recovered is False
    clean = build(
        policies=policies,
        baseline=0.0,
        current=0.0,
        kpi=str(KpiName.DROP_RATE),
        incident=incident,
    )
    assert clean.service.verify(incident).kpi_recovered is True


def test_missing_kpi_data_fails_closed(policies):
    h = build(policies=policies, baseline=None, current=None)
    result = h.service.verify(INCIDENT)
    assert result.kpi_recovered is False
    assert "cannot be demonstrated" in result.reason


def test_an_incident_with_no_breached_kpi_skips_the_check(policies):
    incident = replace(INCIDENT, breached_kpis=())
    h = build(policies=policies, incident=incident)
    result = h.service.verify(incident)
    check = result.check(CheckName.KPI_RECOVERY)
    assert check is not None
    assert check.applied is False
    assert check.passed is True


def test_every_affected_element_is_measured(policies):
    incident = replace(INCIDENT, affected_elements=("CELL_1", "CELL_2"))
    h = build(policies=policies, incident=incident)
    h.kpis.baselines[("CELL_2", str(KpiName.AVAILABILITY))] = 99.0
    h.kpis.currents[("CELL_2", str(KpiName.AVAILABILITY))] = 80.0
    result = h.service.verify(incident)
    assert len(result.kpi_details) == 2
    assert result.kpi_recovered is False


def test_breached_from_names_the_kpis_over_threshold():
    assert breached_from([(str(KpiName.AVAILABILITY), 95.0)]) == (str(KpiName.AVAILABILITY),)
    assert breached_from([(str(KpiName.AVAILABILITY), 99.9)]) == ()
    assert breached_from([(str(KpiName.DROP_RATE), 4.0)]) == (str(KpiName.DROP_RATE),)
    assert breached_from([("not_a_kpi", 1.0)]) == ()


# ================================================================= alarms


def test_an_open_alarm_fails_verification(policies):
    h = build(policies=policies, open_alarms=("ALM_1", "ALM_2"))
    result = h.service.verify(INCIDENT)
    assert result.passed is False
    assert CheckName.ALARMS_CLEARED in result.failed_checks
    assert result.open_alarms == ("ALM_1", "ALM_2")
    assert "still raised" in result.reason


def test_alarm_clearance_can_be_switched_off_by_policy(policies):
    relaxed = with_verification(policies, require_alarm_clear=False)
    h = build(policies=relaxed, open_alarms=("ALM_1",))
    result = h.service.verify(INCIDENT)
    check = result.check(CheckName.ALARMS_CLEARED)
    assert check is not None
    assert check.applied is False
    assert result.passed is True


def test_the_shipped_policy_requires_alarm_clearance(policies):
    assert policies.verification.require_alarm_clear is True


# ========================================================== quiet period


def test_a_new_complaint_inside_the_quiet_period_fails(policies):
    """A fix that stops the alarm but not the complaints is not a fix."""
    h = build(policies=policies, complaints=3)
    result = h.service.verify(INCIDENT)
    assert result.passed is False
    assert CheckName.QUIET_PERIOD in result.failed_checks
    assert result.complaints_after == 3
    assert "the customers did not" in result.reason


def test_the_quiet_period_is_measured_from_resolution(policies):
    h = build(policies=policies)
    h.service.verify(INCIDENT)
    assert h.complaints.seen_since == [RESOLVED]


def test_an_unresolved_incident_measures_from_the_incident_start(policies):
    incident = replace(INCIDENT, resolved_at=None)
    h = build(policies=policies, incident=incident)
    h.service.verify(incident)
    assert h.complaints.seen_since == [STARTED]


def test_a_quiet_period_that_has_not_elapsed_fails(policies):
    """PRD section 15: "clock skew" — an unexpired window proves nothing."""
    incident = replace(INCIDENT, resolved_at=NOW - timedelta(minutes=5))
    h = build(policies=policies, incident=incident)
    result = h.service.verify(incident)
    assert result.passed is False
    assert "quiet period not elapsed" in result.reason
    assert "5 of 30 minutes" in result.reason


def test_the_quiet_period_length_comes_from_policy(policies):
    strict = with_verification(policies, quiet_period_minutes=240)
    h = build(policies=strict)
    result = h.service.verify(INCIDENT)
    assert result.passed is False
    assert "of 240 minutes" in result.reason


def test_silence_alone_does_not_pass_verification(policies):
    """PRD section 15: "outage that suppresses complaints (silence as signal)".

    Nobody is complaining because nobody has service. The quiet period is happy; the KPI
    check is what refuses closure.
    """
    h = build(policies=policies, baseline=99.5, current=10.0, complaints=0)
    result = h.service.verify(INCIDENT)
    assert result.quiet_period_ok is True
    assert result.kpi_recovered is False
    assert result.passed is False


# ============================================================== reopening


def test_failure_publishes_case_reopened_with_the_reason(policies):
    h = build(policies=policies, complaints=2)
    result = h.service.verify(INCIDENT)
    assert result.reopened is True
    reopened = h.publisher.of(EventType.CASE_REOPENED)
    assert len(reopened) == 1
    assert reopened[0].payload["reason"] == result.reason
    assert reopened[0].payload["complaints_after"] == 2
    assert str(CheckName.QUIET_PERIOD) in reopened[0].payload["failed_checks"]


def test_failure_does_not_publish_verification_completed(policies):
    h = build(policies=policies, complaints=1)
    h.service.verify(INCIDENT)
    assert h.publisher.of(EventType.VERIFICATION_COMPLETED) == []


def test_reopen_can_be_switched_off_by_policy(policies):
    held = with_verification(policies, reopen_on_failure=False)
    h = build(policies=held, complaints=1)
    result = h.service.verify(INCIDENT)
    assert result.passed is False
    assert result.reopened is False
    assert h.publisher.events == []


def test_the_decision_outcome_distinguishes_reopened_from_failed(policies):
    reopening = build(policies=policies, complaints=1)
    reopening.service.verify(INCIDENT)
    assert reopening.decisions.entries[0]["outcome"] == "reopened"

    held = build(policies=with_verification(policies, reopen_on_failure=False), complaints=1)
    held.service.verify(INCIDENT)
    assert held.decisions.entries[0]["outcome"] == "failed"


def test_a_flapping_incident_that_rebreaks_inside_the_quiet_period_is_reopened(policies):
    """PRD section 15: "flapping alarms and alarm storms".

    The element recovered — KPIs are back and the alarm cleared — and then broke again
    inside the quiet period, which is exactly what flapping looks like from the outside:
    a fresh alarm and fresh complaints on an incident someone is about to close.
    """
    incident = replace(INCIDENT, cause="flapping")
    recovered = build(policies=policies, incident=incident)
    assert recovered.service.verify(incident).passed is True

    rebroken = build(
        policies=policies,
        incident=incident,
        baseline=99.5,
        current=70.0,
        open_alarms=("ALM_FLAP",),
        complaints=4,
    )
    result = rebroken.service.verify(incident)
    assert result.passed is False
    assert set(result.failed_checks) == {
        CheckName.KPI_RECOVERY,
        CheckName.ALARMS_CLEARED,
        CheckName.QUIET_PERIOD,
    }
    assert result.reopened is True
    assert rebroken.publisher.of(EventType.CASE_REOPENED)


def test_all_three_failures_are_reported_together(policies):
    h = build(policies=policies, current=50.0, open_alarms=("A",), complaints=9)
    result = h.service.verify(INCIDENT)
    assert len(result.failed_checks) == 3
    assert result.reason.count(";") == 2


# ======================================================== state machine


def test_a_pass_maps_onto_the_resolve_transition(policies):
    h = build(policies=policies)
    result = h.service.verify(INCIDENT)
    transition = h.service.transition_for(result)
    assert transition.trigger is Trigger.RESOLVE
    assert transition.target is CaseState.RESOLVED
    assert transition.emits is EventType.VERIFICATION_COMPLETED


def test_a_failure_maps_onto_the_reopen_transition(policies):
    h = build(policies=policies, complaints=1)
    result = h.service.verify(INCIDENT)
    transition = h.service.transition_for(result)
    assert transition.trigger is Trigger.REOPEN
    assert transition.target is CaseState.REOPENED
    assert transition.emits is EventType.CASE_REOPENED


def test_the_transition_comes_from_the_state_machine_and_refuses_a_bad_state(policies):
    h = build(policies=policies)
    result = h.service.verify(INCIDENT)
    with pytest.raises(IllegalTransition):
        h.service.transition_for(result, state=CaseState.RECEIVED)


# ======================================================= clock and replay


def test_the_verdict_is_driven_by_the_injected_clock(policies):
    """PRD section 15: "UTC vs Africa/Nairobi" — no ambient time, no local-time maths."""
    incident = replace(INCIDENT, resolved_at=NOW - timedelta(minutes=10))
    h = build(policies=policies, incident=incident)
    assert h.service.verify(incident).passed is False
    h.clock.advance(timedelta(minutes=25))
    assert h.service.verify(incident).passed is True


def test_every_timestamp_in_the_result_is_utc(policies):
    h = build(policies=policies)
    result = h.service.verify(INCIDENT)
    assert result.checked_at.tzinfo is not None
    assert result.checked_at.utcoffset() == timedelta(0)


def test_verifying_twice_yields_the_same_verdict(policies):
    """PRD section 15: "duplicate and out-of-order events"."""
    h = build(policies=policies)
    first = h.service.verify(INCIDENT)
    second = h.service.verify(INCIDENT)
    assert first.passed == second.passed
    assert first.checked_at == second.checked_at
    events = h.publisher.of(EventType.VERIFICATION_COMPLETED)
    assert len({e.event_id for e in events}) == 1


def test_the_incident_view_prefers_started_at_over_detected_at():
    view = IncidentView(
        id="X",
        status="verifying",
        region="Nairobi",
        detected_at=NOW,
        started_at=NOW - timedelta(hours=1),
    )
    assert view.window_start == NOW - timedelta(hours=1)
    assert (
        IncidentView(id="Y", status="verifying", region="Nairobi", detected_at=NOW).window_start
        == NOW
    )
