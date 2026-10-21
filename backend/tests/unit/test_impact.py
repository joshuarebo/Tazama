"""Impact unit tests: the numbers, and the explanation of every number.

The explanation matters as much as the arithmetic here: these figures feed prioritisation,
and a P1 nobody can defend is a P1 nobody will act on. Each test therefore asserts both
the value and the evidence that justifies it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tazama.core.clock import FixedClock
from tazama.core.policy import Policies, load_policies
from tazama.domain.events import EventType
from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.infrastructure.eventbus.memory import InMemoryEventBus
from tazama.modules.impact.models import (
    AffectedService,
    CellFacts,
    EnterpriseExposure,
    ImpactTarget,
)
from tazama.modules.impact.service import (
    SLA_DURATION_SATURATION_MINUTES,
    SLA_ENTERPRISE_SATURATION,
    ImpactService,
)

NOW = datetime(2026, 3, 17, 0, 0, tzinfo=UTC)
DAY = datetime(2026, 3, 16, 0, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


# ===================================================================== fakes


@dataclass
class FakeIncidentCells:
    by_incident: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def cells_for(self, incident_id: str) -> tuple[str, ...]:
        return self.by_incident.get(incident_id, ())


@dataclass
class FakeCellStats:
    rows: dict[str, CellFacts] = field(default_factory=dict)

    def for_cells(self, cell_ids: Sequence[str]) -> tuple[CellFacts, ...]:
        return tuple(self.rows[cell_id] for cell_id in cell_ids if cell_id in self.rows)


@dataclass
class FakeSubscriberStats:
    exposure: EnterpriseExposure = field(default_factory=EnterpriseExposure)
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def enterprise_exposure(self, cell_ids: Sequence[str]) -> EnterpriseExposure:
        self.asked.append(tuple(cell_ids))
        return self.exposure


@dataclass
class FakeKpiFacts:
    breached: tuple[KpiName, ...] = ()

    def breached_kpis(
        self,
        cell_ids: Sequence[str],
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[KpiName, ...]:
        return self.breached


@dataclass
class FakeDecisionLog:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Sequence[Any] = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "action": action,
                "entity_id": entity_id,
                "outcome": outcome,
                "inputs": inputs,
                "evidence": tuple(evidence),
                "rule_version": rule_version,
            },
        )


@pytest.fixture
def policies(repo_root) -> Policies:
    return load_policies(repo_root / "config" / "policies.yaml")


def cell(cell_id: str, site_id: str, subscribers: int) -> CellFacts:
    return CellFacts(
        cell_id=cell_id,
        site_id=site_id,
        region="Nairobi",
        subscribers_est=subscribers,
        tech="4G",
    )


def build(
    policies: Policies,
    *,
    cells: Sequence[CellFacts] = (),
    exposure: EnterpriseExposure | None = None,
    breached: Sequence[KpiName] = (),
    incident_cells: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ImpactService, dict[str, Any]]:
    parts: dict[str, Any] = {
        "incident_cells": FakeIncidentCells(incident_cells or {}),
        "cells": FakeCellStats({row.cell_id: row for row in cells}),
        "subscribers": FakeSubscriberStats(exposure or EnterpriseExposure()),
        "kpis": FakeKpiFacts(tuple(breached)),
        "publisher": InMemoryEventBus(),
        "decisions": FakeDecisionLog(),
    }
    service = ImpactService(clock=FixedClock(NOW), policies=policies, **parts)
    return service, parts


def target(
    *,
    incident_id: str = "INC-1",
    start_hour: int = 6,
    end_hour: int = 9,
    cell_ids: tuple[str, ...] = (),
    severity: float = 0.9,
    complaints: int = 12,
) -> ImpactTarget:
    return ImpactTarget(
        incident_id=incident_id,
        region="Nairobi",
        window=TimeWindow(start=at(start_hour), end=at(end_hour)),
        cause="cell_outage",
        primary_node_id="CELL-A",
        primary_node_kind="cell",
        severity=severity,
        complaint_count=complaints,
        cell_ids=cell_ids,
    )


def evidence_for(impact: Any, metric: str) -> Any:
    return next(item for item in impact.evidence if item.metric == metric)


# ================================================================== reach


def test_affected_subscribers_is_the_sum_of_the_cells_estimates(policies) -> None:
    service, _ = build(
        policies,
        cells=[
            cell("CELL-A", "SITE-1", 1200),
            cell("CELL-B", "SITE-1", 800),
            cell("CELL-C", "SITE-2", 3000),
        ],
    )

    impact = service.calculate(target(cell_ids=("CELL-A", "CELL-B", "CELL-C")))

    assert impact.affected_subscribers == 5000
    assert impact.affected_cells == 3
    assert impact.affected_sites == 2
    evidence = evidence_for(impact, "affected_subscribers")
    assert evidence.value == 5000
    assert "estimate" in evidence.method
    assert evidence.inputs["per_cell"]["CELL-C"] == 3000


def test_the_subscriber_figure_is_documented_as_an_estimate(policies) -> None:
    """The docstring promise is part of the contract: it is shown wherever this is."""
    service, _ = build(policies, cells=[cell("CELL-A", "SITE-1", 10)])

    impact = service.calculate(target(cell_ids=("CELL-A",)))

    assert "estimate" in (impact.__doc__ or "")
    assert "not a measurement" in (impact.__doc__ or "")
    assert "estimate" in impact.to_payload()["estimate_note"]


def test_cells_come_from_the_incident_links_when_not_supplied(policies) -> None:
    service, parts = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 500), cell("CELL-B", "SITE-1", 700)],
        incident_cells={"INC-1": ("CELL-A", "CELL-B")},
    )

    impact = service.calculate(target())

    assert impact.affected_subscribers == 1200
    assert impact.cell_ids == ("CELL-A", "CELL-B")


def test_duplicate_cell_ids_are_counted_once(policies) -> None:
    service, _ = build(policies, cells=[cell("CELL-A", "SITE-1", 500)])

    impact = service.calculate(target(cell_ids=("CELL-A", "CELL-A")))

    assert impact.affected_cells == 1
    assert impact.affected_subscribers == 500


def test_an_incident_with_no_linked_cells_is_measured_as_zero(policies) -> None:
    service, parts = build(policies)

    impact = service.calculate(target())

    assert impact.affected_cells == 0
    assert impact.affected_subscribers == 0
    assert impact.enterprise_accounts == 0
    assert impact.affected_services == ()
    assert parts["subscribers"].asked == [], "no cells means no subscriber query"


# ============================================================ enterprise


def test_enterprise_accounts_come_from_the_affected_cells(policies) -> None:
    service, parts = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        exposure=EnterpriseExposure(
            accounts=3,
            subscribers=42,
            tiers=("gold", "platinum"),
            refs=("ENT-0101", "ENT-0202", "ENT-0303"),
        ),
    )

    impact = service.calculate(target(cell_ids=("CELL-A",)))

    assert impact.enterprise_accounts == 3
    assert impact.enterprise_subscribers == 42
    assert impact.sla_tiers == ("gold", "platinum")
    assert parts["subscribers"].asked == [("CELL-A",)]
    evidence = evidence_for(impact, "enterprise_accounts")
    assert evidence.inputs["sla_tiers"] == ["gold", "platinum"]
    assert "is_enterprise" in evidence.method


# =============================================================== services


@pytest.mark.parametrize(
    ("breached", "expected"),
    [
        ((KpiName.AVAILABILITY,), {"voice", "data", "sms", "signalling"}),
        ((KpiName.DL_THROUGHPUT, KpiName.PRB_UTILISATION), {"data"}),
        ((KpiName.DROP_RATE,), {"voice"}),
        ((KpiName.RRC_SETUP_SUCCESS,), {"signalling", "voice"}),
        ((), set()),
    ],
)
def test_affected_services_are_inferred_from_the_breached_kpis(
    policies,
    breached: tuple[KpiName, ...],
    expected: set[str],
) -> None:
    service, _ = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        breached=breached,
    )

    impact = service.calculate(target(cell_ids=("CELL-A",)))

    assert {str(item) for item in impact.affected_services} == expected
    evidence = evidence_for(impact, "affected_services")
    assert evidence.inputs["breached_kpis"] == [kpi.value for kpi in breached]


def test_affected_services_are_returned_in_a_stable_order(policies) -> None:
    service, _ = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        breached=(KpiName.SINR, KpiName.AVAILABILITY, KpiName.DL_THROUGHPUT),
    )

    impact = service.calculate(target(cell_ids=("CELL-A",)))

    assert impact.affected_services == tuple(AffectedService)


# =============================================================== sla risk


def test_sla_risk_is_inside_the_unit_interval_and_explained(policies) -> None:
    service, _ = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        exposure=EnterpriseExposure(accounts=4, subscribers=20, tiers=("platinum",)),
    )

    impact = service.calculate(target(cell_ids=("CELL-A",)))

    assert 0.0 <= impact.sla_risk <= 1.0
    evidence = evidence_for(impact, "sla_risk")
    assert evidence.inputs["enterprise_saturation"] == SLA_ENTERPRISE_SATURATION
    assert evidence.inputs["duration_saturation_minutes"] == SLA_DURATION_SATURATION_MINUTES
    recomposed = (
        0.45 * evidence.inputs["enterprise_term"]
        + 0.30 * evidence.inputs["duration_term"]
        + 0.25 * evidence.inputs["severity_term"]
    )
    assert impact.sla_risk == pytest.approx(recomposed, abs=1e-4)


def test_sla_risk_rises_with_enterprise_exposure(policies) -> None:
    def score(accounts: int) -> float:
        service, _ = build(
            policies,
            cells=[cell("CELL-A", "SITE-1", 1000)],
            exposure=EnterpriseExposure(
                accounts=accounts,
                subscribers=accounts * 5,
                tiers=("gold",) if accounts else (),
            ),
        )
        return service.calculate(target(cell_ids=("CELL-A",))).sla_risk

    assert score(0) < score(2) < score(8)


def test_sla_risk_rises_with_duration_and_severity(policies) -> None:
    def score(end_hour: int, severity: float) -> float:
        service, _ = build(policies, cells=[cell("CELL-A", "SITE-1", 1000)])
        return service.calculate(
            target(cell_ids=("CELL-A",), end_hour=end_hour, severity=severity),
        ).sla_risk

    assert score(7, 0.2) < score(10, 0.2)
    assert score(7, 0.2) < score(7, 0.95)


def test_sla_risk_saturates_rather_than_exceeding_one(policies) -> None:
    service, _ = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        exposure=EnterpriseExposure(accounts=400, subscribers=9000, tiers=("platinum",)),
    )

    impact = service.calculate(
        target(cell_ids=("CELL-A",), start_hour=0, end_hour=23, severity=1.0),
    )

    assert impact.sla_risk == 1.0


# ================================================== prioritisation handover


def test_the_payload_carries_every_prioritisation_input(policies) -> None:
    service, _ = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        exposure=EnterpriseExposure(accounts=2, subscribers=9, tiers=("silver",)),
        breached=(KpiName.AVAILABILITY,),
    )

    payload = service.calculate(target(cell_ids=("CELL-A",))).to_payload()

    for name in (
        "affected_subscribers",
        "enterprise_accounts",
        "duration_minutes",
        "sla_risk",
        "complaint_count",
        "is_silent_sufferer",
    ):
        assert name in payload, f"prioritisation needs {name}"
    assert payload["duration_minutes"] == 180.0


def test_a_degradation_nobody_reported_is_flagged_as_a_silent_sufferer(policies) -> None:
    service, _ = build(policies, cells=[cell("CELL-A", "SITE-1", 1000)])

    silent = service.calculate(target(cell_ids=("CELL-A",), complaints=0))
    noisy = service.calculate(target(cell_ids=("CELL-A",), complaints=30))

    assert silent.is_silent_sufferer
    assert not noisy.is_silent_sufferer


# ==================================================== events and audit trail


def test_impact_calculated_is_published_with_the_payload(policies) -> None:
    service, parts = build(policies, cells=[cell("CELL-A", "SITE-1", 1000)])

    impact = service.calculate(target(cell_ids=("CELL-A",)))
    events = parts["publisher"].of_type(EventType.IMPACT_CALCULATED)

    assert len(events) == 1
    assert events[0].aggregate_id == "INC-1"
    assert events[0].payload["affected_subscribers"] == 1000
    assert events[0].occurred_at == NOW


def test_every_number_in_the_result_has_evidence(policies) -> None:
    service, _ = build(
        policies,
        cells=[cell("CELL-A", "SITE-1", 1000)],
        breached=(KpiName.AVAILABILITY,),
    )

    impact = service.calculate(target(cell_ids=("CELL-A",)))
    metrics = {item.metric for item in impact.evidence}

    assert metrics == {
        "affected_cells",
        "affected_sites",
        "affected_subscribers",
        "enterprise_accounts",
        "affected_services",
        "duration_minutes",
        "sla_risk",
    }
    assert all(item.method for item in impact.evidence)


def test_calculating_records_one_decision_with_the_rule_version(
    policies,
) -> None:
    service, parts = build(policies, cells=[cell("CELL-A", "SITE-1", 1000)])

    service.calculate(target(cell_ids=("CELL-A",)))
    calls = parts["decisions"].calls

    assert len(calls) == 1
    assert calls[0]["action"] == "calculate_impact"
    assert calls[0]["entity_id"] == "INC-1"
    assert calls[0]["rule_version"] == policies.rule_version
    assert calls[0]["evidence"]
