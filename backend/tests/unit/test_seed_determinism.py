"""The seed must be reproducible, timezone-clean and free of raw identifiers.

Each dataset lives in its own temporary SQLite file rather than the shared in-memory
database, so two seeds can be compared side by side without disposing one to build the
other. Schema comes from `Base.metadata.create_all` (conftest contract), not Alembic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from tazama.core.settings import Settings, get_settings
from tazama.infrastructure.db import models as m
from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.seed import ANCHOR, SeedAborted, SeedSummary, run_seed

#: Small enough to run several times in a unit test, large enough to contain every
#: labelled scenario (they all live in the final 24 hours).
FAST: dict[str, int] = {"days": 2, "fine_days": 1}

TARGET_CELLS = 600

#: The pattern a raw Kenyan MSISDN would match. Nothing the seed stores may match it.
RAW_MSISDN = re.compile(r"(?<!\d)(?:\+?254|0)(?:7|1)\d{8}(?!\d)")

#: Seeding costs seconds; memoise instead of using a module-scoped fixture, which would
#: be built before conftest's function-scoped autouse environment exists.
_CACHE: dict[str, tuple[Engine, Settings, SeedSummary]] = {}


def _seed(path: Path, **kwargs: Any) -> tuple[Engine, Settings, SeedSummary]:
    settings: Settings = get_settings().model_copy(update={"SQLITE_PATH": str(path)})
    engine = get_engine(settings)
    Base.metadata.create_all(engine)
    summary = run_seed(settings=settings, **kwargs)
    return engine, settings, summary


def _cached(
    key: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Engine, Settings, SeedSummary]:
    if key not in _CACHE:
        path = tmp_path_factory.mktemp(f"seed_{key}") / f"{key}.db"
        _CACHE[key] = _seed(path, seed=4242, **FAST)
    return _CACHE[key]


@pytest.fixture
def primary(tmp_path_factory: pytest.TempPathFactory) -> tuple[Engine, Settings, SeedSummary]:
    return _cached("a", tmp_path_factory)


@pytest.fixture
def replica(tmp_path_factory: pytest.TempPathFactory) -> tuple[Engine, Settings, SeedSummary]:
    """A second, independent database seeded with exactly the same arguments."""
    return _cached("b", tmp_path_factory)


def test_two_databases_same_arguments_produce_one_hash(
    primary: tuple[Engine, Settings, SeedSummary],
    replica: tuple[Engine, Settings, SeedSummary],
) -> None:
    _, _, first = primary
    _, _, second = replica
    assert first.content_hash == second.content_hash
    assert first.row_counts == second.row_counts
    # The hash is also persisted, so drift is visible to an operator, not just a test.
    assert len(first.content_hash) == 64


def test_seed_run_row_records_the_parameters(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    engine, _, summary = primary
    with session_factory(engine)() as session:
        row = session.execute(select(m.SeedRun)).scalars().one()
    assert row.seed == 4242
    assert row.days == FAST["days"]
    assert row.fine_days == FAST["fine_days"]
    assert row.content_hash == summary.content_hash
    assert row.anchor_at == ANCHOR
    assert row.row_counts is not None
    assert row.row_counts["kpi_cell"] == summary.row_counts["kpi_cell"]


def test_fast_configuration_really_is_the_small_dataset(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    """Guard against a test that silently seeds 30 days and passes slowly.

    The KPI volume is exactly determined by the arguments, so the fast path is provable
    rather than assumed.
    """
    engine, _, summary = primary
    expected = TARGET_CELLS * (FAST["days"] * 24 + FAST["fine_days"] * 96)
    assert summary.row_counts["kpi_cell"] == expected
    full_run = TARGET_CELLS * (30 * 24 + 7 * 96)
    assert expected < full_run / 8

    with session_factory(engine)() as session:
        counted = session.execute(select(func.count()).select_from(m.KpiCell)).scalar_one()
        fine = session.execute(
            select(func.count()).select_from(m.KpiCell).where(m.KpiCell.granularity == "15m"),
        ).scalar_one()
    assert counted == expected
    assert fine == TARGET_CELLS * FAST["fine_days"] * 96


def test_a_different_seed_changes_the_hash(
    tmp_path: Path,
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    _, _, baseline = primary
    _, _, other = _seed(tmp_path / "other.db", seed=909_090, **FAST)
    assert other.content_hash != baseline.content_hash
    # Volumes are driven by the arguments, not by the seed, so they must still match.
    assert other.row_counts == baseline.row_counts


def test_every_stored_timestamp_is_utc_aware(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    engine, _, _ = primary
    probes: tuple[tuple[Any, ...], ...] = (
        (m.Complaint.received_at, m.Complaint.source_ts, m.Complaint.ingested_at),
        (m.KpiCell.bucket_start,),
        (m.Alarm.raised_at, m.Alarm.cleared_at),
        (m.CareCall.started_at,),
        (m.MaintenanceWindow.starts_at, m.MaintenanceWindow.ends_at),
        (m.Subscriber.created_at, m.Subscriber.last_seen_at),
        (m.Site.commissioned_at,),
        (m.GroundTruth.created_at,),
        (m.KnowledgeDoc.valid_from,),
    )
    with session_factory(engine)() as session:
        for columns in probes:
            rows = session.execute(select(*columns).limit(200)).all()
            assert rows, f"no rows to probe for {columns[0]}"
            for row in rows:
                for value in row:
                    if value is None:
                        continue
                    assert value.tzinfo is not None, f"naive datetime in {columns[0]}"
                    assert value.utcoffset().total_seconds() == 0, f"non-UTC in {columns[0]}"


def test_no_column_holds_anything_shaped_like_an_msisdn(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    """Pseudonymisation is structural: there is no raw number to leak (ADR 0008)."""
    engine, _, _ = primary
    with session_factory(engine)() as session:
        subscribers = session.execute(select(m.Subscriber)).scalars().all()
        texts = session.execute(select(m.Complaint.raw_text)).scalars().all()
        locations = session.execute(select(m.Complaint.stated_location)).scalars().all()
        reasons = session.execute(select(m.CareCall.reason_text)).scalars().all()

    assert len(subscribers) == 30_000
    for sub in subscribers:
        blob = " ".join(
            str(value)
            for value in (
                sub.id,
                sub.region,
                sub.home_cell_id,
                sub.last_cell_id,
                sub.segment,
                sub.enterprise_ref,
                sub.sla_tier,
                sub.msisdn_format,
            )
            if value is not None
        )
        assert RAW_MSISDN.search(blob) is None, f"MSISDN-shaped value on {sub.id}"

    for text in texts:
        assert RAW_MSISDN.search(text) is None
    for blob in (*locations, *reasons):
        if blob is not None:
            assert RAW_MSISDN.search(blob) is None

    # All four input shapes were exercised on the way in, and only the shape survived.
    assert {sub.msisdn_format for sub in subscribers} == {
        "e164_plus",
        "cc_no_plus",
        "local_07",
        "local_01",
    }


def test_seeding_a_populated_database_without_reset_is_refused(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    _, settings, _ = primary
    with pytest.raises(SeedAborted) as caught:
        run_seed(settings=settings, seed=4242, **FAST)
    message = str(caught.value)
    assert "--reset" in message
    assert "already contains seeded data" in message


def test_reset_replaces_the_dataset_byte_for_byte(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    engine, settings, baseline = primary
    again = run_seed(settings=settings, seed=4242, reset=True, **FAST)
    assert again.content_hash == baseline.content_hash
    assert again.row_counts == baseline.row_counts
    with session_factory(engine)() as session:
        assert session.execute(select(func.count()).select_from(m.SeedRun)).scalar_one() == 1


def test_rejects_arguments_that_would_drop_the_scenarios(tmp_path: Path) -> None:
    settings: Settings = get_settings().model_copy(
        update={"SQLITE_PATH": str(tmp_path / "bad.db")},
    )
    Base.metadata.create_all(get_engine(settings))
    with pytest.raises(SeedAborted, match="days must be at least 2"):
        run_seed(settings=settings, days=1, fine_days=1)
    with pytest.raises(SeedAborted, match="fine_days"):
        run_seed(settings=settings, days=2, fine_days=5)


def test_render_is_a_string_and_names_the_scenarios(
    primary: tuple[Engine, Settings, SeedSummary],
) -> None:
    _, _, summary = primary
    plain = summary.render()
    detailed = summary.render(include_scenarios=True)
    assert summary.content_hash in plain
    assert "kpi_cell" in plain
    assert "SCN-01-single-cell-outage" not in plain
    assert "SCN-01-single-cell-outage" in detailed
    assert len(summary.scenarios) == 11
    assert len({fact.scenario_id for fact in summary.scenarios}) == 11
