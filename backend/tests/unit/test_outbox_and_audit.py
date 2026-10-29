"""Outbox, dispatcher and audit.

The outbox is what makes the pipeline honest: the state change and its events commit
together, delivery is at-least-once, failures back off and then dead-letter, and the audit
log is append-only. Each of those claims is tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tazama.domain.events import (
    AggregateType,
    ComplaintReceived,
    DomainEvent,
    EventType,
    IncidentDetected,
)
from tazama.infrastructure.db.audit_repo import SqlAuditSink
from tazama.infrastructure.db.models import AuditLog, OutboxEvent
from tazama.infrastructure.eventbus import (
    MAX_ATTEMPTS,
    InMemoryEventBus,
    OutboxDispatcher,
    SessionOutboxWriter,
)
from tazama.infrastructure.eventbus.dispatcher import _backoff
from tazama.modules.audit.models import Evidence
from tazama.modules.audit.service import AuditService

T0 = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)


def _event(aggregate_id: str = "CMP-1", dedupe_key: str = "dk-1") -> ComplaintReceived:
    return ComplaintReceived(
        aggregate_type=AggregateType.COMPLAINT,
        aggregate_id=aggregate_id,
        occurred_at=T0,
        dedupe_key=dedupe_key,
        correlation_id="CID1",
        payload={"channel": "sms"},
    )


# ------------------------------------------------------------ in-memory bus


def test_memory_bus_dedupes_on_event_id() -> None:
    bus = InMemoryEventBus()
    assert bus.publish(_event()) is True
    assert bus.publish(_event()) is False
    assert len(bus) == 1


def test_memory_bus_records_order_and_filters() -> None:
    bus = InMemoryEventBus()
    bus.publish(_event())
    bus.publish(
        IncidentDetected(
            aggregate_type=AggregateType.INCIDENT,
            aggregate_id="INC-1",
            occurred_at=T0,
            dedupe_key="inc-1",
        ),
    )
    assert bus.types() == (EventType.COMPLAINT_RECEIVED, EventType.INCIDENT_DETECTED)
    assert len(bus.of_type(EventType.INCIDENT_DETECTED)) == 1
    assert bus.for_aggregate("INC-1")[0].aggregate_id == "INC-1"


def test_memory_bus_can_deliver_synchronously() -> None:
    seen: list[DomainEvent] = []
    bus = InMemoryEventBus(deliver=True)
    bus.register(EventType.COMPLAINT_RECEIVED, seen.append)
    bus.publish(_event())
    assert len(seen) == 1


# ---------------------------------------------------------------- outbox


def test_publish_writes_one_row(db_session, frozen_clock) -> None:
    writer = SessionOutboxWriter(db_session, frozen_clock)
    assert writer.publish(_event()) is True
    db_session.commit()
    row = db_session.execute(select(OutboxEvent)).scalar_one()
    assert row.event_type == "ComplaintReceived"
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.payload == {"channel": "sms"}
    assert row.correlation_id == "CID1"


def test_publishing_the_same_fact_twice_is_a_no_op(db_session, frozen_clock) -> None:
    writer = SessionOutboxWriter(db_session, frozen_clock)
    assert writer.publish(_event()) is True
    assert writer.publish(_event()) is False
    db_session.commit()
    assert db_session.execute(select(func.count()).select_from(OutboxEvent)).scalar_one() == 1


def test_events_roll_back_with_the_state_change(db_engine, frozen_clock) -> None:
    """The whole point of an outbox: no events without the change that caused them."""
    from tazama.infrastructure.db import session_factory

    session = session_factory(db_engine)()
    try:
        SessionOutboxWriter(session, frozen_clock).publish(_event())
        session.rollback()
    finally:
        session.close()

    with session_factory(db_engine)() as check:
        assert check.execute(select(func.count()).select_from(OutboxEvent)).scalar_one() == 0


def test_publish_all_counts_new_events(db_session, frozen_clock) -> None:
    writer = SessionOutboxWriter(db_session, frozen_clock)
    events = [_event("CMP-1", "dk-1"), _event("CMP-2", "dk-2"), _event("CMP-1", "dk-1")]
    assert writer.publish_all(events) == 2


# ------------------------------------------------------------ dispatcher


@pytest.fixture
def dispatcher(db_engine, frozen_clock):
    return OutboxDispatcher(engine=db_engine, clock=frozen_clock)


def _seed_event(db_engine, clock, event: DomainEvent) -> None:
    from tazama.infrastructure.db import session_factory

    with session_factory(db_engine)() as session, session.begin():
        SessionOutboxWriter(session, clock).publish(event)


def test_dispatch_delivers_to_a_handler(db_engine, frozen_clock, dispatcher) -> None:
    seen: list[DomainEvent] = []
    dispatcher.register(EventType.COMPLAINT_RECEIVED, seen.append)
    _seed_event(db_engine, frozen_clock, _event())

    report = dispatcher.poll_once()
    assert (report.claimed, report.delivered, report.failed) == (1, 1, 0)
    assert seen[0].aggregate_id == "CMP-1"
    assert seen[0].payload == {"channel": "sms"}


def test_delivered_events_are_not_redelivered(db_engine, frozen_clock, dispatcher) -> None:
    dispatcher.register(EventType.COMPLAINT_RECEIVED, lambda _event: None)
    _seed_event(db_engine, frozen_clock, _event())
    assert dispatcher.poll_once().delivered == 1
    assert dispatcher.poll_once().idle is True


def test_event_without_a_handler_is_not_a_failure(db_engine, frozen_clock, dispatcher) -> None:
    _seed_event(db_engine, frozen_clock, _event())
    report = dispatcher.poll_once()
    assert report.skipped_no_handler == 1
    assert report.failed == 0


def test_failing_handler_is_retried_with_backoff(db_engine, frozen_clock, dispatcher) -> None:
    def explode(_event: DomainEvent) -> None:
        raise RuntimeError("consumer is broken")

    dispatcher.register(EventType.COMPLAINT_RECEIVED, explode)
    _seed_event(db_engine, frozen_clock, _event())

    report = dispatcher.poll_once()
    assert (report.failed, report.dead_lettered) == (1, 0)

    from tazama.infrastructure.db import session_factory

    with session_factory(db_engine)() as session:
        row = session.execute(select(OutboxEvent)).scalar_one()
    assert row.status == "retry"
    assert row.attempts == 1
    assert "consumer is broken" in (row.last_error or "")
    # Backoff moved it into the future, so the next poll does not pick it up.
    assert row.available_at > frozen_clock.now()
    assert dispatcher.poll_once().claimed == 0


def test_retries_stop_at_the_dead_letter(db_engine, frozen_clock, dispatcher) -> None:
    dispatcher.register(
        EventType.COMPLAINT_RECEIVED,
        lambda _event: (_ for _ in ()).throw(RuntimeError("still broken")),
    )
    _seed_event(db_engine, frozen_clock, _event())

    for _ in range(MAX_ATTEMPTS):
        dispatcher.poll_once()
        frozen_clock.advance(timedelta(hours=1))  # skip past the backoff

    letters = dispatcher.dead_letters()
    assert len(letters) == 1
    assert letters[0].attempts == MAX_ATTEMPTS
    assert "still broken" in letters[0].last_error
    # A dead letter is not retried any more.
    assert dispatcher.poll_once().claimed == 0


def test_dead_letters_can_be_requeued_after_a_fix(db_engine, frozen_clock, dispatcher) -> None:
    calls: list[int] = []

    def flaky(_event: DomainEvent) -> None:
        calls.append(1)
        if len(calls) <= MAX_ATTEMPTS:
            raise RuntimeError("broken")

    dispatcher.register(EventType.COMPLAINT_RECEIVED, flaky)
    _seed_event(db_engine, frozen_clock, _event())
    for _ in range(MAX_ATTEMPTS):
        dispatcher.poll_once()
        frozen_clock.advance(timedelta(hours=1))
    assert dispatcher.dead_letters()

    assert dispatcher.requeue_dead_letters() == 1
    assert dispatcher.poll_once().delivered == 1
    assert dispatcher.dead_letters() == ()


def test_one_poisonous_event_does_not_block_the_batch(db_engine, frozen_clock, dispatcher) -> None:
    delivered: list[str] = []

    def selective(event: DomainEvent) -> None:
        if event.aggregate_id == "CMP-BAD":
            raise RuntimeError("poison")
        delivered.append(event.aggregate_id)

    dispatcher.register(EventType.COMPLAINT_RECEIVED, selective)
    _seed_event(db_engine, frozen_clock, _event("CMP-BAD", "dk-bad"))
    _seed_event(db_engine, frozen_clock, _event("CMP-OK", "dk-ok"))

    report = dispatcher.poll_once()
    assert report.claimed == 2
    assert delivered == ["CMP-OK"]
    assert report.failed == 1


def test_drain_is_bounded(db_engine, frozen_clock, dispatcher) -> None:
    """A handler that publishes another event must not spin forever."""
    from tazama.infrastructure.db import session_factory

    counter = {"n": 0}

    def multiply(event: DomainEvent) -> None:
        counter["n"] += 1
        with session_factory(db_engine)() as session, session.begin():
            SessionOutboxWriter(session, frozen_clock).publish(
                _event(f"CMP-{counter['n']}", f"dk-{counter['n']}"),
            )

    dispatcher.register(EventType.COMPLAINT_RECEIVED, multiply)
    _seed_event(db_engine, frozen_clock, _event("CMP-0", "dk-0"))
    report = dispatcher.drain(max_polls=5)
    assert report.delivered <= 6  # bounded, not infinite


def test_handlers_run_in_registration_order(db_engine, frozen_clock, dispatcher) -> None:
    order: list[str] = []
    dispatcher.register(EventType.COMPLAINT_RECEIVED, lambda _e: order.append("first"))
    dispatcher.register(EventType.COMPLAINT_RECEIVED, lambda _e: order.append("second"))
    _seed_event(db_engine, frozen_clock, _event())
    dispatcher.poll_once()
    assert order == ["first", "second"]


def test_backoff_grows_and_is_jittered_deterministically() -> None:
    first = _backoff(1, "EVT_AAA")
    second = _backoff(2, "EVT_AAA")
    assert second > first
    assert _backoff(1, "EVT_AAA") == first  # same event, same delay: replayable
    assert _backoff(1, "EVT_BBB") != first  # different events spread out
    assert _backoff(99, "EVT_AAA") == _backoff(len(("x",) * 5), "EVT_AAA")  # clamped


async def test_outbox_lag_reports_pending_work(db_engine, frozen_clock, settings) -> None:
    from tazama.infrastructure.eventbus import outbox_lag

    lag, pending = await outbox_lag(settings)
    assert (lag, pending) == (0.0, 0)

    _seed_event(db_engine, frozen_clock, _event())
    lag, pending = await outbox_lag(settings)
    assert pending == 1
    assert lag > 0  # the event is dated in the past relative to the real clock


# ------------------------------------------------------------------ audit


@pytest.fixture
def audit(db_engine, frozen_clock):
    return AuditService(sink=SqlAuditSink(db_engine), clock=frozen_clock)


def test_decision_is_recorded_with_its_versions(audit, db_session) -> None:
    audit.record(
        action="correlate",
        entity_type="incident",
        entity_id="INC-1",
        outcome="created",
        inputs={"complaints": 7, "radius_km": 3.0},
        evidence=[Evidence(kind="alarm", ref="ALM-1", note="cell unavailable")],
        confidence=0.82,
        rule_version="policies-v1",
    )
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert row.action == "correlate"
    assert row.outcome == "created"
    assert row.inputs == {"complaints": 7, "radius_km": 3.0}
    assert row.evidence[0]["ref"] == "ALM-1"
    assert row.confidence == 0.82
    assert row.rule_version == "policies-v1"
    assert row.actor == "system"


def test_audit_scrubs_identifiers_and_secrets(audit, db_session) -> None:
    audit.record(
        action="notify",
        entity_type="notification",
        entity_id="NTF-1",
        outcome="proposed",
        inputs={
            "msisdn": "254712345678",
            "api_key": "sk-live-abcdef123456",  # pragma: allowlist secret
        },
    )
    stored = db_session.execute(select(AuditLog)).scalar_one().inputs
    assert "712345678" not in str(stored)
    assert stored["api_key"] == "***"


def test_audit_input_count_is_bounded(audit, db_session) -> None:
    audit.record(
        action="x",
        entity_type="incident",
        entity_id="INC-1",
        outcome="ok",
        inputs={f"k{i}": i for i in range(200)},
    )
    assert len(db_session.execute(select(AuditLog)).scalar_one().inputs) <= 40


def test_human_action_records_the_person(audit, db_session) -> None:
    from tazama.core.security import Principal

    audit.record_human_action(
        principal=Principal(subject="amina", role="manager"),
        action="approve_notification",
        entity_type="notification",
        entity_id="NTF-1",
        outcome="approved",
        client_ip="10.0.0.9",
    )
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert (row.actor, row.actor_role) == ("amina", "manager")
    assert row.client_ip == "10.0.0.9"


def test_history_is_newest_first(audit, frozen_clock) -> None:
    for index in range(3):
        audit.record(
            action=f"step-{index}",
            entity_type="incident",
            entity_id="INC-1",
            outcome="ok",
            inputs={},
        )
        frozen_clock.advance(timedelta(minutes=1))
    history = audit.history("incident", "INC-1")
    assert [d.action for d in history] == ["step-2", "step-1", "step-0"]


def test_history_is_scoped_to_the_entity(audit) -> None:
    audit.record(action="a", entity_type="incident", entity_id="INC-1", outcome="ok", inputs={})
    audit.record(action="b", entity_type="incident", entity_id="INC-2", outcome="ok", inputs={})
    assert len(audit.history("incident", "INC-1")) == 1


def test_recent_can_filter_by_action_and_actor(audit) -> None:
    audit.record(action="route", entity_type="case", entity_id="C1", outcome="ok", inputs={})
    audit.record(
        action="approve",
        entity_type="case",
        entity_id="C1",
        outcome="ok",
        inputs={},
        actor="amina",
    )
    assert len(audit.recent(action="route")) == 1
    assert len(audit.recent(actor="amina")) == 1
    assert len(audit.recent()) == 2


def test_audit_can_share_the_callers_transaction(db_engine, db_session, frozen_clock) -> None:
    """An audit row written with the state change must roll back with it."""
    from tazama.infrastructure.db import session_factory

    session = session_factory(db_engine)()
    try:
        AuditService(sink=SqlAuditSink(session=session), clock=frozen_clock).record(
            action="correlate",
            entity_type="incident",
            entity_id="INC-9",
            outcome="created",
            inputs={},
        )
        session.rollback()
    finally:
        session.close()
    assert db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 0


def test_evidence_is_a_reference_not_a_copy() -> None:
    evidence = Evidence(kind="kpi", ref="kpi_cell:CELL-1:2026-03-17T10", note="availability 0")
    assert evidence.to_dict() == {
        "kind": "kpi",
        "ref": "kpi_cell:CELL-1:2026-03-17T10",
        "note": "availability 0",
        "weight": 1.0,
    }


def test_audit_sink_exposes_no_mutation_methods() -> None:
    """Append-only is a property of the interface, not just of our habits."""
    from tazama.modules.audit.ports import AuditSink

    methods = {name for name in dir(AuditSink) if not name.startswith("_")}
    assert methods == {"append", "for_entity", "recent"}
    for forbidden in ("update", "delete", "purge", "edit"):
        assert forbidden not in methods
