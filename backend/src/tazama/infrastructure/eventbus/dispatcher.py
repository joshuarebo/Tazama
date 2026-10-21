"""Polling dispatcher: drains the outbox into registered handlers.

Delivery is at-least-once. A handler that raises gets the event back after a backoff, and
after `MAX_ATTEMPTS` the event is dead-lettered rather than retried forever — a backlog
that grows quietly is worse than a loud failure.

Each event is claimed and marked in its own transaction, so one poisonous event cannot
block the batch behind it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine

from tazama.core.clock import Clock, SystemClock
from tazama.core.ids import set_correlation_id
from tazama.core.logging import get_logger
from tazama.core.settings import Settings
from tazama.domain.events import DomainEvent, EventType
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import OutboxEvent
from tazama.infrastructure.eventbus.outbox import MAX_ATTEMPTS, pending_events

_log = get_logger(__name__)

Handler = Callable[[DomainEvent], None]

#: Backoff per attempt, in seconds. Jittered on use so retries do not synchronise into a
#: thundering herd against whatever already failed.
BACKOFF_SECONDS: tuple[int, ...] = (2, 10, 60, 300, 900)


@dataclass(slots=True)
class DispatchReport:
    """What one poll did. Returned so the worker can log and the tests can assert."""

    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead_lettered: int = 0
    skipped_no_handler: int = 0
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return self.claimed == 0

    def render(self) -> str:
        return (
            f"claimed={self.claimed} delivered={self.delivered} failed={self.failed} "
            f"dead_lettered={self.dead_lettered} no_handler={self.skipped_no_handler} "
            f"in {self.duration_ms:.0f}ms"
        )


@dataclass(frozen=True, slots=True)
class _Claim:
    """One claimed outbox row, read out of the session before handlers run.

    The rows are copied into plain values so no handler runs while a session is open —
    a handler opens its own transaction, and nesting them is how deadlocks start.
    """

    id: str
    event_type: str
    event_version: int
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    occurred_at: datetime
    dedupe_key: str
    correlation_id: str | None
    attempts: int

    def to_record(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "event_version": self.event_version,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "dedupe_key": self.dedupe_key,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class DeadLetter:
    event_id: str
    event_type: str
    aggregate_id: str
    attempts: int
    last_error: str


class OutboxDispatcher:
    """Registers handlers per event type and drains the outbox."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        clock: Clock | None = None,
        batch_size: int = 100,
    ) -> None:
        self._engine = engine or get_engine()
        self._clock = clock or SystemClock()
        self._batch_size = batch_size
        self._handlers: dict[EventType, list[Handler]] = {}

    # ------------------------------------------------------------ wiring

    def register(self, event_type: EventType, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def register_many(self, handlers: dict[EventType, Sequence[Handler]]) -> None:
        for event_type, group in handlers.items():
            for handler in group:
                self.register(event_type, handler)

    @property
    def registered_types(self) -> frozenset[EventType]:
        return frozenset(self._handlers)

    # ---------------------------------------------------------- draining

    def poll_once(self) -> DispatchReport:
        """Claim a batch and deliver it. Safe to call from one worker at a time."""
        started = time.perf_counter()
        report = DispatchReport()
        factory = session_factory(self._engine)

        with factory() as session:
            now = self._clock.now()
            batch = [
                event
                for event in pending_events(session, limit=self._batch_size)
                if event.available_at <= now
            ]
            report.claimed = len(batch)
            claims = [
                _Claim(
                    id=row.id,
                    event_type=row.event_type,
                    event_version=row.event_version,
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    payload=dict(row.payload or {}),
                    occurred_at=row.occurred_at,
                    dedupe_key=row.dedupe_key or row.id,
                    correlation_id=row.correlation_id,
                    attempts=row.attempts,
                )
                for row in batch
            ]

        for claim in claims:
            self._deliver(claim, report)

        report.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if report.claimed:
            _log.info("outbox_poll", extra={"report": report.render()})
        return report

    def _deliver(self, claim: _Claim, report: DispatchReport) -> None:
        event_id = claim.id
        set_correlation_id(claim.correlation_id or event_id[:16])
        try:
            event_type = EventType(claim.event_type)
        except ValueError:
            # An unknown type means a newer producer wrote it: leave it for that consumer
            # rather than dead-lettering a perfectly good event.
            report.skipped_no_handler += 1
            self._mark(
                event_id,
                status="pending",
                attempts=claim.attempts,
                error="unknown event type",
            )
            return

        handlers = self._handlers.get(event_type)
        if not handlers:
            report.skipped_no_handler += 1
            self._mark(event_id, status="published", attempts=claim.attempts)
            return

        event = DomainEvent.from_record(claim.to_record())
        try:
            for handler in handlers:
                handler(event)
        except Exception as exc:
            attempts = claim.attempts + 1
            message = f"{type(exc).__name__}: {exc}"[:200]
            report.failed += 1
            report.errors.append(f"{event_type}: {message}")
            if attempts >= MAX_ATTEMPTS:
                report.dead_lettered += 1
                self._mark(event_id, status="dead_letter", attempts=attempts, error=message)
                _log.error(
                    "event_dead_lettered",
                    extra={"event_id": event_id, "event_type": str(event_type), "error": message},
                )
            else:
                self._mark(
                    event_id,
                    status="retry",
                    attempts=attempts,
                    error=message,
                    delay_s=_backoff(attempts, event_id),
                )
                _log.warning(
                    "event_retry",
                    extra={"event_id": event_id, "attempts": attempts, "error": message},
                )
            return

        report.delivered += 1
        self._mark(event_id, status="published", attempts=claim.attempts)

    def _mark(
        self,
        event_id: str,
        *,
        status: str,
        attempts: int,
        error: str | None = None,
        delay_s: float = 0.0,
    ) -> None:
        now = self._clock.now()
        values: dict[str, object] = {"status": status, "attempts": attempts, "last_error": error}
        if status == "published":
            values["published_at"] = now
        if delay_s:
            values["available_at"] = now + timedelta(seconds=delay_s)
        with session_factory(self._engine)() as session, session.begin():
            session.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(**values))

    # ------------------------------------------------------- inspection

    def drain(self, *, max_polls: int = 50) -> DispatchReport:
        """Keep polling until the outbox is quiet. Used by tests, replay and `make demo`.

        Bounded on purpose: handlers publish further events, so an unbounded drain could
        spin forever on a cycle.
        """
        total = DispatchReport()
        for _ in range(max_polls):
            report = self.poll_once()
            total.claimed += report.claimed
            total.delivered += report.delivered
            total.failed += report.failed
            total.dead_lettered += report.dead_lettered
            total.skipped_no_handler += report.skipped_no_handler
            total.errors.extend(report.errors)
            if report.idle:
                break
        return total

    def dead_letters(self, *, limit: int = 50) -> tuple[DeadLetter, ...]:
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.status == "dead_letter")
            .order_by(OutboxEvent.occurred_at.desc())
            .limit(limit)
        )
        with session_factory(self._engine)() as session:
            rows = session.execute(stmt).scalars().all()
        return tuple(
            DeadLetter(
                event_id=row.id,
                event_type=row.event_type,
                aggregate_id=row.aggregate_id,
                attempts=row.attempts,
                last_error=row.last_error or "",
            )
            for row in rows
        )

    def requeue_dead_letters(self) -> int:
        """Operator action after fixing a consumer (see the outbox-backlog runbook)."""
        with session_factory(self._engine)() as session, session.begin():
            result = session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.status == "dead_letter")
                .values(
                    status="pending", attempts=0, last_error=None, available_at=self._clock.now()
                ),
            )
        # `rowcount` lives on the cursor result; the ORM result type does not declare it.
        return int(getattr(result, "rowcount", 0) or 0)


def _backoff(attempts: int, event_id: str) -> float:
    """Exponential-ish backoff with deterministic jitter.

    The jitter is derived from the event id rather than a random source, so a replay of the
    same failure sequence behaves identically — and retries still spread out.
    """
    base = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
    jitter = (sum(event_id.encode("utf-8")) % 1000) / 1000.0
    return base * (0.75 + 0.5 * jitter)


async def outbox_lag(settings: Settings | None = None) -> tuple[float, int]:
    """`(oldest pending age in seconds, pending count)` for `/readyz` and the doctor."""
    return await asyncio.to_thread(_outbox_lag_sync, settings)


def _outbox_lag_sync(settings: Settings | None = None) -> tuple[float, int]:
    engine = get_engine(settings)
    with session_factory(engine)() as session:
        row = session.execute(
            select(func.count(), func.min(OutboxEvent.occurred_at)).where(
                OutboxEvent.status.in_(("pending", "retry")),
            ),
        ).one()
    pending = int(row[0] or 0)
    if not pending or row[1] is None:
        return 0.0, 0
    oldest = row[1]
    now = SystemClock().now()
    if oldest.tzinfo is None:
        from datetime import UTC

        oldest = oldest.replace(tzinfo=UTC)
    return max(0.0, (now - oldest).total_seconds()), pending
