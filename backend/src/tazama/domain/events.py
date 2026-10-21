"""Domain events: the backbone of the pipeline (PRD section 8).

Rules that make the chain replayable and safe to evolve:
  * **Versioned and additive-only.** A consumer must keep working when a producer adds a
    field, so new fields are optional with a default and no field is ever removed or
    repurposed. Bump `version` when meaning changes, and keep handling the old version.
  * **Deterministic identity.** `event_id` is derived from the event type, the aggregate
    and a dedupe key, so publishing the same fact twice yields the same id and consumers
    can be idempotent on it. Replay produces identical ids.
  * **Pure data.** No I/O, no clock, no database. The payload carries pseudonymised
    identifiers only.
  * **Correlation travels with the event**, so one request can be traced from the API
    through the worker to the decision it caused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Self

from tazama.core.ids import deterministic_id


class EventType(StrEnum):
    """Every event in the pipeline, in the order the PRD lists them."""

    COMPLAINT_RECEIVED = "ComplaintReceived"
    COMPLAINT_VALIDATED = "ComplaintValidated"
    COMPLAINT_ENRICHED = "ComplaintEnriched"
    SITE_IDENTIFIED = "SiteIdentified"
    CORRELATED = "Correlated"
    INCIDENT_DETECTED = "IncidentDetected"
    INCIDENT_UPDATED = "IncidentUpdated"
    IMPACT_CALCULATED = "ImpactCalculated"
    DIAGNOSIS_COMPLETED = "DiagnosisCompleted"
    CASE_PRIORITIZED = "CasePrioritized"
    CASE_ROUTED = "CaseRouted"
    NOTIFICATION_PROPOSED = "NotificationProposed"
    NOTIFICATION_APPROVED = "NotificationApproved"
    NOTIFICATION_REJECTED = "NotificationRejected"
    NOTIFICATION_SENT = "NotificationSent"
    NOTIFICATION_SUPPRESSED = "NotificationSuppressed"
    RECOVERY_RECOMMENDED = "RecoveryRecommended"
    RECOVERY_APPROVED = "RecoveryApproved"
    VERIFICATION_COMPLETED = "VerificationCompleted"
    CASE_CLOSED = "CaseClosed"
    CASE_REOPENED = "CaseReopened"
    #: Network-first entry point: degradation seen before anyone complains.
    DEGRADATION_DETECTED = "DegradationDetected"
    #: Raised when a human must decide (low confidence, policy refusal).
    HUMAN_REVIEW_REQUESTED = "HumanReviewRequested"


class AggregateType(StrEnum):
    COMPLAINT = "complaint"
    INCIDENT = "incident"
    CASE = "case"
    NOTIFICATION = "notification"
    CELL = "cell"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class. Subclasses add a typed payload and nothing else."""

    #: Declared once per subclass; never read from the payload.
    event_type: ClassVar[EventType]
    version: ClassVar[int] = 1

    aggregate_type: AggregateType
    aggregate_id: str
    occurred_at: datetime
    #: Stable natural key for this fact. Two events with the same key are the same event.
    dedupe_key: str
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_id(self) -> str:
        """Deterministic id: the same fact always produces the same id."""
        return deterministic_id(
            "EVT",
            str(self.event_type),
            str(self.version),
            self.aggregate_type,
            self.aggregate_id,
            self.dedupe_key,
        )

    def to_record(self) -> dict[str, Any]:
        """Flat shape the outbox stores. `payload` is JSON; the rest are columns."""
        return {
            "id": self.event_id,
            "event_type": str(self.event_type),
            "event_version": self.version,
            "aggregate_type": str(self.aggregate_type),
            "aggregate_id": self.aggregate_id,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at,
            "dedupe_key": self.dedupe_key,
            "correlation_id": self.correlation_id or None,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> DomainEvent:
        """Rebuild an event from an outbox row (used by consumers and by replay)."""
        event_type = EventType(record["event_type"])
        target = EVENT_CLASSES.get(event_type, DomainEvent)
        rebuilt = object.__new__(target)
        object.__setattr__(rebuilt, "aggregate_type", AggregateType(record["aggregate_type"]))
        object.__setattr__(rebuilt, "aggregate_id", record["aggregate_id"])
        object.__setattr__(rebuilt, "occurred_at", record["occurred_at"])
        object.__setattr__(rebuilt, "dedupe_key", record["dedupe_key"])
        object.__setattr__(rebuilt, "correlation_id", record.get("correlation_id") or "")
        object.__setattr__(rebuilt, "payload", dict(record.get("payload") or {}))
        return rebuilt

    def with_payload(self, **extra: Any) -> Self:
        """Additive copy — used by consumers that enrich an event before re-publishing."""
        merged = {**self.payload, **extra}
        return type(self)(
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            occurred_at=self.occurred_at,
            dedupe_key=self.dedupe_key,
            correlation_id=self.correlation_id,
            payload=merged,
        )


# --------------------------------------------------------- complaint journey


@dataclass(frozen=True, slots=True, kw_only=True)
class ComplaintReceived(DomainEvent):
    event_type: ClassVar[EventType] = EventType.COMPLAINT_RECEIVED


@dataclass(frozen=True, slots=True, kw_only=True)
class ComplaintValidated(DomainEvent):
    event_type: ClassVar[EventType] = EventType.COMPLAINT_VALIDATED


@dataclass(frozen=True, slots=True, kw_only=True)
class ComplaintEnriched(DomainEvent):
    event_type: ClassVar[EventType] = EventType.COMPLAINT_ENRICHED


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteIdentified(DomainEvent):
    """Payload: `cell_id`, `site_id`, `confidence`, `method`, `needs_human`."""

    event_type: ClassVar[EventType] = EventType.SITE_IDENTIFIED


@dataclass(frozen=True, slots=True, kw_only=True)
class Correlated(DomainEvent):
    """Payload: `incident_id`, `reason`, `weight`."""

    event_type: ClassVar[EventType] = EventType.CORRELATED


# ------------------------------------------------------------ network first


@dataclass(frozen=True, slots=True, kw_only=True)
class DegradationDetected(DomainEvent):
    """Payload: `cell_id`, `kpi`, `value`, `threshold`, `window_start`, `window_end`."""

    event_type: ClassVar[EventType] = EventType.DEGRADATION_DETECTED


# ---------------------------------------------------------------- incidents


@dataclass(frozen=True, slots=True, kw_only=True)
class IncidentDetected(DomainEvent):
    event_type: ClassVar[EventType] = EventType.INCIDENT_DETECTED


@dataclass(frozen=True, slots=True, kw_only=True)
class IncidentUpdated(DomainEvent):
    event_type: ClassVar[EventType] = EventType.INCIDENT_UPDATED


@dataclass(frozen=True, slots=True, kw_only=True)
class ImpactCalculated(DomainEvent):
    event_type: ClassVar[EventType] = EventType.IMPACT_CALCULATED


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagnosisCompleted(DomainEvent):
    event_type: ClassVar[EventType] = EventType.DIAGNOSIS_COMPLETED


# -------------------------------------------------------------------- cases


@dataclass(frozen=True, slots=True, kw_only=True)
class CasePrioritized(DomainEvent):
    event_type: ClassVar[EventType] = EventType.CASE_PRIORITIZED


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseRouted(DomainEvent):
    event_type: ClassVar[EventType] = EventType.CASE_ROUTED


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseClosed(DomainEvent):
    event_type: ClassVar[EventType] = EventType.CASE_CLOSED


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseReopened(DomainEvent):
    event_type: ClassVar[EventType] = EventType.CASE_REOPENED


# ------------------------------------------------------------ notifications


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationProposed(DomainEvent):
    event_type: ClassVar[EventType] = EventType.NOTIFICATION_PROPOSED


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationApproved(DomainEvent):
    event_type: ClassVar[EventType] = EventType.NOTIFICATION_APPROVED


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationRejected(DomainEvent):
    event_type: ClassVar[EventType] = EventType.NOTIFICATION_REJECTED


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationSent(DomainEvent):
    event_type: ClassVar[EventType] = EventType.NOTIFICATION_SENT


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationSuppressed(DomainEvent):
    """Payload: `reason` — quiet hours, dedupe, opt-out, rate limit or storm guard."""

    event_type: ClassVar[EventType] = EventType.NOTIFICATION_SUPPRESSED


# ---------------------------------------------------------------- recovery


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryRecommended(DomainEvent):
    event_type: ClassVar[EventType] = EventType.RECOVERY_RECOMMENDED


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApproved(DomainEvent):
    event_type: ClassVar[EventType] = EventType.RECOVERY_APPROVED


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationCompleted(DomainEvent):
    event_type: ClassVar[EventType] = EventType.VERIFICATION_COMPLETED


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanReviewRequested(DomainEvent):
    """Payload: `reason`, `confidence`, `queue`."""

    event_type: ClassVar[EventType] = EventType.HUMAN_REVIEW_REQUESTED


#: Lookup used by `DomainEvent.from_record`. Every EventType must appear here, which a
#: test asserts — an event that cannot be rebuilt cannot be replayed.
EVENT_CLASSES: dict[EventType, type[DomainEvent]] = {
    EventType.COMPLAINT_RECEIVED: ComplaintReceived,
    EventType.COMPLAINT_VALIDATED: ComplaintValidated,
    EventType.COMPLAINT_ENRICHED: ComplaintEnriched,
    EventType.SITE_IDENTIFIED: SiteIdentified,
    EventType.CORRELATED: Correlated,
    EventType.DEGRADATION_DETECTED: DegradationDetected,
    EventType.INCIDENT_DETECTED: IncidentDetected,
    EventType.INCIDENT_UPDATED: IncidentUpdated,
    EventType.IMPACT_CALCULATED: ImpactCalculated,
    EventType.DIAGNOSIS_COMPLETED: DiagnosisCompleted,
    EventType.CASE_PRIORITIZED: CasePrioritized,
    EventType.CASE_ROUTED: CaseRouted,
    EventType.CASE_CLOSED: CaseClosed,
    EventType.CASE_REOPENED: CaseReopened,
    EventType.NOTIFICATION_PROPOSED: NotificationProposed,
    EventType.NOTIFICATION_APPROVED: NotificationApproved,
    EventType.NOTIFICATION_REJECTED: NotificationRejected,
    EventType.NOTIFICATION_SENT: NotificationSent,
    EventType.NOTIFICATION_SUPPRESSED: NotificationSuppressed,
    EventType.RECOVERY_RECOMMENDED: RecoveryRecommended,
    EventType.RECOVERY_APPROVED: RecoveryApproved,
    EventType.VERIFICATION_COMPLETED: VerificationCompleted,
    EventType.HUMAN_REVIEW_REQUESTED: HumanReviewRequested,
}
