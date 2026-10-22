"""Feeding the SSE stream from the outbox.

The first version of this had the worker push directly into the API's in-process stream.
That was wrong twice over: it made `worker` import `api` (the wrong direction), and it only
works when both run in the same process — in the real deployment they are separate
containers, so a browser would have seen nothing.

Instead the **API tails the outbox**: a background task polls for events published since it
last looked and fans thin notices out to connected browsers. Every API replica tails
independently, so the browsers connected to each one all get updates, and nothing in the
worker knows the UI exists.

The tail is read-only and best-effort: if it falls behind or fails, incidents are still
detected, stored and visible on refresh — only the live update is lost.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine

from tazama.api.stream import EventStream, StreamEvent, get_stream, incident_event
from tazama.core.logging import get_logger
from tazama.core.settings import Settings
from tazama.domain.events import EventType
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import OutboxEvent

_log = get_logger(__name__)

#: How often to look for newly published events. Fast enough to feel live, slow enough to
#: be invisible in the database's load.
POLL_INTERVAL_S = 1.5

#: Never replay more than this on startup: a browser that reconnects wants *now*, not the
#: last hour of history.
STARTUP_LOOKBACK = timedelta(minutes=2)

BATCH_SIZE = 200

#: Event type -> SSE event name. Anything absent is internal and not streamed.
STREAMED_EVENTS: dict[str, str] = {
    EventType.INCIDENT_DETECTED: "incident.detected",
    EventType.INCIDENT_UPDATED: "incident.updated",
    EventType.IMPACT_CALCULATED: "incident.impact",
    EventType.DIAGNOSIS_COMPLETED: "incident.diagnosis",
    EventType.CASE_PRIORITIZED: "case.prioritised",
    EventType.CASE_ROUTED: "case.routed",
    EventType.CASE_CLOSED: "case.closed",
    EventType.CASE_REOPENED: "case.reopened",
    EventType.NOTIFICATION_PROPOSED: "notification.proposed",
    EventType.NOTIFICATION_APPROVED: "notification.approved",
    EventType.NOTIFICATION_SENT: "notification.sent",
    EventType.NOTIFICATION_SUPPRESSED: "notification.suppressed",
    EventType.RECOVERY_RECOMMENDED: "recovery.recommended",
    EventType.RECOVERY_APPROVED: "recovery.approved",
    EventType.VERIFICATION_COMPLETED: "incident.verified",
    EventType.DEGRADATION_DETECTED: "degradation.detected",
    EventType.HUMAN_REVIEW_REQUESTED: "review.requested",
}


@dataclass(slots=True)
class TailState:
    """Where the tail has read up to. `last_id` breaks ties within the same instant."""

    since: datetime
    last_id: str = ""
    published: int = 0
    polls: int = 0


def _to_stream_event(row: OutboxEvent) -> StreamEvent | None:
    name = STREAMED_EVENTS.get(row.event_type)
    if name is None:
        return None
    payload = row.payload or {}
    return incident_event(
        name,
        str(payload.get("incident_id") or row.aggregate_id),
        status=_opt(payload.get("status")),
        priority=_opt(payload.get("priority")),
        region=_opt(payload.get("region")),
    )


def _opt(value: object) -> str | None:
    return None if value is None else str(value)


def read_new_events(engine: Engine, state: TailState) -> list[OutboxEvent]:
    """Rows published since the last read, oldest first.

    Keyed on `published_at` rather than `occurred_at`: a late or replayed event has an old
    event time but is news to the browser now.
    """
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == "published")
        .where(OutboxEvent.published_at.is_not(None))
        .where(OutboxEvent.published_at >= state.since)
        .order_by(OutboxEvent.published_at, OutboxEvent.id)
        .limit(BATCH_SIZE)
    )
    with session_factory(engine)() as session:
        rows = list(session.execute(stmt).scalars().all())

    # Drop anything already sent at the boundary instant.
    if state.last_id:
        rows = [row for row in rows if (row.published_at, row.id) > (state.since, state.last_id)]
    return rows


async def tail_once(engine: Engine, state: TailState, stream: EventStream) -> int:
    """One poll. Returns how many notices were published."""
    state.polls += 1
    try:
        rows = await asyncio.to_thread(read_new_events, engine, state)
    except Exception as exc:
        # The UI is not the pipeline: a failed tail is logged and retried, never raised.
        _log.warning("stream_tail_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return 0

    sent = 0
    for row in rows:
        event = _to_stream_event(row)
        if event is not None:
            await stream.publish(event)
            sent += 1
        if row.published_at is not None:
            state.since = row.published_at
            state.last_id = row.id
    state.published += sent
    return sent


async def run_tail(
    settings: Settings,
    *,
    stream: EventStream | None = None,
    interval_s: float = POLL_INTERVAL_S,
    max_polls: int | None = None,
) -> TailState:
    """Poll until cancelled. Started by the API lifespan; bounded in tests."""
    engine = get_engine(settings)
    target = stream or get_stream()
    state = TailState(since=datetime.now(tz=UTC) - STARTUP_LOOKBACK)
    _log.info("stream_tail_start", extra={"interval_s": interval_s})
    try:
        while max_polls is None or state.polls < max_polls:
            await tail_once(engine, state, target)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        _log.info(
            "stream_tail_stop",
            extra={"polls": state.polls, "published": state.published},
        )
        raise
    return state
