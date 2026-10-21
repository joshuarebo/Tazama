"""Domain events and the case lifecycle.

These two files are the backbone of the pipeline, so the properties that make replay and
schema evolution safe are asserted here rather than assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tazama.domain.events import (
    EVENT_CLASSES,
    AggregateType,
    ComplaintReceived,
    DegradationDetected,
    DomainEvent,
    EventType,
    IncidentDetected,
)
from tazama.domain.state_machine import (
    TERMINAL_STATES,
    TRANSITION_TABLE,
    CaseState,
    IllegalTransition,
    Trigger,
    advance,
    allowed_triggers,
    can,
    is_open,
    is_terminal,
    reachable_states,
)

T0 = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)


def _complaint_event(dedupe_key: str = "dk-1", **payload: object) -> ComplaintReceived:
    return ComplaintReceived(
        aggregate_type=AggregateType.COMPLAINT,
        aggregate_id="CMP-1",
        occurred_at=T0,
        dedupe_key=dedupe_key,
        correlation_id="CID1",
        payload=dict(payload),
    )


# ------------------------------------------------------------------ events


def test_event_id_is_deterministic() -> None:
    assert _complaint_event().event_id == _complaint_event().event_id


def test_event_id_changes_with_the_fact_not_the_payload() -> None:
    """Two reports of the same fact must collide even if the payload grew a field."""
    base = _complaint_event()
    enriched = _complaint_event(language="sw")
    assert base.event_id == enriched.event_id
    assert _complaint_event(dedupe_key="dk-2").event_id != base.event_id


def test_event_id_is_namespaced_by_type() -> None:
    complaint = _complaint_event()
    incident = IncidentDetected(
        aggregate_type=AggregateType.COMPLAINT,
        aggregate_id="CMP-1",
        occurred_at=T0,
        dedupe_key="dk-1",
    )
    assert complaint.event_id != incident.event_id


def test_record_round_trip_preserves_identity() -> None:
    original = _complaint_event(cell_id="CELL-1")
    rebuilt = DomainEvent.from_record(original.to_record())
    assert type(rebuilt) is ComplaintReceived
    assert rebuilt.event_id == original.event_id
    assert rebuilt.payload == {"cell_id": "CELL-1"}
    assert rebuilt.correlation_id == "CID1"
    assert rebuilt.occurred_at == T0


def test_record_shape_matches_the_outbox_columns() -> None:
    record = _complaint_event().to_record()
    assert set(record) == {
        "id",
        "event_type",
        "event_version",
        "aggregate_type",
        "aggregate_id",
        "payload",
        "occurred_at",
        "dedupe_key",
        "correlation_id",
    }


def test_every_event_type_can_be_rebuilt() -> None:
    """An event that cannot be reconstructed cannot be replayed."""
    assert set(EVENT_CLASSES) == set(EventType)


def test_every_event_class_declares_its_own_type() -> None:
    for event_type, klass in EVENT_CLASSES.items():
        assert klass.event_type is event_type, klass


def test_with_payload_is_additive_and_does_not_mutate() -> None:
    original = _complaint_event(a=1)
    extended = original.with_payload(b=2)
    assert original.payload == {"a": 1}
    assert extended.payload == {"a": 1, "b": 2}
    assert extended.event_id == original.event_id  # same fact


def test_events_are_immutable() -> None:
    event = _complaint_event()
    with pytest.raises((AttributeError, TypeError)):
        event.aggregate_id = "other"  # type: ignore[misc]


def test_network_first_entry_point_exists() -> None:
    event = DegradationDetected(
        aggregate_type=AggregateType.CELL,
        aggregate_id="CELL-1",
        occurred_at=T0,
        dedupe_key="CELL-1:availability:2026-03-17T10",
        payload={"kpi": "availability_pct", "value": 0.0, "threshold": 99.0},
    )
    assert event.event_type is EventType.DEGRADATION_DETECTED
    assert DomainEvent.from_record(event.to_record()).payload["value"] == 0.0


def test_unknown_type_rebuilds_as_the_base_event() -> None:
    """A newer producer's event must not crash an older consumer."""
    record = _complaint_event().to_record()
    record["event_type"] = EventType.CASE_CLOSED
    assert isinstance(DomainEvent.from_record(record), DomainEvent)


# ----------------------------------------------------------- state machine


def test_happy_path_walks_from_received_to_closed() -> None:
    state = CaseState.RECEIVED
    for trigger in (
        Trigger.VALIDATE,
        Trigger.ENRICH,
        Trigger.LOCATE,
        Trigger.CORRELATE,
        Trigger.DIAGNOSE,
        Trigger.PRIORITISE,
        Trigger.ROUTE,
        Trigger.PROPOSE_NOTIFICATION,
        Trigger.RECOMMEND_RECOVERY,
        Trigger.VERIFY,
        Trigger.RESOLVE,
        Trigger.CLOSE,
    ):
        state = advance(state, trigger).target
    assert state is CaseState.CLOSED


def test_network_first_case_starts_at_correlated() -> None:
    state = advance(CaseState.CORRELATED, Trigger.PRIORITISE).target
    assert state is CaseState.PRIORITISED


@pytest.mark.parametrize(
    ("state", "trigger"),
    [
        (CaseState.RECEIVED, Trigger.CLOSE),
        (CaseState.RECEIVED, Trigger.VERIFY),
        (CaseState.CLOSED, Trigger.CLOSE),
        (CaseState.SUPPRESSED, Trigger.CORRELATE),
        (CaseState.SUPPRESSED, Trigger.REOPEN),
        (CaseState.VALIDATED, Trigger.ROUTE),
        (CaseState.RESOLVED, Trigger.VERIFY),
    ],
)
def test_illegal_transitions_are_refused(state: CaseState, trigger: Trigger) -> None:
    assert not can(state, trigger)
    with pytest.raises(IllegalTransition) as excinfo:
        advance(state, trigger)
    # The error names both sides and what *is* allowed, so a developer can act on it.
    message = str(excinfo.value)
    assert str(trigger) in message
    assert str(state) in message


def test_exhaustive_state_times_trigger_matrix() -> None:
    """Every (state, trigger) pair either transitions or raises — never silently passes."""
    for state in CaseState:
        for trigger in Trigger:
            if can(state, trigger):
                transition = advance(state, trigger)
                assert transition.source is state
                assert isinstance(transition.target, CaseState)
            else:
                with pytest.raises(IllegalTransition):
                    advance(state, trigger)


def test_verification_failure_reopens() -> None:
    assert advance(CaseState.VERIFYING, Trigger.REOPEN).target is CaseState.REOPENED


def test_a_closed_case_can_come_back() -> None:
    """Recurrence is normal in a network; closure is not a wall."""
    assert advance(CaseState.CLOSED, Trigger.REOPEN).target is CaseState.REOPENED


def test_reopened_case_re_enters_the_pipeline() -> None:
    assert advance(CaseState.REOPENED, Trigger.CORRELATE).target is CaseState.CORRELATED


def test_human_review_is_reachable_and_returnable() -> None:
    for state in (
        CaseState.LOCATED,
        CaseState.CORRELATED,
        CaseState.DIAGNOSING,
        CaseState.VERIFYING,
    ):
        assert advance(state, Trigger.ESCALATE_TO_HUMAN).target is CaseState.HUMAN_REVIEW
    assert advance(CaseState.HUMAN_REVIEW, Trigger.RESUME_FROM_HUMAN).target is CaseState.CORRELATED
    assert advance(CaseState.HUMAN_REVIEW, Trigger.SUPPRESS).target is CaseState.SUPPRESSED


def test_suppression_is_terminal() -> None:
    assert is_terminal(CaseState.SUPPRESSED)
    assert allowed_triggers(CaseState.SUPPRESSED) == frozenset()
    assert frozenset({CaseState.SUPPRESSED}) == TERMINAL_STATES


def test_closed_is_not_terminal() -> None:
    assert not is_terminal(CaseState.CLOSED)


@pytest.mark.parametrize(
    ("state", "open_"),
    [
        (CaseState.RECEIVED, True),
        (CaseState.VERIFYING, True),
        (CaseState.HUMAN_REVIEW, True),
        (CaseState.REOPENED, True),
        (CaseState.RESOLVED, False),
        (CaseState.CLOSED, False),
        (CaseState.SUPPRESSED, False),
    ],
)
def test_open_cases_are_the_ones_needing_attention(state: CaseState, open_: bool) -> None:
    assert is_open(state) is open_


def test_every_state_is_reachable_from_an_entry_point() -> None:
    assert reachable_states() == frozenset(CaseState)


def test_transitions_declare_the_event_they_emit() -> None:
    """A state change with no event would leave the outbox out of step with the database."""
    emitting = {
        (CaseState.RECEIVED, Trigger.VALIDATE),
        (CaseState.VALIDATED, Trigger.ENRICH),
        (CaseState.ENRICHED, Trigger.LOCATE),
        (CaseState.LOCATED, Trigger.CORRELATE),
        (CaseState.PRIORITISED, Trigger.ROUTE),
        (CaseState.RESOLVED, Trigger.CLOSE),
    }
    for key in emitting:
        assert TRANSITION_TABLE[key].emits is not None, key


def test_no_duplicate_transitions_are_declared() -> None:
    """`_build_table` raises on a duplicate; this proves the table was actually built."""
    assert len(TRANSITION_TABLE) >= len(CaseState)
