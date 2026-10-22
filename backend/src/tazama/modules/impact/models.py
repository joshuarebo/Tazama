"""Impact DTOs.

Impact deliberately does not import the incidents module: it takes an `ImpactTarget`
built by the caller. That keeps the two modules independent (CLAUDE.md section 3) and
makes impact testable from a literal.

Every number in `Impact` carries the evidence that produced it, because these numbers
drive priority — and a P1 that nobody can explain is a P1 nobody will trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from tazama.domain.value_objects import KpiName, TimeWindow


class AffectedService(StrEnum):
    """Customer-visible services, inferred from which KPI families broke."""

    VOICE = "voice"
    DATA = "data"
    SMS = "sms"
    SIGNALLING = "signalling"


#: Which service a breached KPI family tells us about. One KPI can implicate several:
#: accessibility failures break everything, while throughput only breaks data.
SERVICE_BY_KPI: dict[KpiName, tuple[AffectedService, ...]] = {
    KpiName.AVAILABILITY: (
        AffectedService.VOICE,
        AffectedService.DATA,
        AffectedService.SMS,
        AffectedService.SIGNALLING,
    ),
    KpiName.RRC_SETUP_SUCCESS: (AffectedService.SIGNALLING, AffectedService.VOICE),
    KpiName.ERAB_SETUP_SUCCESS: (AffectedService.DATA, AffectedService.SIGNALLING),
    KpiName.DROP_RATE: (AffectedService.VOICE,),
    KpiName.PRB_UTILISATION: (AffectedService.DATA,),
    KpiName.DL_THROUGHPUT: (AffectedService.DATA,),
    KpiName.UL_THROUGHPUT: (AffectedService.DATA,),
    KpiName.LATENCY: (AffectedService.DATA, AffectedService.VOICE),
    KpiName.ACTIVE_USERS: (AffectedService.DATA,),
    KpiName.SINR: (AffectedService.VOICE, AffectedService.DATA),
}


@dataclass(frozen=True, slots=True)
class ImpactTarget:
    """What impact needs to know about the incident it is measuring."""

    incident_id: str
    region: str
    window: TimeWindow
    cause: str = "unknown"
    primary_node_id: str | None = None
    primary_node_kind: str | None = None
    severity: float = 0.0
    complaint_count: int = 0
    cell_ids: tuple[str, ...] = ()

    @property
    def duration_minutes(self) -> float:
        return round(self.window.duration.total_seconds() / 60.0, 2)

    @property
    def is_silent_sufferer(self) -> bool:
        """Degradation nobody reported. Prioritisation treats this as an amplifier."""
        return self.complaint_count == 0


@dataclass(frozen=True, slots=True)
class CellFacts:
    """Per-cell facts used for the subscriber estimate."""

    cell_id: str
    site_id: str
    region: str
    subscribers_est: int = 0
    tech: str = ""


@dataclass(frozen=True, slots=True)
class EnterpriseExposure:
    """Enterprise subscribers on the affected cells, and their SLA tiers."""

    accounts: int = 0
    subscribers: int = 0
    tiers: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImpactEvidence:
    """How one number was arrived at. `inputs` is the arithmetic, in the open."""

    metric: str
    value: float
    method: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "impact",
            "ref": self.metric,
            "note": f"{self.value:g} via {self.method}",
            "weight": 1.0,
            "inputs": dict(self.inputs),
        }


@dataclass(frozen=True, slots=True)
class Impact:
    """The impact of one incident.

    `affected_subscribers` is an **estimate**, not a measurement: it is the sum of
    `cell.subscribers_est` over the affected cells, which is a planning figure for how
    many subscribers a cell is dimensioned to serve. It is not a count of people who were
    actually on the cell at the time — that would need per-subscriber session data the
    platform deliberately does not hold. Treat it as an upper bound on exposure.
    """

    incident_id: str
    calculated_at: datetime
    affected_cells: int
    affected_sites: int
    affected_subscribers: int
    enterprise_accounts: int
    enterprise_subscribers: int
    affected_services: tuple[AffectedService, ...]
    breached_kpis: tuple[KpiName, ...]
    duration_minutes: float
    severity: float
    complaint_count: int
    sla_risk: float
    is_silent_sufferer: bool
    sla_tiers: tuple[str, ...] = ()
    cell_ids: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()
    evidence: tuple[ImpactEvidence, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Flat shape for the event payload and the prioritisation module's inputs."""
        return {
            "incident_id": self.incident_id,
            "affected_cells": self.affected_cells,
            "affected_sites": self.affected_sites,
            "affected_subscribers": self.affected_subscribers,
            "enterprise_accounts": self.enterprise_accounts,
            "enterprise_subscribers": self.enterprise_subscribers,
            "affected_services": [str(service) for service in self.affected_services],
            "breached_kpis": [kpi.value for kpi in self.breached_kpis],
            "duration_minutes": self.duration_minutes,
            "severity": self.severity,
            "complaint_count": self.complaint_count,
            "sla_risk": self.sla_risk,
            "is_silent_sufferer": self.is_silent_sufferer,
            "sla_tiers": list(self.sla_tiers),
            "estimate_note": "affected_subscribers is a dimensioning estimate, not a count",
        }
