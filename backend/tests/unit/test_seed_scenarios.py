"""Every labelled scenario must be visible in the *observational* data.

These tests read KPIs, alarms, complaints and maintenance windows — never the label — so
they assert exactly what the pipeline will later have to work from. `ground_truth` is used
only to find out which entities and windows to look at, and (for complaints) which cell a
complaint really came from, since the source system deliberately only knows about half of
them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tazama.core.settings import Settings, get_settings
from tazama.domain.value_objects import ComplaintCategory
from tazama.infrastructure.db import models as m
from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.seed import (
    SCN_COMPLAINT_MAP,
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
    ScenarioFact,
    SeedSummary,
    run_seed,
)

NAIROBI = ZoneInfo("Africa/Nairobi")

#: Seeding costs seconds, so the dataset is built once and shared. The fixture is
#: function-scoped (it needs conftest's autouse environment, which is function-scoped)
#: and memoises instead.
_CACHE: dict[str, tuple[Engine, SeedSummary]] = {}


@pytest.fixture
def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Engine, SeedSummary]:
    """One three-day seed for the whole module: all scenarios sit in the final day."""
    if "scn" not in _CACHE:
        path: Path = tmp_path_factory.mktemp("seed_scn") / "scn.db"
        settings: Settings = get_settings().model_copy(update={"SQLITE_PATH": str(path)})
        engine = get_engine(settings)
        Base.metadata.create_all(engine)
        summary = run_seed(settings=settings, seed=777, days=3, fine_days=1)
        _CACHE["scn"] = (engine, summary)
    return _CACHE["scn"]


@pytest.fixture
def session(seeded: tuple[Engine, SeedSummary]) -> Iterable[Session]:
    engine, _ = seeded
    with session_factory(engine)() as sess:
        yield sess


@pytest.fixture
def facts(seeded: tuple[Engine, SeedSummary]) -> dict[str, ScenarioFact]:
    _, summary = seeded
    return {fact.scenario_id: fact for fact in summary.scenarios}


# ------------------------------------------------------------------- helpers


def _kpis(
    session: Session,
    cell_ids: Sequence[str],
    start: datetime,
    end: datetime,
    granularity: str = "1h",
) -> list[m.KpiCell]:
    return list(
        session.execute(
            select(m.KpiCell).where(
                m.KpiCell.cell_id.in_(cell_ids),
                m.KpiCell.granularity == granularity,
                m.KpiCell.bucket_start >= start,
                m.KpiCell.bucket_start < end,
            ),
        )
        .scalars()
        .all(),
    )


def _true_cell_map(session: Session) -> dict[str, str]:
    """complaint id -> the cell it really came from, from the seeded mapping rows."""
    rows = session.execute(
        select(m.GroundTruth.entity_id, m.GroundTruth.detail).where(
            m.GroundTruth.scenario_id == SCN_COMPLAINT_MAP,
        ),
    ).all()
    return {str(entity_id): str(detail["cell_id"]) for entity_id, detail in rows}


def _complaints_for_cells(
    session: Session,
    cell_ids: Iterable[str],
    start: datetime,
    end: datetime,
) -> list[m.Complaint]:
    wanted = set(cell_ids)
    truth = _true_cell_map(session)
    rows = (
        session.execute(
            select(m.Complaint).where(
                m.Complaint.received_at >= start,
                m.Complaint.received_at < end,
            ),
        )
        .scalars()
        .all()
    )
    return [c for c in rows if truth.get(c.id) in wanted or (c.cell_id or "") in wanted]


def _alarms_for(
    session: Session,
    node_ids: Sequence[str],
    start: datetime,
    end: datetime,
) -> list[m.Alarm]:
    return list(
        session.execute(
            select(m.Alarm).where(
                m.Alarm.node_id.in_(node_ids),
                m.Alarm.raised_at >= start,
                m.Alarm.raised_at < end,
            ),
        )
        .scalars()
        .all(),
    )


# ------------------------------------------------------------------ scenarios


def test_scenario_01_single_cell_outage(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_SINGLE_CELL]
    cell_id = fact.entity_id

    rows = _kpis(session, [cell_id], fact.starts_at, fact.ends_at)
    assert len(rows) == 3
    assert all(row.availability_pct == 0.0 for row in rows)
    assert all(row.dl_throughput_mbps == 0.0 for row in rows)

    # Immediately before the window the same cell is healthy: the outage is an event.
    before = _kpis(session, [cell_id], fact.starts_at - timedelta(hours=3), fact.starts_at)
    assert before and all(row.availability_pct >= 99.0 for row in before)

    alarms = _alarms_for(session, [cell_id], fact.starts_at, fact.ends_at)
    assert any(a.alarm_type == "CELL_UNAVAILABLE" and a.severity == "critical" for a in alarms)

    complaints = _complaints_for_cells(session, [cell_id], fact.starts_at, fact.ends_at)
    assert len(complaints) >= 20
    assert all(c.incident_id is None for c in complaints)


def test_scenario_02_link_failure_cascades_to_many_sites(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_LINK_FAILURE]
    sites: list[str] = list(fact.detail["sites"])
    cells: list[str] = list(fact.detail["cells"])
    assert len(sites) >= 5, "a cascade needs many sites to be a different signal"
    assert len(cells) >= 12

    # The topology really does make every affected cell a descendant of the one link.
    link_children = set(
        session.execute(
            select(m.TopologyEdge.child_id).where(m.TopologyEdge.parent_id == fact.entity_id),
        )
        .scalars()
        .all(),
    )
    assert set(sites) == link_children
    assert {
        str(sid)
        for sid in session.execute(
            select(m.Site.parent_node_id).where(m.Site.parent_node_id == fact.entity_id),
        )
        .scalars()
        .all()
    } == {fact.entity_id}

    rows = _kpis(session, cells, fact.starts_at, fact.ends_at)
    assert rows
    assert all(row.availability_pct < 50.0 for row in rows)
    assert all(row.dl_throughput_mbps < 3.0 for row in rows)

    storm = _alarms_for(session, [fact.entity_id, *sites], fact.starts_at, fact.ends_at)
    assert any(a.alarm_type == "LINK_DOWN" for a in storm)
    assert len(storm) >= len(sites) + 1, "an alarm storm, not a single alarm"

    complaints = _complaints_for_cells(session, cells, fact.starts_at, fact.ends_at)
    assert len(complaints) >= 40


def test_scenario_03_evening_congestion_is_in_the_local_evening(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_CONGESTION]
    cells: list[str] = list(fact.detail["cells"])

    for granularity in ("1h", "15m"):
        rows = _kpis(session, cells, fact.starts_at, fact.ends_at, granularity)
        assert rows, f"no {granularity} rows in the congestion window"
        assert all(row.prb_utilisation_pct > 80.0 for row in rows)
        assert all(row.dl_throughput_mbps < 3.0 for row in rows)
        assert all(row.latency_ms > 80.0 for row in rows)
        # Availability stays healthy: congestion is not an outage.
        assert all(row.availability_pct > 99.0 for row in rows)
        local_hours = {row.bucket_start.astimezone(NAIROBI).hour for row in rows}
        assert local_hours <= {18, 19, 20, 21}, "busy hour must be 18:00-22:00 Nairobi"

    complaints = _complaints_for_cells(session, cells, fact.starts_at, fact.ends_at)
    slow = [
        c
        for c in complaints
        if c.category
        in {ComplaintCategory.SLOW_DATA.value, ComplaintCategory.NETWORK_CONGESTION.value}
    ]
    assert len(slow) >= 30


def test_scenario_04_flapping_alarm_has_no_kpi_impact(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_FLAPPING]
    cell_id = fact.entity_id

    alarms = _alarms_for(session, [cell_id], fact.starts_at, fact.ends_at)
    assert len(alarms) >= 6, "six or more raise/clear cycles in an hour is flapping"
    assert all(a.is_flapping for a in alarms)
    assert all(a.cleared_at is not None for a in alarms)
    assert all((a.cleared_at - a.raised_at) <= timedelta(minutes=10) for a in alarms)

    rows = _kpis(session, [cell_id], fact.starts_at, fact.ends_at, "15m")
    assert rows
    assert all(row.availability_pct >= 99.0 for row in rows)
    assert all(row.drop_rate_pct <= 1.0 for row in rows)

    assert _complaints_for_cells(session, [cell_id], fact.starts_at, fact.ends_at) == []


def test_scenario_05_false_alarm_has_no_corroborating_evidence(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_FALSE_ALARM]
    cell_id = fact.entity_id

    alarms = _alarms_for(session, [cell_id], fact.starts_at, fact.ends_at)
    assert len(alarms) == 1
    assert alarms[0].severity == "major"
    assert not alarms[0].is_flapping

    rows = _kpis(session, [cell_id], fact.starts_at, fact.ends_at, "15m")
    assert rows
    assert all(row.availability_pct >= 99.0 for row in rows)
    assert all(row.drop_rate_pct <= 1.0 for row in rows)
    assert all(row.dl_throughput_mbps >= 3.0 for row in rows)

    assert _complaints_for_cells(session, [cell_id], fact.starts_at, fact.ends_at) == []


def test_scenario_06_degradation_is_covered_by_a_maintenance_window(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_MAINTENANCE]
    site_id = str(fact.detail["site_id"])
    cells: list[str] = list(fact.detail["cells"])

    rows = _kpis(session, cells, fact.starts_at, fact.ends_at)
    assert rows
    assert all(row.availability_pct < 99.0 for row in rows)

    windows = (
        session.execute(
            select(m.MaintenanceWindow).where(m.MaintenanceWindow.node_id == site_id),
        )
        .scalars()
        .all()
    )
    assert len(windows) == 1
    window = windows[0]
    assert window.starts_at <= fact.starts_at
    assert window.ends_at >= fact.ends_at
    assert window.approved_by

    # The alarms still fire; suppression has to be the pipeline's decision, not the seed's.
    assert _alarms_for(session, cells, fact.starts_at, fact.ends_at)


def test_scenario_07_non_network_surge_leaves_kpis_healthy(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_NON_NETWORK]
    region = fact.entity_id

    surge = (
        session.execute(
            select(m.Complaint).where(
                m.Complaint.region == region,
                m.Complaint.received_at >= fact.starts_at,
                m.Complaint.received_at < fact.ends_at,
            ),
        )
        .scalars()
        .all()
    )
    non_network = [
        c
        for c in surge
        if c.category in {ComplaintCategory.BILLING.value, ComplaintCategory.BUNDLE_CONFUSION.value}
    ]
    assert len(non_network) >= 100
    # Overwhelmingly non-network, so a category-blind detector would be wrong.
    assert len(non_network) / len(surge) > 0.8

    rows = (
        session.execute(
            select(m.KpiCell).where(
                m.KpiCell.region == region,
                m.KpiCell.granularity == "1h",
                m.KpiCell.bucket_start >= fact.starts_at,
                m.KpiCell.bucket_start < fact.ends_at,
            ),
        )
        .scalars()
        .all()
    )
    assert rows
    assert all(row.availability_pct >= 99.0 for row in rows)
    assert all(row.drop_rate_pct <= 1.0 for row in rows)

    alarms = (
        session.execute(
            select(func.count())
            .select_from(m.Alarm)
            .where(
                m.Alarm.raised_at >= fact.starts_at,
                m.Alarm.raised_at < fact.ends_at,
                m.Alarm.severity == "critical",
            ),
        )
    ).scalar_one()
    assert alarms == 0


def test_scenario_08_outage_silences_the_complaint_channels(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_SILENCED]
    cells: list[str] = list(fact.detail["cells"])
    baseline_start = datetime.fromisoformat(str(fact.detail["baseline_window"][0]))
    baseline_end = datetime.fromisoformat(str(fact.detail["baseline_window"][1]))

    rows = _kpis(session, cells, fact.starts_at, fact.ends_at)
    assert rows
    assert all(row.availability_pct == 0.0 for row in rows)

    before = _complaints_for_cells(session, cells, baseline_start, baseline_end)
    during = _complaints_for_cells(session, cells, fact.starts_at, fact.ends_at)

    before_rate = len(before) / ((baseline_end - baseline_start).total_seconds() / 3600)
    during_rate = len(during) / ((fact.ends_at - fact.starts_at).total_seconds() / 3600)
    assert before_rate > 0, "the baseline has to show what normal looks like"
    assert during_rate < before_rate / 2, "silence is the signal here"


def test_scenario_09_silent_sufferers_complain_to_nobody(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_SILENT]
    cells: list[str] = list(fact.detail["cells"])
    assert len(cells) == 8

    rows = _kpis(session, cells, fact.starts_at, fact.ends_at)
    assert rows
    assert all(row.availability_pct < 99.0 for row in rows)
    assert all(row.drop_rate_pct > 1.0 for row in rows)
    assert {row.cell_id for row in rows} == set(cells)

    assert _complaints_for_cells(session, cells, fact.starts_at, fact.ends_at) == []
    # No alarm either: only the KPIs know, which is what makes this hard.
    assert _alarms_for(session, cells, fact.starts_at, fact.ends_at) == []


def test_scenario_10_duplicate_storm_shares_one_dedupe_key(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_DUPLICATES]
    rows = (
        session.execute(
            select(m.Complaint).where(
                m.Complaint.received_at >= fact.starts_at,
                m.Complaint.received_at < fact.ends_at + timedelta(minutes=1),
            ),
        )
        .scalars()
        .all()
    )
    buckets: dict[str, list[m.Complaint]] = {}
    for row in rows:
        if row.dedupe_key:
            buckets.setdefault(row.dedupe_key, []).append(row)
    storm = max(buckets.values(), key=len)

    assert len(storm) >= 5
    assert len({c.raw_text for c in storm}) == 1
    assert len({c.subscriber_id for c in storm}) == 1
    assert len({c.id for c in storm}) == len(storm), "distinct rows, identical content"
    span = max(c.received_at for c in storm) - min(c.received_at for c in storm)
    assert span <= timedelta(minutes=10)


def test_scenario_11_late_and_out_of_order_events(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    fact = facts[SCN_LATE_EVENTS]

    # The lag is computed in Python: timestamp arithmetic in SQL is backend-specific and
    # this test has to mean the same thing on SQLite and on Oracle.
    rows = (
        session.execute(select(m.Complaint).where(m.Complaint.source_ts.is_not(None)))
        .scalars()
        .all()
    )
    late = [c for c in rows if c.received_at - c.source_ts > timedelta(hours=1)]
    assert len(late) >= int(fact.detail["late_complaints"]) / 2
    assert all(c.source_ts < c.received_at - timedelta(hours=1) for c in late)
    worst = max(c.received_at - c.source_ts for c in late)
    assert worst > timedelta(hours=int(fact.detail["min_lag_hours"]))

    # Arrival order is not chronological either: read in ingestion order, event time goes
    # backwards, so a watermark kept on ingestion time would silently skip events.
    ordered = (
        session.execute(
            select(m.Complaint.received_at)
            .where(m.Complaint.id.in_([c.id for c in late]))
            .order_by(m.Complaint.ingested_at),
        )
        .scalars()
        .all()
    )
    assert any(later < earlier for earlier, later in pairwise(ordered)), (
        "event time must go backwards somewhere in ingestion order"
    )


def test_every_scenario_is_recorded_once_in_ground_truth(
    session: Session,
    facts: dict[str, ScenarioFact],
) -> None:
    rows = (
        session.execute(
            select(m.GroundTruth).where(m.GroundTruth.scenario_id != SCN_COMPLAINT_MAP),
        )
        .scalars()
        .all()
    )
    assert len(rows) == 11
    assert {row.scenario_id for row in rows} == set(facts)
    for row in rows:
        assert row.label
        assert row.starts_at is not None
        assert row.ends_at is not None
        assert row.detail

    # Join coverage can be measured honestly: the truth exists for every complaint, while
    # the observable `cell_id` is populated on only about half of them.
    total = session.execute(select(func.count()).select_from(m.Complaint)).scalar_one()
    mapped = session.execute(
        select(func.count())
        .select_from(m.GroundTruth)
        .where(m.GroundTruth.scenario_id == SCN_COMPLAINT_MAP),
    ).scalar_one()
    known = session.execute(
        select(func.count()).select_from(m.Complaint).where(m.Complaint.cell_id.is_not(None)),
    ).scalar_one()
    assert mapped == total
    assert 0.3 < known / total < 0.6
