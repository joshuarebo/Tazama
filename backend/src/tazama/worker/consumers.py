"""Event consumers: what the worker does when each event arrives.

Wiring lives here and nowhere else, so the pipeline's shape is readable in one file. Each
consumer is **idempotent** — the outbox delivers at least once, and a replay delivers again
on purpose.

Handlers are registered per event type. A handler that raises gets the event redelivered
after a backoff and eventually dead-lettered, so a handler should raise on a transient
failure and swallow nothing it cannot explain.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.engine import Engine

from tazama.api.stream import EventStream, get_stream, incident_event
from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.core.settings import Settings
from tazama.domain.events import DomainEvent, EventType
from tazama.infrastructure.db.audit_repo import SqlAuditSink
from tazama.modules.audit.service import AuditService

_log = get_logger(__name__)

Handler = Callable[[DomainEvent], None]

#: Events that should refresh the Command Center, and the SSE event name each becomes.
STREAMED_EVENTS: dict[EventType, str] = {
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
    EventType.RECOVERY_RECOMMENDED: "recovery.recommended",
    EventType.VERIFICATION_COMPLETED: "incident.verified",
    EventType.DEGRADATION_DETECTED: "degradation.detected",
    EventType.HUMAN_REVIEW_REQUESTED: "review.requested",
}

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


def stream_notifier(stream: EventStream) -> Handler:
    """Push a thin notice to connected browsers. Never raises: the UI is not the pipeline."""

    def handle(event: DomainEvent) -> None:
        name = STREAMED_EVENTS.get(event.event_type)
        if name is None:
            return
        payload = event.payload or {}
        stream.publish_soon(
            incident_event(
                name,
                str(payload.get("incident_id") or event.aggregate_id),
                status=_as_str(payload.get("status")),
                priority=_as_str(payload.get("priority")),
                region=_as_str(payload.get("region")),
            ),
        )

    return handle


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
    stream: EventStream | None = None,
) -> dict[EventType, Sequence[Handler]]:
    """Wire every event type to its handlers.

    Pipeline consumers (correlation, impact, notification, verification) are attached here
    as their modules land; the two cross-cutting consumers below apply to every phase.
    """
    audit = AuditService(sink=SqlAuditSink(engine), clock=clock)
    notify = stream_notifier(stream or get_stream())
    record = event_auditor(audit)

    consumers: dict[EventType, list[Handler]] = {}
    for event_type in STREAMED_EVENTS:
        consumers.setdefault(event_type, []).append(notify)
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


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
