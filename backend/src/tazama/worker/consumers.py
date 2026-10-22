"""Event consumers: what the worker does when each event arrives.

Wiring lives here and nowhere else, so the pipeline's shape is readable in one file. Each
consumer is **idempotent** — the outbox delivers at least once, and a replay delivers again
on purpose.

A handler that raises gets the event redelivered after a backoff and eventually
dead-lettered, so a handler should raise on a transient failure and swallow nothing it
cannot explain.

Note what is *not* here: pushing updates to the browser. The worker is a separate process
from the API, so it cannot reach the API's in-process SSE stream; the API tails the outbox
itself (`api/stream_feed.py`). That also keeps `worker` from importing `api`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy.engine import Engine

from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.core.settings import Settings
from tazama.domain.events import DomainEvent, EventType
from tazama.infrastructure.db.audit_repo import SqlAuditSink
from tazama.modules.audit.service import AuditService

_log = get_logger(__name__)

Handler = Callable[[DomainEvent], None]

#: Events worth an audit row even when no module decided anything — the fact that the
#: pipeline reached this step is itself the record.
AUDITED_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.INCIDENT_DETECTED,
        EventType.CASE_CLOSED,
        EventType.CASE_REOPENED,
        EventType.NOTIFICATION_SENT,
        EventType.NOTIFICATION_SUPPRESSED,
        EventType.RECOVERY_APPROVED,
        EventType.VERIFICATION_COMPLETED,
        EventType.HUMAN_REVIEW_REQUESTED,
    },
)


def event_auditor(audit: AuditService) -> Handler:
    """Record that the pipeline reached a milestone, with the event's own payload."""

    def handle(event: DomainEvent) -> None:
        if event.event_type not in AUDITED_EVENTS:
            return
        audit.record(
            action=f"event:{event.event_type}",
            entity_type=str(event.aggregate_type),
            entity_id=event.aggregate_id,
            outcome="observed",
            inputs=dict(event.payload or {}),
        )

    return handle


def build_consumers(
    *,
    settings: Settings,
    policies: Policies,
    clock: Clock,
    engine: Engine,
) -> dict[EventType, Sequence[Handler]]:
    """Wire every event type to its handlers.

    Pipeline consumers (correlation, impact, notification, verification) attach here as
    their modules land; the milestone auditor applies in every phase.
    """
    audit = AuditService(sink=SqlAuditSink(engine), clock=clock)
    record = event_auditor(audit)

    consumers: dict[EventType, list[Handler]] = {}
    for event_type in AUDITED_EVENTS:
        consumers.setdefault(event_type, []).append(record)

    _log.info(
        "consumers_wired",
        extra={
            "event_types": len(consumers),
            "app_mode": settings.APP_MODE,
            "rule_version": policies.rule_version,
        },
    )
    return {event_type: tuple(handlers) for event_type, handlers in consumers.items()}
