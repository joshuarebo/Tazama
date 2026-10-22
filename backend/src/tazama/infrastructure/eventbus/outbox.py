"""Writing events into the outbox, inside the caller's transaction."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from tazama.core.clock import Clock
from tazama.core.ids import get_correlation_id
from tazama.core.logging import get_logger
from tazama.domain.events import DomainEvent
from tazama.infrastructure.db.models import OutboxEvent

_log = get_logger(__name__)

#: After this many failed dispatch attempts an event is dead-lettered for a human.
#: Low on purpose: a consumer that fails five times is broken, and retrying forever just
#: hides it while the backlog grows (see docs/runbooks/outbox-backlog.md).
MAX_ATTEMPTS = 5


class OutboxWriter(Protocol):
    """What modules depend on. They publish; they do not know about tables."""

    def publish(self, event: DomainEvent) -> bool: ...

    def publish_all(self, events: Iterable[DomainEvent]) -> int: ...


class SessionOutboxWriter:
    """Appends events to `outbox_event` using the session the caller is already in.

    Taking the session (rather than an engine) is the whole point: the events commit with
    the state change or not at all.
    """

    __slots__ = ("_clock", "_session")

    def __init__(self, session: Session, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    def publish(self, event: DomainEvent) -> bool:
        """Append one event. Returns False when it was already published.

        Deduplication is by `event_id`, which is derived from the fact itself, so
        re-running a step after a crash does not duplicate its events.
        """
        record = event.to_record()
        record["correlation_id"] = record.get("correlation_id") or get_correlation_id() or None
        existing = self._session.execute(
            select(OutboxEvent.id).where(OutboxEvent.id == record["id"]),
        ).scalar_one_or_none()
        if existing is not None:
            return False

        now = self._clock.now()
        self._session.execute(
            insert(OutboxEvent).values(
                id=record["id"],
                event_type=record["event_type"],
                event_version=record["event_version"],
                aggregate_type=record["aggregate_type"],
                aggregate_id=record["aggregate_id"],
                payload=record["payload"],
                occurred_at=record["occurred_at"],
                available_at=now,
                status="pending",
                attempts=0,
                correlation_id=record["correlation_id"],
                dedupe_key=record["id"],
            ),
        )
        _log.debug(
            "event_published",
            extra={
                "event_type": record["event_type"],
                "aggregate_id": record["aggregate_id"],
                "event_id": record["id"],
            },
        )
        return True

    def publish_all(self, events: Iterable[DomainEvent]) -> int:
        """Append several events; returns how many were new."""
        return sum(1 for event in events if self.publish(event))


def pending_events(session: Session, *, limit: int = 100) -> Sequence[OutboxEvent]:
    """The next batch to dispatch, oldest first.

    Ordering is by `available_at` then `occurred_at`, so a retried event does not jump the
    queue and ordering stays stable per aggregate.
    """
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status.in_(("pending", "retry")))
        .order_by(OutboxEvent.available_at, OutboxEvent.occurred_at, OutboxEvent.id)
        .limit(limit)
    )
    return session.execute(stmt).scalars().all()
