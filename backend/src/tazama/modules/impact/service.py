"""Impact: how big is this, who is exposed, and how much SLA risk does it carry.

These numbers drive priority, so every one of them is explained in the returned evidence:
which cells were counted, which KPI families implicated which service, and exactly how
the SLA-risk score was composed. A number that cannot be re-derived from the evidence is
a bug in this module.
"""

from __future__ import annotations

from collections.abc import Sequence

from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.domain.events import AggregateType, ImpactCalculated
from tazama.domain.value_objects import KpiName
from tazama.modules.impact.models import (
    SERVICE_BY_KPI,
    AffectedService,
    EnterpriseExposure,
    Impact,
    ImpactEvidence,
    ImpactTarget,
)
from tazama.modules.impact.ports import (
    CellStats,
    DecisionLog,
    EventPublisher,
    IncidentCellReader,
    KpiFacts,
    SubscriberStats,
)

_log = get_logger(__name__)

#: Enterprise accounts at which enterprise exposure is considered saturated. Beyond this
#: the score should not keep climbing: twelve accounts and twenty are both "a lot", and
#: the difference should not outrank a four-hour outage.
SLA_ENTERPRISE_SATURATION = 10

#: Duration at which the duration term saturates (four hours). Past this an incident is
#: already an SLA problem; the extra minutes stop adding information.
SLA_DURATION_SATURATION_MINUTES = 240.0

#: Weights of the three SLA-risk terms. They sum to 1.0, which is what makes the score
#: comparable between incidents (the same discipline as prioritisation.weights).
SLA_WEIGHT_ENTERPRISE = 0.45
SLA_WEIGHT_DURATION = 0.30
SLA_WEIGHT_SEVERITY = 0.25

#: Platinum/gold tiers carry tighter commitments, so their presence lifts the enterprise
#: term rather than being averaged away.
_TIER_WEIGHT: dict[str, float] = {"platinum": 1.0, "gold": 0.8, "silver": 0.6}


class ImpactService:
    """Calculates the impact of one incident and publishes `ImpactCalculated`."""

    def __init__(
        self,
        *,
        incident_cells: IncidentCellReader,
        cells: CellStats,
        subscribers: SubscriberStats,
        kpis: KpiFacts,
        publisher: EventPublisher,
        decisions: DecisionLog,
        clock: Clock,
        policies: Policies,
    ) -> None:
        self._incident_cells = incident_cells
        self._cells = cells
        self._subscribers = subscribers
        self._kpis = kpis
        self._publisher = publisher
        self._decisions = decisions
        self._clock = clock
        self._policies = policies

    def calculate(self, target: ImpactTarget) -> Impact:
        """Measure the incident's reach, exposure and SLA risk.

        `affected_subscribers` is the sum of `cell.subscribers_est` over the affected
        cells. That is a **dimensioning estimate**, not a measurement of who was on the
        cell: the platform holds no per-subscriber session data, by design. It is the
        right order of magnitude and an upper bound on exposure, and it is labelled as an
        estimate everywhere it is shown.
        """
        cell_ids = target.cell_ids or self._incident_cells.cells_for(target.incident_id)
        cell_ids = tuple(sorted(set(cell_ids)))
        facts = self._cells.for_cells(cell_ids) if cell_ids else ()
        site_ids = tuple(sorted({fact.site_id for fact in facts}))
        subscribers_est = sum(fact.subscribers_est for fact in facts)
        exposure = (
            self._subscribers.enterprise_exposure(cell_ids) if cell_ids else EnterpriseExposure()
        )
        breached = self._kpis.breached_kpis(cell_ids, target.window) if cell_ids else ()
        services = _services_for(breached)
        sla_risk, sla_evidence = self._sla_risk(target, exposure)

        evidence: list[ImpactEvidence] = [
            ImpactEvidence(
                metric="affected_cells",
                value=float(len(cell_ids)),
                method="cells linked to the incident by correlation",
                inputs={"cell_ids": list(cell_ids[:25]), "truncated": len(cell_ids) > 25},
            ),
            ImpactEvidence(
                metric="affected_sites",
                value=float(len(site_ids)),
                method="distinct parent sites of the affected cells",
                inputs={"site_ids": list(site_ids[:25]), "truncated": len(site_ids) > 25},
            ),
            ImpactEvidence(
                metric="affected_subscribers",
                value=float(subscribers_est),
                method="sum of cell.subscribers_est (dimensioning estimate, not a count)",
                inputs={
                    "per_cell": {fact.cell_id: fact.subscribers_est for fact in facts[:25]},
                    "cells_counted": len(facts),
                },
            ),
            ImpactEvidence(
                metric="enterprise_accounts",
                value=float(exposure.accounts),
                method="distinct subscriber.enterprise_ref where is_enterprise on these cells",
                inputs={
                    "enterprise_subscribers": exposure.subscribers,
                    "sla_tiers": list(exposure.tiers),
                    "refs": list(exposure.refs[:25]),
                },
            ),
            ImpactEvidence(
                metric="affected_services",
                value=float(len(services)),
                method="inferred from the breached KPI families",
                inputs={
                    "breached_kpis": [kpi.value for kpi in breached],
                    "mapping": {
                        kpi.value: [str(s) for s in SERVICE_BY_KPI.get(kpi, ())] for kpi in breached
                    },
                },
            ),
            ImpactEvidence(
                metric="duration_minutes",
                value=target.duration_minutes,
                method="incident window end minus start (half-open, UTC)",
                inputs={
                    "window_start": target.window.start.isoformat(),
                    "window_end": target.window.end.isoformat(),
                },
            ),
            sla_evidence,
        ]

        impact = Impact(
            incident_id=target.incident_id,
            calculated_at=self._clock.now(),
            affected_cells=len(cell_ids),
            affected_sites=len(site_ids),
            affected_subscribers=subscribers_est,
            enterprise_accounts=exposure.accounts,
            enterprise_subscribers=exposure.subscribers,
            affected_services=services,
            breached_kpis=breached,
            duration_minutes=target.duration_minutes,
            severity=round(target.severity, 4),
            complaint_count=target.complaint_count,
            sla_risk=sla_risk,
            is_silent_sufferer=target.is_silent_sufferer,
            sla_tiers=exposure.tiers,
            cell_ids=cell_ids,
            site_ids=site_ids,
            evidence=tuple(evidence),
        )
        self._publish(impact)
        self._decisions.record(
            action="calculate_impact",
            entity_type="incident",
            entity_id=target.incident_id,
            outcome="calculated",
            inputs=impact.to_payload(),
            evidence=impact.evidence,
            confidence=None,
            rule_version=self._policies.rule_version,
        )
        _log.debug(
            "impact_calculated",
            extra={
                "incident_id": target.incident_id,
                "affected_cells": impact.affected_cells,
                "sla_risk": impact.sla_risk,
            },
        )
        return impact

    # ------------------------------------------------------------------ scoring

    def _sla_risk(
        self,
        target: ImpactTarget,
        exposure: EnterpriseExposure,
    ) -> tuple[float, ImpactEvidence]:
        """Blend enterprise exposure, duration and severity into a 0..1 score."""
        tier_lift = max(
            (_TIER_WEIGHT.get(tier, 0.5) for tier in exposure.tiers),
            default=0.0,
        )
        enterprise_term = min(
            exposure.accounts / SLA_ENTERPRISE_SATURATION,
            1.0,
        ) * (0.6 + 0.4 * tier_lift)
        duration_term = min(target.duration_minutes / SLA_DURATION_SATURATION_MINUTES, 1.0)
        severity_term = min(max(target.severity, 0.0), 1.0)
        score = (
            SLA_WEIGHT_ENTERPRISE * enterprise_term
            + SLA_WEIGHT_DURATION * duration_term
            + SLA_WEIGHT_SEVERITY * severity_term
        )
        score = round(min(max(score, 0.0), 1.0), 4)
        return score, ImpactEvidence(
            metric="sla_risk",
            value=score,
            method=(
                f"{SLA_WEIGHT_ENTERPRISE}*enterprise + {SLA_WEIGHT_DURATION}*duration "
                f"+ {SLA_WEIGHT_SEVERITY}*severity"
            ),
            inputs={
                "enterprise_accounts": exposure.accounts,
                "enterprise_saturation": SLA_ENTERPRISE_SATURATION,
                "best_tier_weight": round(tier_lift, 3),
                "enterprise_term": round(enterprise_term, 4),
                "duration_minutes": target.duration_minutes,
                "duration_saturation_minutes": SLA_DURATION_SATURATION_MINUTES,
                "duration_term": round(duration_term, 4),
                "severity": round(severity_term, 4),
                "severity_term": round(severity_term, 4),
            },
        )

    def _publish(self, impact: Impact) -> None:
        self._publisher.publish(
            ImpactCalculated(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=impact.incident_id,
                occurred_at=impact.calculated_at,
                dedupe_key=(
                    f"{impact.incident_id}|{impact.affected_cells}|"
                    f"{impact.affected_subscribers}|{impact.sla_risk}"
                ),
                payload=impact.to_payload(),
            ),
        )


# --------------------------------------------------------------------- helpers


def _services_for(breached: Sequence[KpiName]) -> tuple[AffectedService, ...]:
    """Union of the services implicated by each breached KPI family, in a stable order."""
    found: set[AffectedService] = set()
    for kpi in breached:
        found.update(SERVICE_BY_KPI.get(kpi, ()))
    return tuple(service for service in AffectedService if service in found)


__all__ = ["SLA_DURATION_SATURATION_MINUTES", "SLA_ENTERPRISE_SATURATION", "ImpactService"]
