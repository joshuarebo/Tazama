"""DTOs for the correlation module: observations in, incident candidates out.

Why these shapes:
  * **Observations are already located.** Correlation is not site identification. A
    complaint arrives with the cell it was served by (or it is not correlatable), which
    keeps the two hardest problems in the product separable and separately measurable.
  * **Event time, not ingestion time.** Every observation carries both. The engine always
    reasons on `event_time`, so a complaint that took six hours to reach us still lands in
    the window where the outage actually happened (PRD section 15, late/out-of-order).
  * **Evidence is a reference, never a copy.** An auditor must be able to re-read the row
    that drove a decision; a snapshot would silently go stale.
  * **A candidate is a proposal.** Correlation decides *what* the incident is; the
    incidents module owns the aggregate and its lifecycle. The candidate's `key` is
    derived from (primary element, cause, window bucket), which is what makes a replay
    converge on the same incident instead of creating a second one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from tazama.core.clock import floor_to
from tazama.core.ids import deterministic_id
from tazama.domain.value_objects import (
    NETWORK_CATEGORIES,
    AlarmSeverity,
    ComplaintCategory,
    GeoPoint,
    IncidentCause,
    KpiName,
    NodeKind,
    TimeWindow,
)

#: Incident identity is bucketed to the hour. Two observations of the same fault an hour
#: apart are the same incident; a genuinely new fault on the same element a day later is
#: not. Hours also match the coarse KPI grain, so a replay cannot land in a new bucket.
IDENTITY_BUCKET_MINUTES = 60


# ------------------------------------------------------------------ observations


@dataclass(frozen=True, slots=True)
class ComplaintObservation:
    """A located complaint, as correlation sees it.

    `dedupe_key` is what stops one angry subscriber manufacturing an incident: the engine
    counts distinct keys, never rows.
    """

    complaint_id: str
    cell_id: str
    site_id: str
    region: str
    category: ComplaintCategory
    received_at: datetime
    source_ts: datetime | None = None
    dedupe_key: str = ""
    subscriber_id: str | None = None
    channel: str = ""

    @property
    def event_time(self) -> datetime:
        """When the customer experienced the problem, not when we heard about it."""
        return self.source_ts or self.received_at

    @property
    def lag(self) -> timedelta:
        return self.received_at - self.event_time

    @property
    def is_network(self) -> bool:
        return self.category in NETWORK_CATEGORIES

    @property
    def dedupe_token(self) -> str:
        """Never empty: a complaint with no key is its own key."""
        return self.dedupe_key or self.complaint_id


@dataclass(frozen=True, slots=True)
class CellInfo:
    """The cell facts correlation needs: where it is and how many people it serves."""

    cell_id: str
    site_id: str
    region: str
    lat: float
    lon: float
    subscribers_est: int = 0
    tech: str = ""

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(lat=self.lat, lon=self.lon)


@dataclass(frozen=True, slots=True)
class KpiSample:
    cell_id: str
    bucket_start: datetime
    value: float | None


@dataclass(frozen=True, slots=True)
class KpiBreach:
    """One cell's KPI health over a window, aggregated in SQL rather than in Python.

    `severity` is the share of KPI samples in the window that breached their modelled
    threshold. A single noisy KPI therefore cannot look like an outage, and a collapse
    across accessibility, retainability and integrity cannot look like noise.
    """

    cell_id: str
    site_id: str
    region: str
    window: TimeWindow
    buckets: int
    breached_buckets: int
    severity: float
    breached_kpis: tuple[KpiName, ...] = ()
    worst: dict[str, float] = field(default_factory=dict)

    @property
    def is_hard_outage(self) -> bool:
        """Availability on the floor is an outage however many other KPIs agree."""
        availability = self.worst.get(KpiName.AVAILABILITY.value)
        return availability is not None and availability < 50.0

    def worst_of(self, kpi: KpiName) -> float | None:
        return self.worst.get(kpi.value)


@dataclass(frozen=True, slots=True)
class AlarmRecord:
    alarm_id: str
    node_id: str
    node_kind: str
    alarm_type: str
    severity: AlarmSeverity
    raised_at: datetime
    cleared_at: datetime | None = None
    cell_id: str | None = None
    site_id: str | None = None
    is_flapping: bool = False

    @property
    def is_significant(self) -> bool:
        return self.severity in (AlarmSeverity.CRITICAL, AlarmSeverity.MAJOR)


@dataclass(frozen=True, slots=True)
class AlarmTransition:
    """A raise or a clear. Flapping is counted in transitions, not in alarms."""

    node_id: str
    at: datetime
    kind: str  # raise | clear


@dataclass(frozen=True, slots=True)
class MaintenanceCover:
    maintenance_id: str
    node_id: str
    node_kind: str
    starts_at: datetime
    ends_at: datetime
    approved_by: str | None = None
    description: str = ""

    @property
    def is_approved(self) -> bool:
        """An unapproved window suppresses nothing: suppression needs an owner."""
        return bool(self.approved_by)

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(start=self.starts_at, end=self.ends_at)


@dataclass(frozen=True, slots=True)
class TopologyNodeInfo:
    node_id: str
    kind: NodeKind
    name: str = ""
    region: str | None = None


@dataclass(frozen=True, slots=True)
class ComplaintCount:
    cell_id: str
    total: int
    distinct: int


# ------------------------------------------------------------------- evidence


@dataclass(frozen=True, slots=True)
class Evidence:
    """One named row that drove a decision. Same shape as the audit module's evidence."""

    kind: str  # kpi | alarm | complaint | topology | maintenance | baseline
    ref: str
    note: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "note": self.note, "weight": self.weight}


@dataclass(frozen=True, slots=True)
class CorrelationLink:
    """What an incident was built from, with why and how much it counted."""

    entity_type: str  # complaint | complaint_dedupe | alarm | kpi | cell | site | topology
    entity_id: str
    weight: float = 1.0
    rationale: str = ""


# ------------------------------------------------------------------- clusters


@dataclass(frozen=True, slots=True)
class ComplaintCluster:
    """Complaints grouped in space and time, normalised by exposure.

    `qualifies` is the whole point of the exposure normalisation: five complaints from a
    cell serving 5,000 subscribers is noise, while five from a cell serving 400 is an
    outage. The raw count alone cannot tell those apart.
    """

    cell_ids: tuple[str, ...]
    complaint_ids: tuple[str, ...]
    dedupe_keys: tuple[str, ...]
    window: TimeWindow
    subscribers_est: int
    radius_km: float = 0.0

    @property
    def raw_count(self) -> int:
        return len(self.complaint_ids)

    @property
    def distinct_count(self) -> int:
        """Duplicate storms count once (PRD section 15, duplicate events)."""
        return len(set(self.dedupe_keys))

    @property
    def per_1k_subs(self) -> float:
        if self.subscribers_est <= 0:
            return float(self.distinct_count)
        return self.distinct_count * 1000.0 / self.subscribers_est

    def qualifies(self, *, min_complaints: int, min_per_1k_subs: float) -> bool:
        return self.distinct_count >= min_complaints and self.per_1k_subs >= min_per_1k_subs


# ------------------------------------------------------------------ candidates


class CorrelationDecision(StrEnum):
    """What correlation did with an observation or a degradation."""

    CREATED = "created"
    UPDATED = "updated"
    ATTACHED = "attached"
    REOPENED = "reopened"
    SUPPRESSED = "suppressed"
    REJECTED = "rejected"
    NO_MATCH = "no_match"


class SuppressionReason(StrEnum):
    """Why something that looked like an incident deliberately is not one."""

    PLANNED_MAINTENANCE = "planned_maintenance"
    FLAPPING = "flapping"
    NON_NETWORK = "non_network"
    DUPLICATE = "duplicate"
    UNCORROBORATED = "uncorroborated"
    BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True)
class IncidentCandidate:
    """A proposed incident. The incidents module turns this into the aggregate."""

    primary_node_id: str
    primary_node_kind: NodeKind
    cause: IncidentCause
    window: TimeWindow
    region: str
    cell_ids: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()
    complaint_ids: tuple[str, ...] = ()
    alarm_ids: tuple[str, ...] = ()
    severity: float = 0.0
    confidence: float = 0.5
    complaint_count: int = 0
    title: str = ""
    summary: str = ""
    detector: str = "correlation"
    suppressed_reason: str | None = None
    evidence: tuple[Evidence, ...] = ()
    links: tuple[CorrelationLink, ...] = ()
    rule_version: str = ""

    @property
    def key(self) -> str:
        """Deterministic identity: same element, same cause, same hour -> same incident."""
        bucket = floor_to(self.window.start, IDENTITY_BUCKET_MINUTES)
        return deterministic_id(
            "INC",
            self.primary_node_id,
            str(self.cause),
            bucket.isoformat(),
        )

    @property
    def is_suppressed(self) -> bool:
        return self.suppressed_reason is not None


@dataclass(frozen=True, slots=True)
class IncidentRef:
    """The incidents module's view of an incident, as correlation needs to see it."""

    incident_id: str
    status: str
    primary_node_id: str
    primary_node_kind: str
    cause: str
    window: TimeWindow
    version: int = 1

    @property
    def is_closed(self) -> bool:
        return self.status in ("closed", "resolved")

    @property
    def is_suppressed(self) -> bool:
        return self.status == "suppressed"


@dataclass(frozen=True, slots=True)
class CorrelationOutcome:
    """One decision, with everything needed to defend it later."""

    decision: CorrelationDecision
    candidate: IncidentCandidate | None = None
    incident_id: str | None = None
    links: tuple[CorrelationLink, ...] = ()
    suppressed_reason: str | None = None
    evidence: tuple[Evidence, ...] = ()
    confidence: float = 0.0
    reopen_requested: bool = False
    note: str = ""

    @property
    def raised_incident(self) -> bool:
        """True only when a live, non-suppressed incident exists because of this."""
        return (
            self.incident_id is not None
            and self.suppressed_reason is None
            and self.decision
            in (
                CorrelationDecision.CREATED,
                CorrelationDecision.UPDATED,
                CorrelationDecision.ATTACHED,
                CorrelationDecision.REOPENED,
            )
        )


# --------------------------------------------------------------------- helpers


def union_window(windows: Iterable[TimeWindow]) -> TimeWindow:
    """Smallest window covering all of them. Raises on an empty iterable, by design."""
    items = list(windows)
    if not items:
        raise ValueError("union_window needs at least one window")
    return TimeWindow(
        start=min(w.start for w in items),
        end=max(w.end for w in items),
    )


def cluster_radius_km(cells: Sequence[CellInfo]) -> float:
    """Widest separation inside a cluster — reported so a reviewer can sanity-check it."""
    if len(cells) < 2:
        return 0.0
    return max(
        cells[i].point.distance_km(cells[j].point)
        for i in range(len(cells))
        for j in range(i + 1, len(cells))
    )
