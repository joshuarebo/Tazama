"""Correlation engine unit tests: one test per rule, driven by in-memory fakes.

The fakes satisfy the module's ports structurally, which is the point of declaring them
as Protocols: the engine under test here is byte-identical to the one that runs against
Oracle, and nothing in this file touches a database or a clock.

Every rule in the brief has a named test below, and the edge-case register (PRD §15) is
covered by the duplicate, late-event, maintenance, flapping, non-network and
silence-as-signal tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tazama.core.clock import FixedClock
from tazama.core.policy import CorrelationPolicy, Policies, load_policies
from tazama.domain.events import EventType
from tazama.domain.value_objects import (
    AlarmSeverity,
    ComplaintCategory,
    IncidentCause,
    KpiName,
    NodeKind,
    TimeWindow,
)
from tazama.infrastructure.eventbus.memory import InMemoryEventBus
from tazama.modules.correlation.models import (
    AlarmRecord,
    AlarmTransition,
    CellInfo,
    ComplaintCount,
    ComplaintObservation,
    CorrelationDecision,
    IncidentCandidate,
    IncidentRef,
    KpiBreach,
    KpiSample,
    MaintenanceCover,
    SuppressionReason,
    TopologyNodeInfo,
)
from tazama.modules.correlation.service import CorrelationService

NOW = datetime(2026, 3, 17, 0, 0, tzinfo=UTC)
DAY = datetime(2026, 3, 16, 0, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def window(start_hour: int, end_hour: int) -> TimeWindow:
    return TimeWindow(start=at(start_hour), end=at(end_hour))


# ===================================================================== fakes


class FakeKpiReader:
    def __init__(self, breaches: Sequence[KpiBreach] = ()) -> None:
        self._breaches = tuple(breaches)
        self.series_calls: list[tuple[str, KpiName]] = []

    def series(
        self,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[KpiSample, ...]:
        self.series_calls.append((cell_id, kpi))
        return ()

    def breaches(
        self,
        window: TimeWindow,
        granularity: str = "1h",
        *,
        cell_ids: Sequence[str] | None = None,
        min_severity: float = 0.0,
    ) -> tuple[KpiBreach, ...]:
        found = [
            breach
            for breach in self._breaches
            if breach.severity >= min_severity and breach.window.overlaps(window)
        ]
        if cell_ids is not None:
            wanted = set(cell_ids)
            found = [breach for breach in found if breach.cell_id in wanted]
        return tuple(found)

    def baseline(
        self,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> float | None:
        return None


class FakeAlarmReader:
    def __init__(
        self,
        alarms: Sequence[AlarmRecord] = (),
        transitions: dict[str, Sequence[datetime]] | None = None,
    ) -> None:
        self._alarms = tuple(alarms)
        self._transitions = transitions or {}

    def active(self, window: TimeWindow) -> tuple[AlarmRecord, ...]:
        return tuple(
            alarm
            for alarm in self._alarms
            if alarm.raised_at < window.end and (alarm.cleared_at or window.end) > window.start
        )

    def for_element(self, element_id: str, window: TimeWindow) -> tuple[AlarmRecord, ...]:
        return tuple(a for a in self.active(window) if a.node_id == element_id)

    def transitions(self, element_id: str, window: TimeWindow) -> tuple[AlarmTransition, ...]:
        return tuple(
            AlarmTransition(node_id=element_id, at=moment, kind="raise")
            for moment in self._transitions.get(element_id, ())
            if window.contains(moment)
        )


class FakeMaintenanceReader:
    def __init__(self, covers: Sequence[MaintenanceCover] = ()) -> None:
        self._covers = tuple(covers)

    def covering(self, element_id: str, window: TimeWindow) -> tuple[MaintenanceCover, ...]:
        return tuple(
            cover
            for cover in self._covers
            if cover.node_id == element_id and cover.window.overlaps(window)
        )


class FakeComplaintReader:
    def __init__(
        self,
        observations: Sequence[ComplaintObservation] = (),
        counts: Sequence[ComplaintCount] = (),
        rates: dict[tuple[str, datetime], float] | None = None,
    ) -> None:
        self._observations = tuple(observations)
        self._counts = tuple(counts)
        self._rates = rates or {}

    def in_window(
        self,
        window: TimeWindow,
        cell_ids: Sequence[str] | None = None,
    ) -> tuple[ComplaintObservation, ...]:
        wanted = set(cell_ids) if cell_ids is not None else None
        return tuple(
            row
            for row in self._observations
            if window.contains(row.event_time) and (wanted is None or row.cell_id in wanted)
        )

    def counts_by_cell(self, window: TimeWindow) -> tuple[ComplaintCount, ...]:
        return self._counts

    def baseline_rate(self, cell_id: str, window: TimeWindow) -> float:
        return self._rates.get((cell_id, window.start), 0.0)


class FakeCellDirectory:
    def __init__(self, cells: Sequence[CellInfo]) -> None:
        self._cells = {cell.cell_id: cell for cell in cells}

    def get(self, cell_id: str) -> CellInfo | None:
        return self._cells.get(cell_id)

    def by_region(self, region: str) -> tuple[CellInfo, ...]:
        return tuple(c for c in self._cells.values() if c.region == region)

    def with_geo(self, cell_ids: Sequence[str]) -> tuple[CellInfo, ...]:
        return tuple(self._cells[cell_id] for cell_id in cell_ids if cell_id in self._cells)


class FakeTopologyReader:
    def __init__(
        self,
        chains: dict[str, Sequence[TopologyNodeInfo]] | None = None,
        descendants: dict[str, Sequence[str]] | None = None,
    ) -> None:
        self._chains = chains or {}
        self._descendants = descendants or {}

    def upstream_of(self, node_id: str) -> tuple[TopologyNodeInfo, ...]:
        return tuple(self._chains.get(node_id, ()))

    def descendants_of(self, node_id: str, kind: NodeKind | None = None) -> tuple[str, ...]:
        return tuple(self._descendants.get(node_id, ()))

    def shared_upstream(self, cell_ids: Sequence[str]) -> TopologyNodeInfo | None:
        chains = [self.upstream_of(cell_id) for cell_id in cell_ids]
        if not chains or any(not chain for chain in chains):
            return None
        common = {node.node_id for node in chains[0]}
        for chain in chains[1:]:
            common &= {node.node_id for node in chain}
        return next((node for node in chains[0] if node.node_id in common), None)


@dataclass
class FakeIncidentRepository:
    """Stores candidates by their deterministic key, like the real aggregate does."""

    seeded: list[IncidentRef] = field(default_factory=list)
    opened: dict[str, IncidentCandidate] = field(default_factory=dict)
    reopened: list[str] = field(default_factory=list)

    def open_or_update(self, candidate: IncidentCandidate) -> IncidentRef:
        self.opened[candidate.key] = candidate
        return IncidentRef(
            incident_id=candidate.key,
            status="suppressed" if candidate.is_suppressed else "detected",
            primary_node_id=candidate.primary_node_id,
            primary_node_kind=str(candidate.primary_node_kind),
            cause=str(candidate.cause),
            window=candidate.window,
        )

    def find_candidates(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[IncidentRef, ...]:
        wanted = set(node_ids)
        return tuple(ref for ref in self.seeded if ref.primary_node_id in wanted)

    def reopen(self, incident_id: str, *, reason: str) -> IncidentRef:
        self.reopened.append(incident_id)
        existing = next(ref for ref in self.seeded if ref.incident_id == incident_id)
        return IncidentRef(
            incident_id=existing.incident_id,
            status="reopened",
            primary_node_id=existing.primary_node_id,
            primary_node_kind=existing.primary_node_kind,
            cause=existing.cause,
            window=existing.window,
        )


@dataclass
class FakeLinkRepository:
    links: set[tuple[str, str, str]] = field(default_factory=set)

    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        key = (incident_id, entity_type, entity_id)
        if key in self.links:
            return False
        self.links.add(key)
        return True

    def of_type(self, entity_type: str) -> set[str]:
        return {entity for _, kind, entity in self.links if kind == entity_type}


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
                "entity_type": entity_type,
                "entity_id": entity_id,
                "outcome": outcome,
                "inputs": inputs,
                "evidence": tuple(evidence),
                "confidence": confidence,
                "rule_version": rule_version,
            },
        )


# ================================================================== builders


def make_breach(
    cell_id: str,
    site_id: str,
    *,
    severity: float = 0.9,
    start_hour: int = 6,
    end_hour: int = 9,
    availability: float = 0.0,
    region: str = "Nairobi",
    kpis: Sequence[KpiName] = (KpiName.AVAILABILITY, KpiName.DROP_RATE, KpiName.LATENCY),
    worst: dict[str, float] | None = None,
) -> KpiBreach:
    return KpiBreach(
        cell_id=cell_id,
        site_id=site_id,
        region=region,
        window=window(start_hour, end_hour),
        buckets=end_hour - start_hour,
        breached_buckets=end_hour - start_hour,
        severity=severity,
        breached_kpis=tuple(kpis),
        worst=worst if worst is not None else {KpiName.AVAILABILITY.value: availability},
    )


def make_alarm(
    alarm_id: str,
    node_id: str,
    *,
    kind: NodeKind = NodeKind.CELL,
    alarm_type: str = "CELL_UNAVAILABLE",
    severity: AlarmSeverity = AlarmSeverity.CRITICAL,
    raised_hour: int = 6,
    cleared_hour: int | None = 9,
    cell_id: str | None = None,
    site_id: str | None = None,
) -> AlarmRecord:
    return AlarmRecord(
        alarm_id=alarm_id,
        node_id=node_id,
        node_kind=str(kind),
        alarm_type=alarm_type,
        severity=severity,
        raised_at=at(raised_hour),
        cleared_at=at(cleared_hour) if cleared_hour is not None else None,
        cell_id=cell_id,
        site_id=site_id,
    )


def make_complaint(
    complaint_id: str,
    cell_id: str,
    *,
    hour: int = 6,
    minute: int = 10,
    category: ComplaintCategory = ComplaintCategory.NO_SERVICE,
    dedupe_key: str = "",
    site_id: str = "SITE-1",
    region: str = "Nairobi",
    source_ts: datetime | None = None,
) -> ComplaintObservation:
    return ComplaintObservation(
        complaint_id=complaint_id,
        cell_id=cell_id,
        site_id=site_id,
        region=region,
        category=category,
        received_at=at(hour, minute),
        source_ts=source_ts,
        dedupe_key=dedupe_key or f"key-{complaint_id}",
    )


def make_cell(
    cell_id: str,
    site_id: str = "SITE-1",
    *,
    lat: float = -1.28,
    lon: float = 36.82,
    subscribers: int = 1000,
    region: str = "Nairobi",
) -> CellInfo:
    return CellInfo(
        cell_id=cell_id,
        site_id=site_id,
        region=region,
        lat=lat,
        lon=lon,
        subscribers_est=subscribers,
        tech="4G",
    )


@pytest.fixture
def policies(repo_root) -> Policies:
    return load_policies(repo_root / "config" / "policies.yaml")


def build_service(
    policies: Policies,
    *,
    kpis: FakeKpiReader | None = None,
    alarms: FakeAlarmReader | None = None,
    maintenance: FakeMaintenanceReader | None = None,
    complaints: FakeComplaintReader | None = None,
    cells: FakeCellDirectory | None = None,
    topology: FakeTopologyReader | None = None,
    incidents: FakeIncidentRepository | None = None,
    links: FakeLinkRepository | None = None,
) -> tuple[CorrelationService, dict[str, Any]]:
    parts: dict[str, Any] = {
        "kpis": kpis or FakeKpiReader(),
        "alarms": alarms or FakeAlarmReader(),
        "maintenance": maintenance or FakeMaintenanceReader(),
        "complaints": complaints or FakeComplaintReader(),
        "cells": cells or FakeCellDirectory([make_cell("CELL-A")]),
        "topology": topology or FakeTopologyReader(),
        "incidents": incidents or FakeIncidentRepository(),
        "links": links or FakeLinkRepository(),
        "publisher": InMemoryEventBus(),
        "decisions": FakeDecisionLog(),
    }
    service = CorrelationService(
        clock=FixedClock(NOW),
        policies=policies,
        **parts,
    )
    return service, parts


def with_policy(policies: Policies, **changes: Any) -> Policies:
    """A copy of the loaded policies with one correlation threshold changed."""
    current = policies.correlation
    fields = {
        "spatial_radius_km": current.spatial_radius_km,
        "time_window_minutes": current.time_window_minutes,
        "min_complaints_for_incident": current.min_complaints_for_incident,
        "min_complaints_per_1k_subs": current.min_complaints_per_1k_subs,
        "topology_shared_upstream": current.topology_shared_upstream,
        "alarm_storm_collapse_minutes": current.alarm_storm_collapse_minutes,
        "flapping_min_transitions": current.flapping_min_transitions,
        "maintenance_suppression": current.maintenance_suppression,
        "silent_sufferer_kpi_threshold": current.silent_sufferer_kpi_threshold,
    }
    fields.update(changes)
    return Policies(
        version=policies.version,
        updated=policies.updated,
        owner=policies.owner,
        approvals=policies.approvals,
        confidence=policies.confidence,
        correlation=CorrelationPolicy(**fields),
        prioritisation=policies.prioritisation,
        notification=policies.notification,
        verification=policies.verification,
        ai=policies.ai,
    )


# =============================================== 1. spatio-temporal clustering


def test_complaints_close_in_space_and_time_cluster_into_one_incident(policies) -> None:
    cells = FakeCellDirectory(
        [
            make_cell("CELL-A", "SITE-1", subscribers=1000),
            # ~0.6 km away: inside the 3 km policy radius.
            make_cell("CELL-B", "SITE-1", lat=-1.2855, subscribers=1000),
            # ~55 km away: outside it, and must not be counted.
            make_cell("CELL-FAR", "SITE-9", lat=-1.78, subscribers=1000),
        ],
    )
    observations = [
        make_complaint("CMP-1", "CELL-A", minute=10),
        make_complaint("CMP-2", "CELL-B", minute=15),
        make_complaint("CMP-3", "CELL-A", minute=20),
        make_complaint("CMP-FAR", "CELL-FAR", minute=12),
    ]
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1")]),
        complaints=FakeComplaintReader(
            observations,
            counts=[ComplaintCount(cell_id="CELL-A", total=3, distinct=3)],
        ),
        cells=cells,
    )

    outcome = service.correlate_complaint(observations[0])

    assert outcome.decision is CorrelationDecision.CREATED
    assert outcome.candidate is not None
    assert "CELL-FAR" not in outcome.candidate.cell_ids
    assert set(outcome.candidate.cell_ids) == {"CELL-A", "CELL-B"}


def test_exposure_normalisation_makes_a_busy_cell_need_more_complaints(policies) -> None:
    """Three complaints is an outage on a rural cell and noise on a Nairobi one."""
    busy = FakeCellDirectory([make_cell("CELL-A", "SITE-1", subscribers=20_000)])
    observations = [make_complaint(f"CMP-{i}", "CELL-A", minute=10 + i) for i in range(3)]
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1", severity=0.4)]),
        complaints=FakeComplaintReader(observations),
        cells=busy,
    )

    outcome = service.correlate_complaint(observations[0])

    assert outcome.decision is CorrelationDecision.NO_MATCH
    assert outcome.suppressed_reason == SuppressionReason.BELOW_THRESHOLD.value
    assert "per 1k subscribers" in outcome.note

    quiet = FakeCellDirectory([make_cell("CELL-A", "SITE-1", subscribers=900)])
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1", severity=0.4)]),
        complaints=FakeComplaintReader(
            observations,
            counts=[ComplaintCount(cell_id="CELL-A", total=3, distinct=3)],
        ),
        cells=quiet,
    )

    outcome = service.correlate_complaint(observations[0])

    assert outcome.decision is CorrelationDecision.CREATED


# ================================================== 2. topology-aware joining


def _link_topology() -> tuple[FakeTopologyReader, FakeCellDirectory]:
    link = TopologyNodeInfo(
        node_id="LINK-1",
        kind=NodeKind.TRANSMISSION_LINK,
        name="Nairobi Link 01",
        region="Nairobi",
    )
    site1 = TopologyNodeInfo(node_id="SITE-1", kind=NodeKind.SITE, region="Nairobi")
    site2 = TopologyNodeInfo(node_id="SITE-2", kind=NodeKind.SITE, region="Nairobi")
    topology = FakeTopologyReader(
        chains={
            "CELL-A": (site1, link),
            "CELL-B": (site2, link),
        },
        descendants={
            "SITE-1": ("CELL-A", "CELL-A2"),
            "SITE-2": ("CELL-B", "CELL-B2"),
            "LINK-1": ("CELL-A", "CELL-A2", "CELL-B", "CELL-B2"),
        },
    )
    cells = FakeCellDirectory(
        [
            make_cell("CELL-A", "SITE-1"),
            make_cell("CELL-B", "SITE-2", lat=-1.30),
        ],
    )
    return topology, cells


def test_clusters_sharing_an_upstream_element_become_one_incident(policies) -> None:
    topology, cells = _link_topology()
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader(
            [
                make_breach("CELL-A", "SITE-1", start_hour=10, end_hour=12),
                make_breach("CELL-B", "SITE-2", start_hour=10, end_hour=12),
            ],
        ),
        alarms=FakeAlarmReader(
            [
                make_alarm(
                    "ALM-LINK",
                    "LINK-1",
                    kind=NodeKind.TRANSMISSION_LINK,
                    alarm_type="LINK_DOWN",
                    raised_hour=10,
                    cleared_hour=12,
                ),
            ],
        ),
        cells=cells,
        topology=topology,
    )

    outcomes = service.detect_degradations(window(0, 24))
    created = [o for o in outcomes if o.decision is CorrelationDecision.CREATED]

    assert len(created) == 1, "two sites on one link must collapse into a single incident"
    candidate = created[0].candidate
    assert candidate is not None
    assert candidate.primary_node_id == "LINK-1"
    assert candidate.primary_node_kind is NodeKind.TRANSMISSION_LINK
    assert candidate.cause is IncidentCause.TRANSMISSION_LINK_FAILURE
    assert set(candidate.cell_ids) == {"CELL-A", "CELL-B"}
    assert any(item.kind == "topology" for item in candidate.evidence)


def test_sharing_a_link_without_evidence_about_the_link_does_not_merge(policies) -> None:
    """Two cells on one 400-cell link is a coincidence of the topology, not a cause."""
    link = TopologyNodeInfo(node_id="LINK-1", kind=NodeKind.TRANSMISSION_LINK)
    site1 = TopologyNodeInfo(node_id="SITE-1", kind=NodeKind.SITE)
    site2 = TopologyNodeInfo(node_id="SITE-2", kind=NodeKind.SITE)
    topology = FakeTopologyReader(
        chains={"CELL-A": (site1, link), "CELL-B": (site2, link)},
        descendants={
            "SITE-1": ("CELL-A", "CELL-A2"),
            "SITE-2": ("CELL-B", "CELL-B2"),
            "LINK-1": tuple(f"CELL-{i}" for i in range(20)) + ("CELL-A", "CELL-B"),
        },
    )
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader(
            [
                make_breach("CELL-A", "SITE-1", start_hour=10, end_hour=12),
                make_breach("CELL-B", "SITE-2", start_hour=10, end_hour=12),
            ],
        ),
        cells=FakeCellDirectory(
            [make_cell("CELL-A", "SITE-1"), make_cell("CELL-B", "SITE-2", lat=-1.30)],
        ),
        topology=topology,
    )

    outcomes = service.detect_degradations(window(0, 24))
    created = [o for o in outcomes if o.decision is CorrelationDecision.CREATED]

    assert len(created) == 2
    assert {o.candidate.primary_node_kind for o in created if o.candidate} == {NodeKind.CELL}


def test_a_site_whose_cells_all_degrade_is_rooted_at_the_site(policies) -> None:
    site = TopologyNodeInfo(node_id="SITE-1", kind=NodeKind.SITE, region="Nairobi")
    link = TopologyNodeInfo(node_id="LINK-1", kind=NodeKind.TRANSMISSION_LINK)
    topology = FakeTopologyReader(
        chains={"CELL-A": (site, link), "CELL-B": (site, link)},
        descendants={
            "SITE-1": ("CELL-A", "CELL-B"),
            "LINK-1": tuple(f"CELL-{i}" for i in range(24)),
        },
    )
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader(
            [make_breach("CELL-A", "SITE-1"), make_breach("CELL-B", "SITE-1")],
        ),
        cells=FakeCellDirectory(
            [make_cell("CELL-A", "SITE-1"), make_cell("CELL-B", "SITE-1")],
        ),
        topology=topology,
    )

    created = [
        o
        for o in service.detect_degradations(window(0, 24))
        if o.decision is CorrelationDecision.CREATED
    ]

    assert len(created) == 1
    assert created[0].candidate is not None
    assert created[0].candidate.primary_node_id == "SITE-1"
    assert created[0].candidate.primary_node_kind is NodeKind.SITE


# ===================================================== 3. maintenance suppression


def test_an_approved_maintenance_window_suppresses_the_candidate(policies) -> None:
    site = TopologyNodeInfo(node_id="SITE-1", kind=NodeKind.SITE)
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1", start_hour=20, end_hour=22)]),
        maintenance=FakeMaintenanceReader(
            [
                MaintenanceCover(
                    maintenance_id="MWD-1",
                    node_id="SITE-1",
                    node_kind="site",
                    starts_at=at(19, 45),
                    ends_at=at(22, 15),
                    approved_by="noc_change_board",
                    description="card replacement",
                ),
            ],
        ),
        topology=FakeTopologyReader(chains={"CELL-A": (site,)}),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert [o.decision for o in outcomes] == [CorrelationDecision.SUPPRESSED]
    assert outcomes[0].suppressed_reason == SuppressionReason.PLANNED_MAINTENANCE.value
    assert not any(o.raised_incident for o in outcomes)
    assert any(item.kind == "maintenance" for item in outcomes[0].evidence)


def test_an_unapproved_window_suppresses_nothing(policies) -> None:
    """Suppression needs an owner: an unapproved window is not a reason to stay quiet."""
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1", start_hour=20, end_hour=22)]),
        maintenance=FakeMaintenanceReader(
            [
                MaintenanceCover(
                    maintenance_id="MWD-2",
                    node_id="CELL-A",
                    node_kind="cell",
                    starts_at=at(19),
                    ends_at=at(23),
                    approved_by=None,
                ),
            ],
        ),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert [o.decision for o in outcomes] == [CorrelationDecision.CREATED]


# ======================================================= 4. alarm-storm collapse


def test_an_alarm_storm_on_related_elements_becomes_one_incident(policies) -> None:
    topology, cells = _link_topology()
    storm = [
        make_alarm(
            "ALM-LINK",
            "LINK-1",
            kind=NodeKind.TRANSMISSION_LINK,
            alarm_type="LINK_DOWN",
            raised_hour=10,
            cleared_hour=12,
        ),
        make_alarm("ALM-S1", "SITE-1", kind=NodeKind.SITE, raised_hour=10, cleared_hour=12),
        make_alarm("ALM-S2", "SITE-2", kind=NodeKind.SITE, raised_hour=10, cleared_hour=12),
        make_alarm("ALM-C1", "CELL-A", raised_hour=10, cleared_hour=12, cell_id="CELL-A"),
        make_alarm("ALM-C2", "CELL-B", raised_hour=10, cleared_hour=12, cell_id="CELL-B"),
    ]
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader(
            [
                make_breach("CELL-A", "SITE-1", start_hour=10, end_hour=12),
                make_breach("CELL-B", "SITE-2", start_hour=10, end_hour=12),
            ],
        ),
        alarms=FakeAlarmReader(storm),
        cells=cells,
        topology=topology,
    )

    outcomes = service.detect_degradations(window(0, 24))
    created = [o for o in outcomes if o.raised_incident]

    assert len(created) == 1, "five alarms must not become five incidents"
    candidate = created[0].candidate
    assert candidate is not None
    assert set(candidate.alarm_ids) == {a.alarm_id for a in storm}
    linked_alarms = parts["links"].of_type("alarm")
    assert linked_alarms == {a.alarm_id for a in storm}


# ========================================================== 5. flapping detection


def test_flapping_is_classified_as_flapping_and_raises_one_record(policies) -> None:
    transitions = {"CELL-F": tuple(at(13, i * 7) for i in range(8))}
    service, parts = build_service(
        policies,
        alarms=FakeAlarmReader(
            [
                make_alarm(
                    f"ALM-F{i}",
                    "CELL-F",
                    alarm_type="RRC_FAIL_HIGH",
                    severity=AlarmSeverity.MINOR,
                    raised_hour=13,
                    cleared_hour=14,
                    cell_id="CELL-F",
                )
                for i in range(8)
            ],
            transitions=transitions,
        ),
        cells=FakeCellDirectory([make_cell("CELL-F", "SITE-F")]),
    )

    outcomes = service.detect_degradations(window(12, 15))

    assert [o.decision for o in outcomes] == [CorrelationDecision.SUPPRESSED]
    assert outcomes[0].suppressed_reason == SuppressionReason.FLAPPING.value
    assert outcomes[0].candidate is not None
    assert outcomes[0].candidate.cause is IncidentCause.FLAPPING
    assert len(parts["incidents"].opened) == 1, "one record, not one per transition"
    assert not outcomes[0].raised_incident


def test_too_few_transitions_is_not_flapping(policies) -> None:
    service, _ = build_service(
        policies,
        alarms=FakeAlarmReader(
            [
                make_alarm(
                    "ALM-1",
                    "CELL-F",
                    raised_hour=13,
                    cleared_hour=14,
                    cell_id="CELL-F",
                ),
            ],
            transitions={"CELL-F": (at(13), at(13, 30))},
        ),
        cells=FakeCellDirectory([make_cell("CELL-F", "SITE-F")]),
    )

    outcomes = service.detect_degradations(window(12, 15))

    assert outcomes[0].suppressed_reason == SuppressionReason.UNCORROBORATED.value


# ====================================================== 6. false-alarm rejection


def test_an_alarm_with_healthy_kpis_and_no_complaints_raises_nothing(policies) -> None:
    service, parts = build_service(
        policies,
        alarms=FakeAlarmReader(
            [
                make_alarm(
                    "ALM-FALSE",
                    "CELL-X",
                    alarm_type="BBU_FAULT",
                    severity=AlarmSeverity.MAJOR,
                    raised_hour=2,
                    cleared_hour=3,
                    cell_id="CELL-X",
                ),
            ],
            transitions={"CELL-X": (at(2, 5),)},
        ),
        cells=FakeCellDirectory([make_cell("CELL-X", "SITE-X")]),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert [o.decision for o in outcomes] == [CorrelationDecision.REJECTED]
    assert outcomes[0].suppressed_reason == SuppressionReason.UNCORROBORATED.value
    assert parts["incidents"].opened == {}
    assert "nothing is wrong" in outcomes[0].note


# ====================================================== 7. non-network rejection


@pytest.mark.parametrize(
    "category",
    [ComplaintCategory.BILLING, ComplaintCategory.BUNDLE_CONFUSION, ComplaintCategory.SIM_SWAP],
)
def test_a_non_network_complaint_never_raises_a_network_incident(
    policies,
    category: ComplaintCategory,
) -> None:
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1")]),
    )

    outcomes = [
        service.correlate_complaint(
            make_complaint(f"CMP-{i}", "CELL-A", minute=i, category=category),
        )
        for i in range(200)
    ]

    assert {o.decision for o in outcomes} == {CorrelationDecision.REJECTED}
    assert {o.suppressed_reason for o in outcomes} == {SuppressionReason.NON_NETWORK.value}
    assert parts["incidents"].opened == {}


# ============================================================ 8. silent sufferers


def test_degraded_kpis_with_zero_complaints_still_raise_an_incident(policies) -> None:
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader(
            [
                make_breach(
                    "CELL-S",
                    "SITE-S",
                    severity=0.9,
                    start_hour=12,
                    end_hour=18,
                    worst={
                        KpiName.AVAILABILITY.value: 95.0,
                        KpiName.PRB_UTILISATION.value: 90.0,
                        KpiName.DL_THROUGHPUT.value: 1.1,
                    },
                ),
            ],
        ),
        cells=FakeCellDirectory([make_cell("CELL-S", "SITE-S")]),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert [o.decision for o in outcomes] == [CorrelationDecision.CREATED]
    assert outcomes[0].candidate is not None
    assert outcomes[0].candidate.complaint_count == 0
    assert outcomes[0].candidate.cause is IncidentCause.CONGESTION
    assert "silent sufferers" in outcomes[0].candidate.summary


def test_degradation_below_the_silent_sufferer_threshold_is_not_enough_alone(
    policies,
) -> None:
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader(
            [make_breach("CELL-S", "SITE-S", severity=0.4, worst={})],
        ),
        cells=FakeCellDirectory([make_cell("CELL-S", "SITE-S")]),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert [o.decision for o in outcomes] == [CorrelationDecision.REJECTED]
    assert outcomes[0].suppressed_reason == SuppressionReason.BELOW_THRESHOLD.value


# ========================================================== 9. silence as signal


def test_a_collapse_in_complaints_against_a_cells_own_baseline_is_evidence(
    policies,
) -> None:
    # The baseline window is the same length as the candidate's (04:00-08:00), taken
    # immediately before it: a rate is only comparable to another rate.
    reference_start = at(0)
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader(
            [make_breach("CELL-Q", "SITE-Q", start_hour=4, end_hour=8)],
        ),
        complaints=FakeComplaintReader(
            rates={
                # Busy before the outage, silent during it.
                ("CELL-Q", reference_start): 2.4,
                ("CELL-Q", at(4)): 0.0,
            },
        ),
        cells=FakeCellDirectory([make_cell("CELL-Q", "SITE-Q")]),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert outcomes[0].decision is CorrelationDecision.CREATED
    silence = [item for item in outcomes[0].evidence if item.kind == "baseline"]
    assert len(silence) == 1
    assert "went quiet with the cells" in silence[0].note


def test_a_cell_that_never_gets_complaints_produces_no_silence_evidence(policies) -> None:
    service, _ = build_service(
        policies,
        kpis=FakeKpiReader([make_breach("CELL-Q", "SITE-Q", start_hour=4, end_hour=8)]),
        complaints=FakeComplaintReader(),
        cells=FakeCellDirectory([make_cell("CELL-Q", "SITE-Q")]),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert not [item for item in outcomes[0].evidence if item.kind == "baseline"]


# ============================================================ 10. duplicate storms


def test_complaints_sharing_a_dedupe_key_count_once(policies) -> None:
    existing = IncidentRef(
        incident_id="INC-EXISTING",
        status="detected",
        primary_node_id="CELL-A",
        primary_node_kind="cell",
        cause="cell_outage",
        window=window(15, 18),
    )
    incidents = FakeIncidentRepository(seeded=[existing])
    links = FakeLinkRepository()
    service, parts = build_service(policies, incidents=incidents, links=links)

    storm = [
        make_complaint(
            f"CMP-DUP-{i}",
            "CELL-A",
            hour=16,
            minute=20,
            category=ComplaintCategory.SLOW_DATA,
            dedupe_key="one-angry-subscriber",
        )
        for i in range(9)
    ]
    outcomes = [service.correlate_complaint(row) for row in storm]

    assert outcomes[0].decision is CorrelationDecision.ATTACHED
    assert outcomes[0].suppressed_reason is None
    assert all(o.suppressed_reason == SuppressionReason.DUPLICATE.value for o in outcomes[1:])
    assert links.of_type("complaint") == {"CMP-DUP-0"}, "nine submissions, one complaint link"
    assert links.of_type("complaint_dedupe") == {"one-angry-subscriber"}


# ================================================= 11. late / out-of-order events


def test_a_late_complaint_attaches_by_event_time_not_ingestion_time(policies) -> None:
    existing = IncidentRef(
        incident_id="INC-EARLY",
        status="detected",
        primary_node_id="CELL-A",
        primary_node_kind="cell",
        cause="cell_outage",
        window=window(6, 9),
    )
    service, parts = build_service(
        policies,
        incidents=FakeIncidentRepository(seeded=[existing]),
    )

    late = make_complaint(
        "CMP-LATE",
        "CELL-A",
        hour=21,
        minute=30,
        source_ts=at(7, 15),
    )
    outcome = service.correlate_complaint(late)

    assert outcome.decision is CorrelationDecision.ATTACHED
    assert outcome.incident_id == "INC-EARLY"
    assert not outcome.reopen_requested
    recorded = parts["decisions"].calls[-1]["inputs"]
    assert recorded["event_time"] == at(7, 15).isoformat()
    assert recorded["received_at"] == at(21, 30).isoformat()
    assert recorded["lag_seconds"] > 14 * 3600


def test_a_late_complaint_for_a_closed_incident_asks_for_a_reopen(policies) -> None:
    closed = IncidentRef(
        incident_id="INC-CLOSED",
        status="closed",
        primary_node_id="CELL-A",
        primary_node_kind="cell",
        cause="cell_outage",
        window=window(6, 9),
    )
    incidents = FakeIncidentRepository(seeded=[closed])
    service, parts = build_service(policies, incidents=incidents)

    outcome = service.correlate_complaint(
        make_complaint("CMP-LATE", "CELL-A", hour=21, source_ts=at(8, 30)),
    )

    assert outcome.decision is CorrelationDecision.REOPENED
    assert outcome.reopen_requested
    assert outcome.incident_id == "INC-CLOSED"
    assert incidents.reopened == ["INC-CLOSED"]
    assert incidents.opened == {}, "a reopen must not create a second incident"


def test_a_complaint_outside_every_incident_window_does_not_attach(policies) -> None:
    far = IncidentRef(
        incident_id="INC-EARLY",
        status="detected",
        primary_node_id="CELL-A",
        primary_node_kind="cell",
        cause="cell_outage",
        window=window(1, 2),
    )
    service, parts = build_service(policies, incidents=FakeIncidentRepository(seeded=[far]))

    outcome = service.correlate_complaint(make_complaint("CMP-1", "CELL-A", hour=20))

    assert outcome.decision is CorrelationDecision.NO_MATCH
    assert outcome.suppressed_reason == SuppressionReason.UNCORROBORATED.value


# ================================================================== plumbing


def test_every_outcome_records_exactly_one_decision_with_the_rule_version(
    policies,
) -> None:
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader(
            [make_breach("CELL-A", "SITE-1"), make_breach("CELL-B", "SITE-2")],
        ),
        cells=FakeCellDirectory(
            [make_cell("CELL-A", "SITE-1"), make_cell("CELL-B", "SITE-2")],
        ),
    )

    outcomes = service.detect_degradations(window(0, 24))
    calls = parts["decisions"].calls

    assert len(calls) == len(outcomes)
    assert {call["rule_version"] for call in calls} == {policies.rule_version}
    assert all(call["inputs"]["policy"]["spatial_radius_km"] == 3.0 for call in calls)


def test_degradation_detected_is_published_for_every_breached_cell(policies) -> None:
    service, parts = build_service(
        policies,
        kpis=FakeKpiReader(
            [make_breach("CELL-A", "SITE-1"), make_breach("CELL-B", "SITE-2")],
        ),
        cells=FakeCellDirectory(
            [make_cell("CELL-A", "SITE-1"), make_cell("CELL-B", "SITE-2")],
        ),
    )

    service.detect_degradations(window(0, 24))
    bus: InMemoryEventBus = parts["publisher"]
    events = bus.of_type(EventType.DEGRADATION_DETECTED)

    assert {event.aggregate_id for event in events} == {"CELL-A", "CELL-B"}
    assert events[0].payload["severity"] == pytest.approx(0.9)


def test_candidate_keys_are_deterministic_across_runs(policies) -> None:
    def run() -> set[str]:
        service, parts = build_service(
            policies,
            kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1")]),
            cells=FakeCellDirectory([make_cell("CELL-A", "SITE-1")]),
        )
        service.detect_degradations(window(0, 24))
        return set(parts["incidents"].opened)

    assert run() == run()


def test_topology_merging_can_be_switched_off_by_policy(policies) -> None:
    topology, cells = _link_topology()
    service, _ = build_service(
        with_policy(policies, topology_shared_upstream=False),
        kpis=FakeKpiReader(
            [
                make_breach("CELL-A", "SITE-1", start_hour=10, end_hour=12),
                make_breach("CELL-B", "SITE-2", start_hour=10, end_hour=12),
            ],
        ),
        alarms=FakeAlarmReader(
            [
                make_alarm(
                    "ALM-LINK",
                    "LINK-1",
                    kind=NodeKind.TRANSMISSION_LINK,
                    alarm_type="LINK_DOWN",
                    raised_hour=10,
                    cleared_hour=12,
                ),
            ],
        ),
        cells=cells,
        topology=topology,
    )

    created = [
        o
        for o in service.detect_degradations(window(0, 24))
        if o.decision is CorrelationDecision.CREATED
    ]

    assert len(created) == 2


def test_maintenance_suppression_can_be_switched_off_by_policy(policies) -> None:
    site = TopologyNodeInfo(node_id="SITE-1", kind=NodeKind.SITE)
    service, _ = build_service(
        with_policy(policies, maintenance_suppression=False),
        kpis=FakeKpiReader([make_breach("CELL-A", "SITE-1", start_hour=20, end_hour=22)]),
        maintenance=FakeMaintenanceReader(
            [
                MaintenanceCover(
                    maintenance_id="MWD-1",
                    node_id="SITE-1",
                    node_kind="site",
                    starts_at=at(19, 45),
                    ends_at=at(22, 15),
                    approved_by="noc_change_board",
                ),
            ],
        ),
        topology=FakeTopologyReader(chains={"CELL-A": (site,)}),
    )

    outcomes = service.detect_degradations(window(0, 24))

    assert [o.decision for o in outcomes] == [CorrelationDecision.CREATED]
