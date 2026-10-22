"""In-memory event bus for unit tests and for the deterministic replay harness.

It satisfies the same `OutboxWriter` protocol as the database writer and applies the same
deduplication rule, so a module tested against this behaves identically in production.
There is no persistence and no retry: failures surface immediately, which is what a unit
test wants.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from tazama.domain.events import DomainEvent, EventType

Handler = Callable[[DomainEvent], None]


class InMemoryEventBus:
    """Collects published events and optionally delivers them synchronously."""

    def __init__(self, *, deliver: bool = False) -> None:
        self._events: list[DomainEvent] = []
        self._seen: set[str] = set()
        self._handlers: dict[EventType, list[Handler]] = {}
        self._deliver = deliver

    # ------------------------------------------------------ writer side

    def publish(self, event: DomainEvent) -> bool:
        if event.event_id in self._seen:
            return False
        self._seen.add(event.event_id)
        self._events.append(event)
        if self._deliver:
            for handler in self._handlers.get(event.event_type, []):
                handler(event)
        return True

    def publish_all(self, events: Iterable[DomainEvent]) -> int:
        return sum(1 for event in events if self.publish(event))

    # ------------------------------------------------------ reader side

    def register(self, event_type: EventType, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    @property
    def events(self) -> tuple[DomainEvent, ...]:
        return tuple(self._events)

    def of_type(self, event_type: EventType) -> tuple[DomainEvent, ...]:
        return tuple(e for e in self._events if e.event_type == event_type)

    def types(self) -> tuple[EventType, ...]:
        """Event types in publication order — handy for asserting a pipeline's shape."""
        return tuple(e.event_type for e in self._events)

    def for_aggregate(self, aggregate_id: str) -> tuple[DomainEvent, ...]:
        return tuple(e for e in self._events if e.aggregate_id == aggregate_id)

    def clear(self) -> None:
        self._events.clear()
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._events)
