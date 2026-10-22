"""Incident DTOs: the aggregate, its links, and the queries the UI needs.

The identity rule is the important part. An incident's id is derived from
(primary element, cause, window-start bucket), so the same fault observed twice — by a
replay, by a second worker, by the complaint path after the network path — converges on
one incident instead of creating a second one. Nothing in this module invents an id.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from tazama.core.clock import floor_to
from tazama.core.ids import deterministic_id
from tazama.domain.value_objects import (
    IncidentCause,
    IncidentStatus,
    NodeKind,
    Priority,
    TimeWindow,
)

#: Incident identity is bucketed to the hour; see `IncidentCandidate.key`.
IDENTITY_BUCKET_MINUTES = 60

#: Statuses an incident can no longer be *worked*, but can still be reopened from.
TERMINAL_STATUSES = frozenset({IncidentStatus.CLOSED, IncidentStatus.SUPPRESSED})

#: An incident always spans at least one KPI bucket, so its window is never empty.
_MINIMUM_SPAN = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class IncidentCandidate:
    """What correlation proposes. Everything here is derived from observations."""

    primary_node_id: str
    primary_node_kind: NodeKind
    cause: IncidentCause
    window: TimeWindow
    region: str
    cell_ids: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()
    alarm_ids: tuple[str, ...] = ()
    severity: float = 0.0
    confidence: float = 0.5
    complaint_count: int = 0
    title: str = ""
    summary: str = ""
    detector: str = "correlation"
    suppressed_reason: str | None = None
    rule_version: str | None = None

    @property
    def key(self) -> str:
        """Deterministic id — the basis of idempotency for the whole pipeline."""
        bucket = floor_to(self.window.start, IDENTITY_BUCKET_MINUTES)
        return deterministic_id(
            "INC",
            self.primary_node_id,
            str(self.cause),
            bucket.isoformat(),
        )


@dataclass(frozen=True, slots=True)
class Incident:
    """The aggregate. `version` is the optimistic lock; nothing updates without it."""

    id: str
    title: str
    status: IncidentStatus
    priority: Priority
    cause: IncidentCause
    region: str
    detected_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    window_end: datetime | None = None
    cause_confidence: float | None = None
    primary_node_id: str | None = None
    primary_node_kind: str | None = None
    affected_cells: int = 0
    affected_sites: int = 0
    affected_subs: int = 0
    enterprise_accounts: int = 0
    complaint_count: int = 0
    sla_risk: float | None = None
    summary: str | None = None
    suppressed_reason: str | None = None
    detector: str = "correlation"
    rule_version: str | None = None
    version: int = 1

    @property
    def window(self) -> TimeWindow:
        """The incident's event window. `window_end` tracks the latest evidence seen."""
        start = self.started_at or self.detected_at
        end = self.window_end or self.resolved_at or self.closed_at
        if end is None or end <= start:
            end = start + _MINIMUM_SPAN
        return TimeWindow(start=start, end=end)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES and self.status is not IncidentStatus.RESOLVED

    @property
    def is_suppressed(self) -> bool:
        return self.status is IncidentStatus.SUPPRESSED

    @property
    def duration_minutes(self) -> float:
        return round(self.window.duration.total_seconds() / 60.0, 2)

    def with_changes(self, **changes: Any) -> Incident:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class IncidentLink:
    """One piece of evidence attached to an incident, with why and how much it counted."""

    incident_id: str
    entity_type: str
    entity_id: str
    weight: float = 1.0
    rationale: str = ""
    linked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SimilarityQuery:
    """Structured filters first. Vector similarity is a Phase 4 refinement, not a basis.

    Filtering on cause, region, element kind and comparable size is explainable and
    cheap; an embedding that quietly matched a Mombasa power failure to a Nairobi
    congestion incident would be neither.
    """

    cause: IncidentCause
    region: str | None = None
    primary_node_kind: str | None = None
    min_affected_cells: int = 0
    max_affected_cells: int | None = None
    exclude_id: str | None = None
    limit: int = 5


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """Result of merging two incidents, for the audit trail."""

    into_id: str
    absorbed_id: str
    links_moved: int
    reason: str
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class VerificationSignal:
    """Proof that closure is allowed. Published by the verification module."""

    incident_id: str
    passed: bool
    checked_at: datetime
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One named row behind a decision. Same shape as every other module's evidence."""

    kind: str
    ref: str
    note: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "note": self.note, "weight": self.weight}
