"""Verification: closure has to be earned (PRD section 8, "Verification").

Three checks, all required:

  1. **KPI recovery** — every breached KPI is back within `kpi_recovery_ratio` of a baseline
     taken from a window *before* the incident started. Taking the baseline after the event
     would measure the degradation and call it normal.
  2. **Alarms cleared** — nothing still raised on the affected elements, when
     `require_alarm_clear` is on.
  3. **Quiet period** — no new complaints on those elements for `quiet_period_minutes`.
     An alarm that clears while customers keep calling is not a fix, and this is the check
     that catches a flapping incident which recovered and re-broke inside the window.

Pass publishes `VerificationCompleted` and reports the state-machine transition the caller
should apply (`VERIFYING -> RESOLVE`). Failure with `reopen_on_failure` publishes
`CaseReopened` and returns the reason, so the caller applies `VERIFYING -> REOPEN`. The
transitions come from `domain.state_machine.advance()`, never from a status written by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from tazama.core.clock import Clock
from tazama.core.errors import NotFound
from tazama.core.ids import get_correlation_id
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.domain.events import (
    AggregateType,
    CaseReopened,
    DomainEvent,
    VerificationCompleted,
)
from tazama.domain.state_machine import CaseState, Transition, Trigger, advance
from tazama.domain.value_objects import KPI_POLARITY, KpiName, TimeWindow
from tazama.modules.verification.models import (
    CheckName,
    CheckResult,
    IncidentView,
    KpiRecovery,
    VerificationResult,
)
from tazama.modules.verification.ports import (
    AlarmReader,
    ComplaintCounter,
    DecisionLog,
    EventPublisher,
    IncidentReader,
    KpiReader,
)

_log = get_logger(__name__)

ENTITY_TYPE = "incident"

#: How far back the pre-incident baseline window reaches. Long enough to average out one
#: busy hour, short enough that last week's network is not treated as today's normal.
BASELINE_SPAN = timedelta(hours=24)

#: Gap left between the baseline window and the incident start, so the ramp into the
#: degradation does not contaminate the baseline it is being compared against.
BASELINE_GUARD = timedelta(minutes=30)

#: Window used to read the current value. Two hourly KPI buckets.
CURRENT_SPAN = timedelta(hours=2)


class VerificationService:
    def __init__(
        self,
        *,
        incidents: IncidentReader,
        kpis: KpiReader,
        alarms: AlarmReader,
        complaints: ComplaintCounter,
        publisher: EventPublisher,
        decisions: DecisionLog,
        policies: Policies,
        clock: Clock,
    ) -> None:
        self._incidents = incidents
        self._kpis = kpis
        self._alarms = alarms
        self._complaints = complaints
        self._publisher = publisher
        self._decisions = decisions
        self._policies = policies
        self._clock = clock

    # -------------------------------------------------------------- verify

    def verify(self, incident: IncidentView | str) -> VerificationResult:
        """Run all three checks and publish the verdict.

        Accepts an `IncidentView` (the caller already has it) or an incident id, which is
        read through the `IncidentReader` port.
        """
        view = self._resolve(incident)
        policy = self._policies.verification
        now = self._clock.now()

        kpi_check, kpi_details = self._check_kpis(view, now=now)
        alarm_check, open_alarms = self._check_alarms(view, now=now)
        quiet_check, complaints_after = self._check_quiet_period(view, now=now)

        checks = (kpi_check, alarm_check, quiet_check)
        passed = all(check.passed for check in checks)
        reason = "" if passed else "; ".join(check.detail for check in checks if not check.passed)
        reopened = bool(not passed and policy.reopen_on_failure)

        result = VerificationResult(
            incident_id=view.id,
            checked_at=now,
            passed=passed,
            checks=checks,
            kpi_details=kpi_details,
            complaints_after=complaints_after,
            open_alarms=open_alarms,
            reason=reason,
            reopened=reopened,
        )

        if passed:
            self._publish(
                VerificationCompleted(
                    aggregate_type=AggregateType.INCIDENT,
                    aggregate_id=view.id,
                    occurred_at=now,
                    dedupe_key=f"{view.id}:verified:{now.isoformat()}",
                    correlation_id=get_correlation_id(),
                    payload={
                        "kpi_recovered": True,
                        "alarms_cleared": True,
                        "quiet_period_ok": True,
                        "quiet_period_minutes": policy.quiet_period_minutes,
                        "kpi_recovery_ratio": policy.kpi_recovery_ratio,
                        "checks": [c.to_dict() for c in checks],
                    },
                ),
            )
        elif reopened:
            self._publish(
                CaseReopened(
                    aggregate_type=AggregateType.INCIDENT,
                    aggregate_id=view.id,
                    occurred_at=now,
                    dedupe_key=f"{view.id}:reopened:{now.isoformat()}",
                    correlation_id=get_correlation_id(),
                    payload={
                        "reason": reason,
                        "failed_checks": [str(name) for name in result.failed_checks],
                        "complaints_after": complaints_after,
                        "open_alarms": list(open_alarms),
                    },
                ),
            )
        else:
            # Policy says do not reopen: the case stays in verification for a human.
            _log.info(
                "verification failed and reopen_on_failure is off",
                extra={"incident_id": view.id, "reason": reason},
            )

        self._decisions.record(
            action="incident_verify",
            entity_type=ENTITY_TYPE,
            entity_id=view.id,
            outcome="passed" if passed else ("reopened" if reopened else "failed"),
            inputs={
                "quiet_period_minutes": policy.quiet_period_minutes,
                "kpi_recovery_ratio": policy.kpi_recovery_ratio,
                "require_alarm_clear": policy.require_alarm_clear,
                "reopen_on_failure": policy.reopen_on_failure,
                "breached_kpis": list(view.breached_kpis),
                "affected_elements": list(view.affected_elements),
                "complaints_after": complaints_after,
                "open_alarms": list(open_alarms),
                "checks": [c.to_dict() for c in checks],
            },
            evidence=tuple(k.to_dict() for k in kpi_details),
            rule_version=self._policies.rule_version,
        )
        return result

    def transition_for(
        self, result: VerificationResult, *, state: CaseState = CaseState.VERIFYING
    ) -> Transition:
        """The lifecycle transition this verdict implies.

        The service does not write a status: it returns the transition from the one place
        transitions are declared, and the caller commits it with the events in one
        transaction. An illegal state raises rather than silently doing nothing.
        """
        trigger = Trigger.RESOLVE if result.passed else Trigger.REOPEN
        return advance(state, trigger)

    # -------------------------------------------------------------- checks

    def _check_kpis(
        self, view: IncidentView, *, now: datetime
    ) -> tuple[CheckResult, tuple[KpiRecovery, ...]]:
        ratio_floor = self._policies.verification.kpi_recovery_ratio
        if not view.breached_kpis or not view.affected_elements:
            return (
                CheckResult(
                    name=CheckName.KPI_RECOVERY,
                    passed=True,
                    detail="no breached KPI recorded on the incident",
                    applied=False,
                ),
                (),
            )

        start = view.window_start
        baseline_window = TimeWindow(
            start=start - BASELINE_GUARD - BASELINE_SPAN,
            end=start - BASELINE_GUARD,
        )
        current_window = TimeWindow(start=now - CURRENT_SPAN, end=now)

        details: list[KpiRecovery] = []
        unmeasured: list[str] = []
        for element_id in view.affected_elements:
            for kpi in view.breached_kpis:
                baseline = self._kpis.baseline(
                    element_id=element_id, kpi=kpi, window=baseline_window
                )
                current = self._kpis.current(element_id=element_id, kpi=kpi, window=current_window)
                if baseline is None or current is None:
                    unmeasured.append(f"{element_id}/{kpi}")
                    continue
                details.append(
                    _recovery(
                        element_id=element_id,
                        kpi=kpi,
                        baseline=baseline,
                        current=current,
                        ratio_floor=ratio_floor,
                    ),
                )

        if unmeasured and not details:
            # Fail closed: no measurement is not the same as recovery.
            return (
                CheckResult(
                    name=CheckName.KPI_RECOVERY,
                    passed=False,
                    detail=(
                        f"no KPI data to compare for {', '.join(sorted(unmeasured))}; "
                        f"recovery cannot be demonstrated"
                    ),
                ),
                (),
            )

        failing = [d for d in details if not d.recovered]
        if failing:
            worst = min(failing, key=lambda d: d.ratio)
            detail = (
                f"{len(failing)} of {len(details)} KPI measurements still below "
                f"{ratio_floor:.0%} of baseline (worst: {worst.element_id}/{worst.kpi} at "
                f"{worst.ratio:.0%})"
            )
        else:
            detail = (
                f"all {len(details)} KPI measurements at or above {ratio_floor:.0%} of the "
                f"pre-incident baseline"
            )
            if unmeasured:
                detail += f"; no data for {', '.join(sorted(unmeasured))}"
        return (
            CheckResult(name=CheckName.KPI_RECOVERY, passed=not failing, detail=detail),
            tuple(details),
        )

    def _check_alarms(
        self, view: IncidentView, *, now: datetime
    ) -> tuple[CheckResult, tuple[str, ...]]:
        if not self._policies.verification.require_alarm_clear:
            return (
                CheckResult(
                    name=CheckName.ALARMS_CLEARED,
                    passed=True,
                    detail="alarm clearance not required by policy",
                    applied=False,
                ),
                (),
            )
        open_alarms = self._alarms.open_alarms(element_ids=view.affected_elements, at=now)
        if open_alarms:
            return (
                CheckResult(
                    name=CheckName.ALARMS_CLEARED,
                    passed=False,
                    detail=(
                        f"{len(open_alarms)} alarm(s) still raised on the affected "
                        f"elements: {', '.join(open_alarms[:5])}"
                    ),
                ),
                open_alarms,
            )
        return (
            CheckResult(
                name=CheckName.ALARMS_CLEARED,
                passed=True,
                detail="no alarms raised on the affected elements",
            ),
            (),
        )

    def _check_quiet_period(self, view: IncidentView, *, now: datetime) -> tuple[CheckResult, int]:
        minutes = self._policies.verification.quiet_period_minutes
        span = timedelta(minutes=minutes)
        since = view.resolved_at or view.window_start
        complaints = self._complaints.count_since(element_ids=view.affected_elements, since=since)
        elapsed = now - since

        if elapsed < span:
            # Not yet earned. This is a refusal, not a failure of the fix — but it is still
            # a "no", because a quiet period that has not elapsed proves nothing.
            return (
                CheckResult(
                    name=CheckName.QUIET_PERIOD,
                    passed=False,
                    detail=(
                        f"quiet period not elapsed: {int(elapsed.total_seconds() // 60)} of "
                        f"{minutes} minutes since {since.isoformat()}"
                    ),
                ),
                complaints,
            )
        if complaints:
            return (
                CheckResult(
                    name=CheckName.QUIET_PERIOD,
                    passed=False,
                    detail=(
                        f"{complaints} new complaint(s) on the affected elements since "
                        f"{since.isoformat()}: the alarm stopped but the customers did not"
                    ),
                ),
                complaints,
            )
        return (
            CheckResult(
                name=CheckName.QUIET_PERIOD,
                passed=True,
                detail=f"no new complaints for {minutes} minutes",
            ),
            0,
        )

    # ------------------------------------------------------------- helpers

    def _resolve(self, incident: IncidentView | str) -> IncidentView:
        if isinstance(incident, IncidentView):
            return incident
        view = self._incidents.get(incident)
        if view is None:
            raise NotFound(f"Unknown incident {incident}.")
        return view

    def _publish(self, event: DomainEvent) -> None:
        self._publisher.publish(event)


def _recovery(
    *, element_id: str, kpi: str, baseline: float, current: float, ratio_floor: float
) -> KpiRecovery:
    """Express recovery as a single 0..1+ ratio, whichever way the KPI points.

    For a KPI where lower is worse (availability, throughput) recovery is
    `current / baseline`. For one where higher is worse (drop rate, latency) it is
    `baseline / current`. Both then compare against one policy threshold.
    """
    lower_is_worse = _lower_is_worse(kpi)
    if lower_is_worse:
        ratio = 1.0 if baseline == 0 else current / baseline
    else:
        # A baseline of zero on a "higher is worse" KPI (no drops before the incident) means
        # any current value above zero is a regression; equal zeros are a full recovery.
        if current == 0:
            ratio = 1.0
        elif baseline == 0:
            ratio = 0.0
        else:
            ratio = baseline / current
    return KpiRecovery(
        element_id=element_id,
        kpi=kpi,
        baseline=baseline,
        current=current,
        ratio=ratio,
        recovered=ratio >= ratio_floor,
        lower_is_worse=lower_is_worse,
    )


def _lower_is_worse(kpi: str) -> bool:
    """Polarity from the domain's own table; an unknown KPI is treated as higher-is-better."""
    try:
        name = KpiName(kpi)
    except ValueError:
        return True
    return KPI_POLARITY[name][0]


def breached_from(samples: Sequence[tuple[str, float]]) -> tuple[str, ...]:
    """Helper for callers assembling an `IncidentView`: which samples breach a threshold."""
    breached: list[str] = []
    for kpi, value in samples:
        try:
            name = KpiName(kpi)
        except ValueError:
            continue
        lower_is_worse, threshold = KPI_POLARITY[name]
        if (value < threshold) if lower_is_worse else (value > threshold):
            breached.append(str(name))
    return tuple(dict.fromkeys(breached))
