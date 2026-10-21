"""Transactional outbox and event dispatch (ADR 0003).

Why an outbox rather than a broker: the state change and the events it causes must commit
together, or a crash between them leaves the system lying about itself. Here the events are
rows in the same database, written in the caller's transaction, and a poller dispatches
them afterwards. That gives at-least-once delivery, replay, and an audit trail of what the
system believed, with no extra infrastructure.

Consumers must therefore be **idempotent**: an event may arrive more than once, and
`DomainEvent.event_id` is deterministic so a consumer can recognise it.
"""

from tazama.infrastructure.eventbus.dispatcher import (
    DeadLetter,
    DispatchReport,
    OutboxDispatcher,
    outbox_lag,
)
from tazama.infrastructure.eventbus.memory import InMemoryEventBus
from tazama.infrastructure.eventbus.outbox import (
    MAX_ATTEMPTS,
    OutboxWriter,
    SessionOutboxWriter,
)

__all__ = [
    "MAX_ATTEMPTS",
    "DeadLetter",
    "DispatchReport",
    "InMemoryEventBus",
    "OutboxDispatcher",
    "OutboxWriter",
    "SessionOutboxWriter",
    "outbox_lag",
]
