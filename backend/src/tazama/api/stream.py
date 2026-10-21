"""Server-sent events: live incident updates for the Command Center (PRD section 8).

Why SSE rather than WebSockets: the traffic is one-directional (the server tells the
browser that something changed), SSE survives proxies and reconnects on its own, and it
needs no extra protocol handling on either side.

What travels over the wire is deliberately thin — an id, a type and a few fields — so the
browser re-fetches through the normal, authorised API instead of receiving data it may not
be allowed to see. An SSE frame is not a place to enforce authorisation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tazama.core.logging import get_logger

_log = get_logger(__name__)

#: Per-subscriber queue depth. A slow browser is dropped rather than allowed to grow
#: memory without bound (back-pressure beats a memory leak).
QUEUE_SIZE = 100

#: Heartbeat interval. Keeps proxies from closing an idle connection and lets the browser
#: notice a dead server.
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One SSE frame."""

    event: str
    data: dict[str, Any]
    id: str | None = None

    def render(self) -> str:
        lines = []
        if self.id:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event}")
        lines.append(f"data: {json.dumps(self.data, separators=(',', ':'), default=str)}")
        return "\n".join(lines) + "\n\n"


@dataclass(eq=False)
class Subscriber:
    """One connected browser. `eq=False` keeps identity semantics, so two subscribers with
    equally-empty queues are still two entries in the set."""

    queue: asyncio.Queue[StreamEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE),
    )
    dropped: int = 0


class EventStream:
    """In-process fan-out of stream events to connected browsers.

    Scope note: this broadcasts within one API process. With several API replicas, each
    serves the browsers connected to it, so a cross-process relay (Redis pub/sub, or the
    outbox tailed by every replica) is needed before horizontal scaling. Recorded in
    `docs/tech-debt.md` rather than pretended away.
    """

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = asyncio.Lock()
        self._sequence = 0

    async def publish(self, event: StreamEvent) -> int:
        """Fan out to every subscriber. Returns how many received it."""
        async with self._lock:
            self._sequence += 1
            numbered = StreamEvent(
                event=event.event,
                data=event.data | {"seq": self._sequence},
                id=event.id or str(self._sequence),
            )
            targets = list(self._subscribers)

        delivered = 0
        for subscriber in targets:
            try:
                subscriber.queue.put_nowait(numbered)
                delivered += 1
            except asyncio.QueueFull:
                # Drop for this subscriber only: the browser will re-fetch on reconnect.
                subscriber.dropped += 1
                _log.warning("sse_subscriber_lagging", extra={"dropped": subscriber.dropped})
        return delivered

    def publish_soon(self, event: StreamEvent) -> None:
        """Publish from synchronous code (a worker consumer, for instance).

        Never raises: a UI update must not be able to fail a pipeline step.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("sse_publish_without_loop", extra={"event": event.event})
            return
        loop.create_task(self.publish(event))  # noqa: RUF006 - fire and forget by design

    async def subscribe(self) -> AsyncIterator[StreamEvent]:
        """Yield events for one connection until the client goes away."""
        subscriber = Subscriber()
        async with self._lock:
            self._subscribers.add(subscriber)
        _log.info("sse_connected", extra={"subscribers": len(self._subscribers)})
        try:
            yield StreamEvent(
                event="hello",
                data={"at": datetime.now(tz=UTC).isoformat(), "heartbeat_s": HEARTBEAT_SECONDS},
            )
            while True:
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield StreamEvent(
                        event="heartbeat", data={"at": datetime.now(tz=UTC).isoformat()}
                    )
                    continue
                yield event
        finally:
            async with self._lock:
                self._subscribers.discard(subscriber)
            _log.info("sse_disconnected", extra={"subscribers": len(self._subscribers)})

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


#: One stream per API process, injected through `api/deps.py`.
_STREAM = EventStream()


def get_stream() -> EventStream:
    return _STREAM


def incident_event(
    kind: str,
    incident_id: str,
    *,
    status: str | None = None,
    priority: str | None = None,
    region: str | None = None,
) -> StreamEvent:
    """A thin incident notice: enough to update a list, not enough to leak anything."""
    return StreamEvent(
        event=kind,
        data={
            "incident_id": incident_id,
            "status": status,
            "priority": priority,
            "region": region,
            "at": datetime.now(tz=UTC).isoformat(),
        },
        id=f"{kind}:{incident_id}",
    )
