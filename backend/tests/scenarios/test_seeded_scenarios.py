"""The Phase 2 gate: the detection engine scored against the eleven seeded scenarios.

How this suite works, and why:

* It seeds a **small deterministic dataset** (`seed=20251020, days=2, fine_days=1`) into a
  temporary SQLite file. Every labelled scenario lives in the final 24 hours, so two days
  of history contains all eleven and the gate stays fast.
* It then runs the real pipeline over the scenario day with the **real adapters**:
  `detect_degradations` first (network-first), then `correlate_complaint` for every located
  complaint in event-time order, then impact for every live incident.
* Only then does it open `ground_truth`. The detectors never see it: a grep assertion in
  `test_no_module_reads_ground_truth` proves no file under `modules/` so much as mentions
  the table, so the pipeline genuinely rediscovers what the seed built.

Scoring vocabulary used below:
  * a **detection** is a live (non-suppressed) incident;
  * a detection is a **true positive** when it overlaps a labelled scenario that expects an
    incident, in both element and time;
  * **precision** is over detections, **recall** is over labelled positive scenarios — a
    scenario that produces three incidents on three different cells is one recall hit and
    three precision hits, which is the honest reading of "did we find it, and did we cry
    wolf".
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tazama.core.clock import FixedClock
from tazama.core.policy import Policies, load_policies
from tazama.domain.value_objects import IncidentCause, IncidentStatus, TimeWindow
from tazama.infrastructure.db import models as m
from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.incident_repo import (
    CorrelationIncidentBridge,
    SqlIncidentLinkStore,
    SqlIncidentStore,
)
from tazama.infrastructure.db.kpi_repo import (
    SqlAlarmReader,
    SqlCellDirectory,
    SqlCellStats,
    SqlComplaintReader,
    SqlKpiFacts,
    SqlKpiReader,
    SqlMaintenanceReader,
    SqlSubscriberStats,
    SqlTopologyReader,
)
from tazama.infrastructure.db.seed import (
    ANCHOR,
    SCENARIO_DAY,
    SCN_CONGESTION,
    SCN_DUPLICATES,
    SCN_FALSE_ALARM,
    SCN_FLAPPING,
    SCN_LATE_EVENTS,
    SCN_LINK_FAILURE,
    SCN_MAINTENANCE,
    SCN_NON_NETWORK,
    SCN_SILENCED,
    SCN_SILENT,
    SCN_SINGLE_CELL,
    SeedSummary,
    run_seed,
)
from tazama.infrastructure.eventbus.memory import InMemoryEventBus
from tazama.modules.correlation.models import CorrelationDecision, CorrelationOutcome
from tazama.modules.correlation.service import CorrelationService
from tazama.modules.impact.models import ImpactTarget
from tazama.modules.impact.service import ImpactService
from tazama.modules.incidents.models import Incident, VerificationSignal
from tazama.modules.incidents.service import IncidentService

pytestmark = [pytest.mark.scenario]

SEED = 20_251_020

#: The window the engine is run over: the day every labelled scenario lives on.
SCENARIO_WINDOW = TimeWindow(start=SCENARIO_DAY, end=ANCHOR)

#: Scenarios whose label says an incident must be raised.
POSITIVE_SCENARIOS = (
    SCN_SINGLE_CELL,
    SCN_LINK_FAILURE,
    SCN_CONGESTION,
    SCN_SILENCED,
    SCN_SILENT,
)

#: Scenarios whose label says no customer-facing incident may be raised.
NEGATIVE_SCENARIOS = (
    SCN_FLAPPING,
    SCN_FALSE_ALARM,
    SCN_MAINTENANCE,
    SCN_NON_NETWORK,
)

#: Slack either side of a labelled window when matching a detection to a scenario. KPI
#: buckets are hourly, so a degradation that starts at 06:00 can only be seen from the
#: 06:00 bucket onward; an hour of tolerance is the grid, not a fudge factor.
MATCH_TOLERANCE = timedelta(hours=1)

PRECISION_FLOOR = 0.85
RECALL_FLOOR = 0.80

_CACHE: dict[str, PipelineRun] = {}


# ============================================================ test doubles


@dataclass
class RecordingDecisionLog:
    """The real audit sink is another module's; the gate only needs the decisions."""

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

    def for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["entity_id"] == entity_id]


# =============================================================== the run


@dataclass
class PipelineRun:
    """Everything one end-to-end run produced, so the tests can score it."""

    engine: Engine
    summary: SeedSummary
    policies: Policies
    outcomes: tuple[CorrelationOutcome, ...]
    complaint_outcomes: tuple[CorrelationOutcome, ...]
    incidents: tuple[Incident, ...]
    links: dict[str, tuple[tuple[str, str], ...]]
    impacts: dict[str, Any]
    decisions: RecordingDecisionLog
    events: tuple[str, ...]
    second_run_incident_ids: frozenset[str] = frozenset()
    second_run_versions: dict[str, int] = field(default_factory=dict)

    @property
    def detections(self) -> tuple[Incident, ...]:
        """Live incidents: what a NOC engineer would actually be paged about."""
        return tuple(
            incident
            for incident in self.incidents
            if incident.status is not IncidentStatus.SUPPRESSED
        )

    @property
    def suppressed(self) -> tuple[Incident, ...]:
        return tuple(
            incident for incident in self.incidents if incident.status is IncidentStatus.SUPPRESSED
        )

    def cells_of(self, incident_id: str) -> set[str]:
        return {entity for kind, entity in self.links.get(incident_id, ()) if kind == "cell"}

    def entities_of(self, incident_id: str) -> set[str]:
        return {entity for _, entity in self.links.get(incident_id, ())}

    def complaints_of(self, incident_id: str) -> set[str]:
        return {entity for kind, entity in self.links.get(incident_id, ()) if kind == "complaint"}


def _build(
    session: Session, policies: Policies
) -> tuple[
    CorrelationService,
    IncidentService,
    ImpactService,
    SqlIncidentStore,
    SqlIncidentLinkStore,
    InMemoryEventBus,
    RecordingDecisionLog,
]:
    """Wire the real adapters, exactly as the composition root will."""
    clock = FixedClock(ANCHOR)
    bus = InMemoryEventBus()
    decisions = RecordingDecisionLog()
    store = SqlIncidentStore(session)
    link_store = SqlIncidentLinkStore(session, clock=clock)
    incidents = IncidentService(
        store=store,
        links=link_store,
        publisher=bus,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )
    bridge = CorrelationIncidentBridge(incidents)
    correlation = CorrelationService(
        kpis=SqlKpiReader(session),
        alarms=SqlAlarmReader(session),
        maintenance=SqlMaintenanceReader(session),
        complaints=SqlComplaintReader(session),
        cells=SqlCellDirectory(session),
        topology=SqlTopologyReader(session),
        incidents=bridge,
        links=bridge,
        publisher=bus,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )
    impact = ImpactService(
        incident_cells=link_store,
        cells=SqlCellStats(session),
        subscribers=SqlSubscriberStats(session),
        kpis=SqlKpiFacts(session),
        publisher=bus,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )
    return correlation, incidents, impact, store, link_store, bus, decisions


def _run_detection(
    session: Session,
    policies: Policies,
) -> tuple[
    tuple[CorrelationOutcome, ...],
    tuple[CorrelationOutcome, ...],
    SqlIncidentStore,
    SqlIncidentLinkStore,
    InMemoryEventBus,
    RecordingDecisionLog,
    ImpactService,
]:
    correlation, _, impact, store, link_store, bus, decisions = _build(session, policies)

    # 1. Network-first. This alone must find the silent sufferers and the silenced site.
    network = correlation.detect_degradations(SCENARIO_WINDOW)

    # 2. Customer-first, in event-time order. Unlocated complaints belong to site
    #    identification, not to correlation, so they are skipped here by the reader.
    reader = SqlComplaintReader(session)
    complaint_outcomes = tuple(
        correlation.correlate_complaint(observation)
        for observation in reader.in_window(SCENARIO_WINDOW)
    )
    session.flush()
    return network, complaint_outcomes, store, link_store, bus, decisions, impact


@pytest.fixture
def run(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> PipelineRun:
    """One seed, one pipeline run, shared by every test in this module.

    Function-scoped because conftest's environment fixture is, but memoised: seeding and
    running twice per test would make the gate unusable.
    """
    if "run" in _CACHE:
        return _CACHE["run"]

    from tazama.core.settings import Settings, get_settings

    path = tmp_path_factory.mktemp("phase2") / "scenarios.db"
    settings: Settings = get_settings().model_copy(update={"SQLITE_PATH": str(path)})
    engine = get_engine(settings)
    Base.metadata.create_all(engine)
    summary = run_seed(settings=settings, seed=SEED, days=2, fine_days=1, reset=True)
    policies = load_policies(repo_root / "config" / "policies.yaml")

    factory = session_factory(engine)
    with factory() as session:
        network, complaints, store, link_store, bus, decisions, impact = _run_detection(
            session,
            policies,
        )
        incidents = store.open_in_window(SCENARIO_WINDOW)
        links = {
            incident.id: tuple(
                (row.entity_type, row.entity_id) for row in link_store.for_incident(incident.id)
            )
            for incident in incidents
        }
        impacts: dict[str, Any] = {}
        for incident in incidents:
            if incident.status is IncidentStatus.SUPPRESSED:
                continue
            impacts[incident.id] = impact.calculate(
                ImpactTarget(
                    incident_id=incident.id,
                    region=incident.region,
                    window=incident.window,
                    cause=str(incident.cause),
                    primary_node_id=incident.primary_node_id,
                    primary_node_kind=incident.primary_node_kind,
                    severity=incident.cause_confidence or 0.0,
                    complaint_count=incident.complaint_count,
                ),
            )
        session.commit()
        events = tuple(str(event.event_type) for event in bus.events)

    # 3. Replay the whole thing. Nothing may change: same ids, same versions, no dupes.
    with factory() as session:
        _run_detection(session, policies)
        session.commit()
        replayed = SqlIncidentStore(session).open_in_window(SCENARIO_WINDOW)

    result = PipelineRun(
        engine=engine,
        summary=summary,
        policies=policies,
        outcomes=network,
        complaint_outcomes=complaints,
        incidents=incidents,
        links=links,
        impacts=impacts,
        decisions=decisions,
        events=events,
        second_run_incident_ids=frozenset(row.id for row in replayed),
        second_run_versions={row.id: row.version for row in replayed},
    )
    _CACHE["run"] = result
    return result


@dataclass(frozen=True)
class Fact:
    """A labelled scenario, read from `ground_truth` by the scorer only."""

    scenario_id: str
    kind: str
    label: str
    entity_type: str
    entity_id: str
    region: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    detail: dict[str, Any]

    @property
    def cells(self) -> set[str]:
        return {str(cell) for cell in self.detail.get("cells", ())}

    @property
    def sites(self) -> set[str]:
        return {str(site) for site in self.detail.get("sites", ())}

    @property
    def elements(self) -> set[str]:
        found = self.cells | self.sites | {self.entity_id}
        link_id = self.detail.get("link_id")
        if link_id:
            found.add(str(link_id))
        return found

    @property
    def window(self) -> TimeWindow | None:
        if self.starts_at is None or self.ends_at is None:
            return None
        return TimeWindow(start=self.starts_at, end=self.ends_at)


@pytest.fixture
def facts(run: PipelineRun) -> dict[str, Fact]:
    """The labels. Read here and nowhere else — the detectors never see this table."""
    with session_factory(run.engine)() as session:
        rows = session.execute(
            select(
                m.GroundTruth.scenario_id,
                m.GroundTruth.scenario_kind,
                m.GroundTruth.label,
                m.GroundTruth.entity_type,
                m.GroundTruth.entity_id,
                m.GroundTruth.region,
                m.GroundTruth.starts_at,
                m.GroundTruth.ends_at,
                m.GroundTruth.detail,
            ).where(
                m.GroundTruth.scenario_id.in_(
                    POSITIVE_SCENARIOS + NEGATIVE_SCENARIOS + (SCN_DUPLICATES, SCN_LATE_EVENTS)
                )
            ),
        ).all()
    return {
        str(row[0]): Fact(
            scenario_id=str(row[0]),
            kind=str(row[1]),
            label=str(row[2]),
            entity_type=str(row[3]),
            entity_id=str(row[4]),
            region=row[5],
            starts_at=row[6],
            ends_at=row[7],
            detail=dict(row[8] or {}),
        )
        for row in rows
    }


# ================================================================= matching


def _matches(run: PipelineRun, incident: Incident, fact: Fact) -> bool:
    """Does this incident describe this scenario, in element and in time?"""
    fact_window = fact.window
    if fact_window is not None:
        tolerant = fact_window.expand(MATCH_TOLERANCE)
        if not incident.window.overlaps(tolerant):
            return False
    elements = fact.elements
    if incident.primary_node_id in elements:
        return True
    return bool(run.entities_of(incident.id) & elements)


def _score(run: PipelineRun, facts: dict[str, Fact]) -> dict[str, Any]:
    positives = [facts[name] for name in POSITIVE_SCENARIOS]
    detections = run.detections
    matched: dict[str, list[str]] = {fact.scenario_id: [] for fact in positives}
    false_positives: list[Incident] = []

    for incident in detections:
        hits = [fact.scenario_id for fact in positives if _matches(run, incident, fact)]
        if hits:
            for scenario_id in hits:
                matched[scenario_id].append(incident.id)
        else:
            false_positives.append(incident)

    true_positives = len(detections) - len(false_positives)
    precision = true_positives / len(detections) if detections else 0.0
    found = [name for name, ids in matched.items() if ids]
    recall = len(found) / len(positives) if positives else 0.0
    return {
        "detections": detections,
        "matched": matched,
        "false_positives": false_positives,
        "true_positives": true_positives,
        "precision": precision,
        "recall": recall,
        "missed": [name for name, ids in matched.items() if not ids],
        "positives": positives,
    }


def _confusion(run: PipelineRun, facts: dict[str, Fact], score: dict[str, Any]) -> str:
    lines = [
        "",
        "confusion matrix (detections = live, non-suppressed incidents)",
        f"  labelled positive scenarios : {len(score['positives'])}",
        f"  detected scenarios          : {len(score['positives']) - len(score['missed'])}",
        f"  missed scenarios (FN)       : {len(score['missed'])} {score['missed']}",
        f"  detections                  : {len(score['detections'])}",
        f"  true positives              : {score['true_positives']}",
        f"  false positives             : {len(score['false_positives'])}",
        f"  precision                   : {score['precision']:.4f} (floor {PRECISION_FLOOR})",
        f"  recall                      : {score['recall']:.4f} (floor {RECALL_FLOOR})",
        "",
        "  per scenario:",
    ]
    for name in POSITIVE_SCENARIOS:
        ids = score["matched"][name]
        lines.append(f"    {name:<34} expect incident  detected {len(ids):>3}")
    for name in NEGATIVE_SCENARIOS:
        fact = facts[name]
        live = [i.id for i in run.detections if _matches(run, i, fact)]
        lines.append(
            f"    {name:<34} expect none      detected {len(live):>3} {live[:3]}",
        )
    if score["false_positives"]:
        lines += ["", "  false positives:"]
        for incident in score["false_positives"][:15]:
            lines.append(
                f"    {incident.id} {incident.cause} {incident.primary_node_kind} "
                f"{incident.primary_node_id} "
                f"{incident.window.start.isoformat()} -> {incident.window.end.isoformat()} "
                f"cells={incident.affected_cells} complaints={incident.complaint_count}",
            )
    lines += ["", f"  suppressed records: {len(run.suppressed)}"]
    for incident in run.suppressed[:10]:
        lines.append(
            f"    {incident.id} {incident.cause} reason={incident.suppressed_reason} "
            f"node={incident.primary_node_id}",
        )
    return "\n".join(lines)


# =========================================================== per-scenario tests


def test_scenario_01_single_cell_outage_raises_exactly_one_incident(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_SINGLE_CELL]
    cell_id = fact.entity_id
    found = [i for i in run.detections if _matches(run, i, fact)]

    assert len(found) == 1, f"expected one incident on {cell_id}, got {[i.id for i in found]}"
    incident = found[0]
    assert incident.cause is IncidentCause.CELL_OUTAGE
    assert incident.primary_node_id == cell_id
    assert incident.primary_node_kind == "cell"
    assert cell_id in run.cells_of(incident.id)
    assert incident.complaint_count > 0, "the complaint spike must corroborate the outage"
    assert run.complaints_of(incident.id), "the complaints must be linked as evidence"


def test_scenario_02_link_failure_is_one_incident_rooted_at_the_link(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_LINK_FAILURE]
    link_id = str(fact.detail["link_id"])
    expected_cells = fact.cells
    found = [i for i in run.detections if _matches(run, i, fact)]

    assert len(found) == 1, (
        f"a transmission-link failure across {len(fact.sites)} sites must collapse into "
        f"one incident, got {[(i.id, i.primary_node_id) for i in found]}"
    )
    incident = found[0]
    assert incident.primary_node_id == link_id
    assert incident.primary_node_kind == "transmission_link"
    assert incident.cause is IncidentCause.TRANSMISSION_LINK_FAILURE
    assert incident.affected_sites > 1, "the incident must span many sites"
    linked_cells = run.cells_of(incident.id)
    assert linked_cells >= expected_cells, (
        f"missing {len(expected_cells - linked_cells)} of the link's cells"
    )


def test_scenario_03_evening_congestion_is_detected_and_classified(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_CONGESTION]
    found = [i for i in run.detections if _matches(run, i, fact)]

    assert found, "evening congestion must raise at least one incident"
    assert {i.cause for i in found} == {IncidentCause.CONGESTION}
    covered = {cell for i in found for cell in run.cells_of(i.id)}
    assert covered >= fact.cells, f"missed congested cells: {sorted(fact.cells - covered)}"


def test_scenario_04_flapping_is_classified_as_flapping_and_raises_no_incident(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_FLAPPING]
    cell_id = fact.entity_id

    live = [i for i in run.detections if _matches(run, i, fact)]
    assert live == [], f"flapping must not page anybody: {[i.id for i in live]}"

    flapping = [
        i
        for i in run.suppressed
        if i.primary_node_id == cell_id and i.cause is IncidentCause.FLAPPING
    ]
    assert len(flapping) == 1, (
        f"expected exactly one flapping record for {cell_id} "
        f"({fact.detail['cycles']} raise/clear cycles), got {len(flapping)}"
    )
    assert flapping[0].suppressed_reason == "flapping"


def test_scenario_05_false_alarm_raises_nothing(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_FALSE_ALARM]
    cell_id = fact.entity_id

    assert [i for i in run.detections if _matches(run, i, fact)] == []
    assert [i for i in run.incidents if i.primary_node_id == cell_id] == [], (
        "an alarm with healthy KPIs and no complaints must not become an incident at all"
    )
    rejected = [
        call
        for call in run.decisions.calls
        if call["entity_id"] == cell_id and call["outcome"] == "rejected"
    ]
    assert rejected, "the rejection must still be recorded as a decision"


def test_scenario_06_planned_maintenance_produces_zero_incidents(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_MAINTENANCE]

    live = [i for i in run.detections if _matches(run, i, fact)]
    assert live == [], (
        f"planned maintenance must be suppressed, not paged: "
        f"{[(i.id, i.primary_node_id, i.cause) for i in live]}"
    )
    suppressed = [i for i in run.suppressed if _matches(run, i, fact)]
    assert suppressed, "the suppression must be recorded, never silent"
    assert all(i.suppressed_reason == "planned_maintenance" for i in suppressed)
    assert all(i.cause is IncidentCause.PLANNED_MAINTENANCE for i in suppressed)


def test_scenario_07_non_network_surge_raises_no_network_incident(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_NON_NETWORK]
    assert fact.window is not None
    surge = fact.window.expand(MATCH_TOLERANCE)
    region = fact.region

    offenders = [i for i in run.detections if i.region == region and i.window.overlaps(surge)]
    assert offenders == [], (
        f"160 billing complaints must not become a network incident: "
        f"{[(i.id, i.cause, i.primary_node_id) for i in offenders]}"
    )
    rejected = [
        call
        for call in run.decisions.calls
        if call["action"] == "correlate_complaint"
        and call["inputs"].get("suppressed_reason") == "non_network"
    ]
    assert len(rejected) >= 100, (
        f"the non-network rejections must be recorded; found {len(rejected)}"
    )


def test_scenario_08_an_outage_that_silences_the_channels_is_still_detected(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_SILENCED]
    found = [i for i in run.detections if _matches(run, i, fact)]

    assert found, "silence plus collapsed KPIs is an outage, not health"
    covered = {cell for i in found for cell in run.cells_of(i.id)}
    assert covered >= fact.cells
    assert {i.cause for i in found} == {IncidentCause.CELL_OUTAGE}

    silence_evidence = [
        item
        for incident in found
        for call in run.decisions.for_entity(incident.id)
        for item in call["evidence"]
        if getattr(item, "kind", "") == "baseline"
    ]
    assert silence_evidence, (
        "the complaint-rate collapse must be recorded as evidence: it is the signal"
    )
    assert "went quiet" in silence_evidence[0].note


def test_scenario_09_silent_sufferers_are_detected_without_a_single_complaint(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_SILENT]
    found = [i for i in run.detections if _matches(run, i, fact)]

    assert found, "degraded KPIs with zero complaints must still raise an incident"
    covered = {cell for i in found for cell in run.cells_of(i.id)}
    assert covered >= fact.cells, f"missed silent cells: {sorted(fact.cells - covered)}"
    assert all(i.complaint_count == 0 for i in found), (
        "these cells have no complaints by construction; a non-zero count means the "
        "detector is counting somebody else's"
    )


def test_scenario_10_a_duplicate_storm_counts_once(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_DUPLICATES]
    expected_duplicates = int(fact.detail["duplicate_count"])
    cell_id = next(iter(fact.cells))

    with session_factory(run.engine)() as session:
        storm = session.execute(
            select(m.Complaint.id, m.Complaint.dedupe_key)
            .where(
                m.Complaint.cell_id == cell_id,
                m.Complaint.received_at >= fact.starts_at,
                m.Complaint.received_at < fact.ends_at,
            )
            .order_by(m.Complaint.id),
        ).all()
    keys = {str(row[1]) for row in storm}
    storm_ids = {str(row[0]) for row in storm}

    assert len(storm_ids) == expected_duplicates
    assert len(keys) == 1, "the seed gives all nine submissions one dedupe key"

    linked = {
        entity
        for incident_id in run.links
        for kind, entity in run.links[incident_id]
        if kind == "complaint" and entity in storm_ids
    }
    assert len(linked) == 1, (
        f"{expected_duplicates} duplicate submissions must contribute one complaint link, "
        f"got {len(linked)}"
    )
    deduped = [o for o in run.complaint_outcomes if o.suppressed_reason == "duplicate"]
    assert len(deduped) >= expected_duplicates - 1


def test_scenario_11_late_events_are_judged_at_their_event_time(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    fact = facts[SCN_LATE_EVENTS]
    min_lag = timedelta(hours=int(fact.detail["min_lag_hours"]))

    with session_factory(run.engine)() as session:
        # The lag is computed in Python: SQLite would subtract these as strings, and a
        # silently-empty result would make this test pass while proving nothing.
        candidates = session.execute(
            select(m.Complaint.id, m.Complaint.received_at, m.Complaint.source_ts)
            .where(
                m.Complaint.cell_id.is_not(None),
                m.Complaint.source_ts.is_not(None),
                m.Complaint.received_at >= SCENARIO_WINDOW.start,
                m.Complaint.received_at < SCENARIO_WINDOW.end,
            )
            .order_by(m.Complaint.id),
        ).all()
    late_rows = {
        str(row[0]): (row[1], row[2])
        for row in candidates
        if row[1] - row[2] >= min_lag
    }
    assert late_rows, "the seed must contain late complaints for this to mean anything"

    judged = {
        call["entity_id"]: call["inputs"]
        for call in run.decisions.calls
        if call["action"] == "correlate_complaint" and call["entity_id"] in late_rows
    }
    assert judged, "the late complaints must have been correlated"
    for complaint_id, inputs in judged.items():
        received_at, source_ts = late_rows[complaint_id]
        assert inputs["event_time"] == source_ts.isoformat(), (
            f"{complaint_id} was judged at its ingestion time, not its event time"
        )
        assert inputs["received_at"] == received_at.isoformat()


def test_a_late_complaint_can_reopen_a_closed_incident(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    """Closure is not the end: a complaint for a closed window reopens, never duplicates."""
    fact = facts[SCN_SINGLE_CELL]
    incident = next(i for i in run.detections if _matches(run, i, fact))
    complaint_id = next(iter(run.complaints_of(incident.id)))

    with session_factory(run.engine)() as session:
        correlation, incidents, _, store, _, _, _ = _build(session, run.policies)
        current = incidents.get(incident.id)
        resolved = incidents.mark_resolved(
            current.id,
            verification=VerificationSignal(
                incident_id=current.id,
                passed=True,
                checked_at=ANCHOR,
                detail="KPIs recovered",
            ),
        )
        closed = incidents.close(resolved.id, expected_version=resolved.version)
        assert closed.status is IncidentStatus.CLOSED
        before = len(store.open_in_window(SCENARIO_WINDOW))

        observation = next(
            row
            for row in SqlComplaintReader(session).in_window(SCENARIO_WINDOW)
            if row.complaint_id == complaint_id
        )
        outcome = correlation.correlate_complaint(observation)
        session.flush()
        after = store.open_in_window(SCENARIO_WINDOW)

        assert outcome.decision is CorrelationDecision.REOPENED
        assert outcome.reopen_requested
        assert outcome.incident_id == incident.id
        assert len(after) == before, "a reopen must not create a second incident"
        assert incidents.get(incident.id).status is IncidentStatus.REOPENED
        session.rollback()


# ================================================================ aggregate


def test_precision_and_recall_meet_the_phase_gate(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    score = _score(run, facts)
    matrix = _confusion(run, facts, score)

    assert score["precision"] >= PRECISION_FLOOR, (
        f"precision {score['precision']:.4f} below the {PRECISION_FLOOR} floor{matrix}"
    )
    assert score["recall"] >= RECALL_FLOOR, (
        f"recall {score['recall']:.4f} below the {RECALL_FLOOR} floor{matrix}"
    )


def test_the_confusion_matrix_is_printed_for_the_record(
    run: PipelineRun,
    facts: dict[str, Fact],
) -> None:
    """Not an assertion about quality — it puts the measured numbers in the test log."""
    score = _score(run, facts)
    report = _confusion(run, facts, score)
    assert "precision" in report
    # The matrix is attached to a deliberate, always-true assertion message so that `-rA`
    # and a failing CI run both surface it without a `print` (ruff T20 forbids print).
    assert score["precision"] >= 0.0, report


def test_maintenance_suppression_is_total(run: PipelineRun) -> None:
    """No live incident may overlap an approved maintenance window on its own element."""
    offenders: list[str] = []
    with session_factory(run.engine)() as session:
        maintenance = SqlMaintenanceReader(session)
        topology = SqlTopologyReader(session)
        for incident in run.detections:
            node_id = incident.primary_node_id or ""
            chain = [node_id, *(node.node_id for node in topology.upstream_of(node_id))]
            for element in chain:
                covers = [
                    cover
                    for cover in maintenance.covering(element, incident.window)
                    if cover.is_approved
                ]
                if covers:
                    offenders.append(
                        f"{incident.id} ({incident.primary_node_kind} {node_id}) overlaps "
                        f"{covers[0].maintenance_id} on {element}",
                    )
                    break

    assert offenders == [], "maintenance suppression must be 100%: " + "; ".join(offenders)


def test_the_engine_is_idempotent(run: PipelineRun) -> None:
    first = frozenset(incident.id for incident in run.incidents)

    assert run.second_run_incident_ids == first, (
        "a replay produced a different incident set: "
        f"new={sorted(run.second_run_incident_ids - first)} "
        f"lost={sorted(first - run.second_run_incident_ids)}"
    )
    assert len(run.second_run_incident_ids) == len(run.incidents), "duplicate incidents"
    changed = {
        incident.id: (incident.version, run.second_run_versions[incident.id])
        for incident in run.incidents
        if run.second_run_versions.get(incident.id) != incident.version
    }
    assert changed == {}, f"a replay rewrote incidents that had not changed: {changed}"


def test_every_detection_carries_evidence_and_a_rule_version(run: PipelineRun) -> None:
    for incident in run.detections:
        assert incident.rule_version == run.policies.rule_version
        assert run.entities_of(incident.id), f"{incident.id} has no links"
        calls = run.decisions.for_entity(incident.id)
        assert calls, f"{incident.id} has no decision record"
        assert any(call["evidence"] for call in calls), f"{incident.id} has no evidence"


def test_impact_is_calculated_for_every_live_incident(run: PipelineRun) -> None:
    assert set(run.impacts) == {incident.id for incident in run.detections}
    for incident_id, impact in run.impacts.items():
        assert impact.affected_cells >= 1, incident_id
        assert impact.affected_subscribers > 0, incident_id
        assert 0.0 <= impact.sla_risk <= 1.0, incident_id
        assert impact.evidence


def test_the_pipeline_publishes_the_expected_events(run: PipelineRun) -> None:
    kinds = set(run.events)

    assert "DegradationDetected" in kinds, "network-first entry point must emit its event"
    assert "IncidentDetected" in kinds
    assert "Correlated" in kinds
    assert "ImpactCalculated" in kinds


def test_no_module_reads_ground_truth(repo_root: Path) -> None:
    """The detectors must rediscover the scenarios, not look up the answers."""
    modules = repo_root / "backend" / "src" / "tazama" / "modules"
    offenders = [
        str(path.relative_to(repo_root))
        for path in modules.rglob("*.py")
        if "ground_truth" in path.read_text(encoding="utf-8")
        or "GroundTruth" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"these module files mention the answer key: {offenders}"

    # Belt and braces: the same check through git's own view of the tree, so a file that
    # is present but unreadable cannot hide.
    result = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-e",
            "ground_truth",
            "-e",
            "GroundTruth",
            "--",
            "backend/src/tazama/modules",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode in (0, 1):
        assert result.stdout.strip() == "", result.stdout
