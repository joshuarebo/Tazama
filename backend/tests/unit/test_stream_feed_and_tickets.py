"""The outbox tail that feeds SSE, and the single-use tickets that authenticate it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from tazama.api.stream import (
    MAX_OUTSTANDING_TICKETS,
    TICKET_TTL_SECONDS,
    EventStream,
    TicketStore,
)
from tazama.api.stream_feed import (
    STREAMED_EVENTS,
    TailState,
    read_new_events,
    run_tail,
    tail_once,
)
from tazama.domain.events import AggregateType, EventType, IncidentDetected, NotificationProposed
from tazama.infrastructure.db import session_factory
from tazama.infrastructure.db.models import OutboxEvent
from tazama.infrastructure.eventbus import SessionOutboxWriter

T0 = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)


def _publish_and_mark(db_engine, clock, event, *, published_at=None) -> None:
    """Write an event and mark it published, which is what the tail looks for."""
    with session_factory(db_engine)() as session, session.begin():
        SessionOutboxWriter(session, clock).publish(event)
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event.event_id)
            .values(status="published", published_at=published_at or clock.now()),
        )


def _incident(incident_id: str = "INC-1", **payload: object) -> IncidentDetected:
    return IncidentDetected(
        aggregate_type=AggregateType.INCIDENT,
        aggregate_id=incident_id,
        occurred_at=T0,
        dedupe_key=f"{incident_id}:detected",
        payload={"region": "Nairobi", "priority": "P1", "status": "detected", **payload},
    )


# ------------------------------------------------------------------- tail


def test_streamed_events_are_a_deliberate_subset() -> None:
    """Internal steps must not leak to browsers just because they exist."""
    streamed = set(STREAMED_EVENTS)
    assert EventType.INCIDENT_DETECTED in streamed
    assert EventType.COMPLAINT_RECEIVED not in streamed
    assert EventType.SITE_IDENTIFIED not in streamed
    assert streamed <= set(EventType)


def test_tail_reads_only_published_events(db_engine, frozen_clock) -> None:
    # Pending: written but not dispatched, so not news yet.
    with session_factory(db_engine)() as session, session.begin():
        SessionOutboxWriter(session, frozen_clock).publish(_incident("INC-PENDING"))
    _publish_and_mark(db_engine, frozen_clock, _incident("INC-DONE"))

    state = TailState(since=T0 - timedelta(minutes=5))
    rows = read_new_events(db_engine, state)
    assert [row.aggregate_id for row in rows] == ["INC-DONE"]


async def test_tail_publishes_thin_notices(db_engine, frozen_clock) -> None:
    _publish_and_mark(db_engine, frozen_clock, _incident())
    stream = EventStream()
    iterator = stream.subscribe()
    await anext(iterator)  # hello

    state = TailState(since=T0 - timedelta(minutes=5))
    assert await tail_once(db_engine, state, stream) == 1

    frame = await anext(iterator)
    assert frame.event == "incident.detected"
    assert frame.data["incident_id"] == "INC-1"
    assert frame.data["priority"] == "P1"
    # Nothing about subscribers travels over the wire.
    assert set(frame.data) == {"incident_id", "status", "priority", "region", "at", "seq"}
    await iterator.aclose()


async def test_tail_does_not_resend_the_same_event(db_engine, frozen_clock) -> None:
    _publish_and_mark(db_engine, frozen_clock, _incident())
    stream = EventStream()
    state = TailState(since=T0 - timedelta(minutes=5))

    assert await tail_once(db_engine, state, stream) == 1
    assert await tail_once(db_engine, state, stream) == 0


async def test_tail_picks_up_later_events(db_engine, frozen_clock) -> None:
    stream = EventStream()
    state = TailState(since=T0 - timedelta(minutes=5))
    _publish_and_mark(db_engine, frozen_clock, _incident("INC-1"))
    assert await tail_once(db_engine, state, stream) == 1

    frozen_clock.advance(timedelta(seconds=30))
    _publish_and_mark(db_engine, frozen_clock, _incident("INC-2"))
    assert await tail_once(db_engine, state, stream) == 1


async def test_tail_skips_events_browsers_should_not_see(db_engine, frozen_clock) -> None:
    from tazama.domain.events import ComplaintReceived

    _publish_and_mark(
        db_engine,
        frozen_clock,
        ComplaintReceived(
            aggregate_type=AggregateType.COMPLAINT,
            aggregate_id="CMP-1",
            occurred_at=T0,
            dedupe_key="dk-1",
        ),
    )
    state = TailState(since=T0 - timedelta(minutes=5))
    assert await tail_once(db_engine, state, EventStream()) == 0
    # The row was still consumed, so it is not re-read forever.
    assert state.last_id


async def test_tail_uses_the_incident_id_from_the_payload(db_engine, frozen_clock) -> None:
    _publish_and_mark(
        db_engine,
        frozen_clock,
        NotificationProposed(
            aggregate_type=AggregateType.NOTIFICATION,
            aggregate_id="NTF-1",
            occurred_at=T0,
            dedupe_key="ntf-1",
            payload={"incident_id": "INC-9"},
        ),
    )
    stream = EventStream()
    iterator = stream.subscribe()
    await anext(iterator)
    await tail_once(db_engine, TailState(since=T0 - timedelta(minutes=5)), stream)
    frame = await anext(iterator)
    assert frame.event == "notification.proposed"
    assert frame.data["incident_id"] == "INC-9"
    await iterator.aclose()


async def test_tail_survives_a_database_failure(db_engine, frozen_clock, monkeypatch) -> None:
    """A failing tail must not take the API down; the UI just stops being live."""
    import tazama.api.stream_feed as feed

    def explode(*_args: object, **_kwargs: object) -> list[OutboxEvent]:
        raise RuntimeError("database went away")

    monkeypatch.setattr(feed, "read_new_events", explode)
    state = TailState(since=T0)
    assert await tail_once(db_engine, state, EventStream()) == 0
    assert state.polls == 1


async def test_run_tail_is_bounded_for_tests(settings, db_engine) -> None:
    state = await run_tail(settings, stream=EventStream(), interval_s=0.0, max_polls=3)
    assert state.polls == 3


# ---------------------------------------------------------------- tickets


def test_ticket_is_random_and_long_enough() -> None:
    store = TicketStore()
    first = store.issue(subject="amina", role="analyst")
    second = store.issue(subject="amina", role="analyst")
    assert first.value != second.value
    assert len(first.value) >= 32


def test_ticket_is_single_use() -> None:
    """A URL ends up in logs and history, so redeeming it twice must fail."""
    store = TicketStore()
    ticket = store.issue(subject="amina", role="analyst")
    assert store.consume(ticket.value) is not None
    assert store.consume(ticket.value) is None


def test_ticket_expires() -> None:
    store = TicketStore(ttl_seconds=30)
    ticket = store.issue(subject="amina", role="analyst", now=1000.0)
    assert store.consume(ticket.value, now=1029.0) is not None

    other = store.issue(subject="amina", role="analyst", now=1000.0)
    assert store.consume(other.value, now=1031.0) is None


def test_ticket_carries_the_principal() -> None:
    store = TicketStore()
    ticket = store.issue(subject="amina", role="manager")
    redeemed = store.consume(ticket.value)
    assert redeemed is not None
    assert (redeemed.subject, redeemed.role) == ("amina", "manager")


@pytest.mark.parametrize("value", ["", "nope", "x" * 64])
def test_unknown_tickets_are_refused(value: str) -> None:
    assert TicketStore().consume(value) is None


def test_expired_tickets_are_evicted() -> None:
    store = TicketStore(ttl_seconds=10)
    for _ in range(5):
        store.issue(subject="a", role="analyst", now=100.0)
    assert store.outstanding == 5
    store.issue(subject="b", role="analyst", now=200.0)
    # The five stale tickets went when the sixth was issued.
    assert store.outstanding == 1


def test_outstanding_tickets_are_bounded() -> None:
    """A flood of ticket requests must not grow memory without bound."""
    store = TicketStore(ttl_seconds=10_000)
    for index in range(MAX_OUTSTANDING_TICKETS + 50):
        store.issue(subject=f"user-{index}", role="analyst", now=100.0)
    assert store.outstanding <= MAX_OUTSTANDING_TICKETS


def test_ttl_is_short() -> None:
    assert TICKET_TTL_SECONDS <= 60
