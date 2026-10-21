"""Profiler, catalog rendering and join coverage against a known tiny dataset.

The dataset is built by hand here rather than with the seed generator: the numbers must be
obvious enough that a wrong coverage figure is visible on inspection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tazama.infrastructure.db.discovery_repo import SqlCatalogReader, SqlCoverageReader
from tazama.infrastructure.db.models import (
    Alarm,
    CareCall,
    Cell,
    Complaint,
    GroundTruth,
    KpiCell,
    Site,
    Subscriber,
)

T0 = datetime(2026, 3, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
def tiny_dataset(db_session):
    """2 cells, 24 KPI buckets, 10 complaints (6 with a cell), 2 care calls, 1 alarm."""
    session = db_session
    session.add(
        Site(
            id="SITE-NRB-001",
            name="Westlands Macro",
            region="Nairobi",
            county="Nairobi",
            lat=-1.2653,
            lon=36.8039,
            site_type="macro",
            power_backup=True,
        ),
    )
    session.flush()  # the site must exist before cells reference it (FKs are enforced)
    for index in (1, 2):
        session.add(
            Cell(
                id=f"CELL-NRB-00{index}",
                site_id="SITE-NRB-001",
                name=f"Westlands {index}",
                tech="4G",
                band="L1800",
                azimuth_deg=120 * index,
                subscribers_est=1200,
                region="Nairobi",
                lat=-1.2653,
                lon=36.8039,
                created_at=T0 - timedelta(days=400),
            ),
        )
    session.add(Subscriber(id="SUB-AAAAAAAAAAAAAAAA", region="Nairobi", msisdn_format="+254"))

    for hour in range(24):
        for index in (1, 2):
            session.add(
                KpiCell(
                    cell_id=f"CELL-NRB-00{index}",
                    site_id="SITE-NRB-001",
                    region="Nairobi",
                    bucket_start=T0 + timedelta(hours=hour),
                    granularity="1h",
                    availability_pct=100.0 if index == 1 else 92.0,
                    rrc_setup_success_pct=99.1,
                    erab_setup_success_pct=98.9,
                    drop_rate_pct=0.4,
                    prb_utilisation_pct=45.0 + hour,
                    dl_throughput_mbps=12.0,
                    ul_throughput_mbps=4.0,
                    latency_ms=38.0,
                    active_users=120.0,
                    sinr_db=11.0,
                ),
            )

    session.add(
        Alarm(
            id="ALM-0001",
            node_id="CELL-NRB-002",
            node_kind="cell",
            cell_id="CELL-NRB-002",
            site_id="SITE-NRB-001",
            alarm_type="cell_unavailable",
            severity="major",
            raised_at=T0 + timedelta(hours=3),
            cleared_at=T0 + timedelta(hours=4),
        ),
    )

    for index in range(10):
        resolved = index < 6
        session.add(
            Complaint(
                id=f"CMP-{index:04d}",
                received_at=T0 + timedelta(hours=index),
                source_ts=T0
                + timedelta(hours=index)
                - (timedelta(hours=3) if index == 9 else timedelta()),
                channel="call_centre",
                language="en" if index % 2 else "sw",
                raw_text="hakuna network Westlands" if index % 2 else "no service at all",
                subscriber_id="SUB-AAAAAAAAAAAAAAAA" if index < 8 else None,
                stated_location="Westlands",
                region="Nairobi" if index < 7 else None,
                cell_id="CELL-NRB-002" if resolved else None,
                site_id="SITE-NRB-001" if index < 5 else None,
                incident_id="INC-0001" if index < 3 else None,
                category="no_service",
                dedupe_key=f"dk-{index}",
            ),
        )
        # Ground truth knows the true cell for every complaint: the ceiling is 100%.
        session.add(
            GroundTruth(
                scenario_id="SCN-TEST",
                scenario_kind="fixture",
                entity_type="complaint",
                entity_id=f"CMP-{index:04d}",
                label="cell",
                detail={"cell_id": "CELL-NRB-002"},
                created_at=T0,
            ),
        )

    for index in range(2):
        session.add(
            CareCall(
                id=f"CALL-{index:04d}",
                started_at=T0 + timedelta(hours=index),
                duration_s=180,
                subscriber_id="SUB-AAAAAAAAAAAAAAAA",
                reason_code="no_service",
                region="Nairobi",
                cell_id="CELL-NRB-001" if index == 0 else None,
            ),
        )
    session.commit()
    return session


# -------------------------------------------------------------- profiler


def test_lists_only_tables_that_exist(db_engine) -> None:
    tables = SqlCatalogReader(db_engine).list_tables()
    assert "complaint" in tables
    assert "kpi_cell" in tables
    assert tables == tuple(sorted(tables))  # stable order


def test_canonical_columns_match_the_schema(db_engine) -> None:
    columns = SqlCatalogReader(db_engine).canonical_columns()
    assert "received_at" in columns["complaint"]
    assert "availability_pct" in columns["kpi_cell"]


def test_profile_counts_rows_nulls_and_cardinality(db_engine, tiny_dataset) -> None:
    profile = SqlCatalogReader(db_engine).profile_table("complaint")
    assert profile.row_count == 10

    cell_column = profile.column("cell_id")
    assert cell_column is not None
    assert cell_column.null_count == 4
    assert cell_column.null_pct(profile.row_count) == 40.0
    assert cell_column.distinct_count == 1

    region = profile.column("region")
    assert region is not None
    assert region.null_count == 3


def test_profile_reports_freshness(db_engine, tiny_dataset) -> None:
    profile = SqlCatalogReader(db_engine).profile_table("complaint")
    assert profile.freshness_column == "received_at"
    assert profile.freshness == T0 + timedelta(hours=9)
    assert profile.freshness.tzinfo is not None


def test_profile_flags_pii_and_masks_its_samples(db_engine, tiny_dataset) -> None:
    profile = SqlCatalogReader(db_engine).profile_table("complaint")
    assert "raw_text" in profile.pii_columns
    assert "subscriber_id" in profile.pii_columns

    subscriber = profile.column("subscriber_id")
    assert subscriber is not None
    assert subscriber.is_pii
    for sample in subscriber.sample:
        # Masked: a prefix plus asterisks, never the whole token.
        assert "SUB-AAAAAAAAAAAAAAAA" not in sample
        assert "*" in sample


def test_shallow_profile_skips_statistics(db_engine, tiny_dataset) -> None:
    profile = SqlCatalogReader(db_engine).profile_table("complaint", deep=False)
    assert profile.row_count == 10
    assert all(column.null_count is None for column in profile.columns)


def test_profile_marks_indexes_and_primary_keys(db_engine, tiny_dataset) -> None:
    profile = SqlCatalogReader(db_engine).profile_table("complaint")
    pk = profile.column("id")
    assert pk is not None and pk.primary_key and pk.indexed
    received = profile.column("received_at")
    assert received is not None and received.indexed


def test_empty_table_profiles_cleanly(db_engine) -> None:
    profile = SqlCatalogReader(db_engine).profile_table("incident")
    assert profile.row_count == 0
    assert profile.freshness is None
    column = profile.column("status")
    assert column is not None
    assert column.null_pct(profile.row_count) is None


# -------------------------------------------------------- join coverage


def test_join_coverage_numbers_are_exact(db_engine, tiny_dataset) -> None:
    coverage = SqlCoverageReader(db_engine).join_coverage()

    assert coverage.row("complaint -> cell").resolved == 6
    assert coverage.complaint_to_cell_pct == 60.0
    assert coverage.row("complaint -> known cell").resolved == 6  # no dangling ids
    assert coverage.row("complaint -> site").resolved == 5
    assert coverage.row("complaint -> incident").resolved == 3
    assert coverage.row("complaint -> subscriber").resolved == 8
    assert coverage.row("complaint -> region").resolved == 7
    assert coverage.row("care call -> cell").resolved == 1
    assert coverage.row("complaint -> cell (ceiling)").resolved == 10


def test_coverage_detects_dangling_cell_ids(db_engine, tiny_dataset) -> None:
    session = tiny_dataset
    session.add(
        Complaint(
            id="CMP-9999",
            received_at=T0,
            channel="sms",
            language="en",
            raw_text="no service",
            cell_id="CELL-DOES-NOT-EXIST",
        ),
    )
    session.commit()
    coverage = SqlCoverageReader(db_engine).join_coverage()
    assert coverage.row("complaint -> cell").resolved == 7
    # The dangling id does not count as a resolvable join.
    assert coverage.row("complaint -> known cell").resolved == 6


def test_coverage_reports_time_alignment_and_late_arrivals(db_engine, tiny_dataset) -> None:
    coverage = SqlCoverageReader(db_engine).join_coverage()
    questions = {row.question: row for row in coverage.time_alignment}
    assert questions["complaint inside KPI window"].resolved == 10
    assert questions["late arrivals (>1h)"].resolved == 1  # complaint 9 is backdated
    assert questions["clock skew (source ahead)"].resolved == 0


def test_coverage_flags_clock_skew(db_engine, tiny_dataset) -> None:
    session = tiny_dataset
    session.add(
        Complaint(
            id="CMP-SKEW",
            received_at=T0,
            source_ts=T0 + timedelta(hours=2),
            channel="sms",
            language="en",
            raw_text="no service",
        ),
    )
    session.commit()
    coverage = SqlCoverageReader(db_engine).join_coverage()
    skew = next(r for r in coverage.time_alignment if r.question.startswith("clock skew"))
    assert skew.resolved == 1
    assert any("ahead of ingestion" in w for w in coverage.warnings)


def test_coverage_warns_when_the_join_is_poor(db_engine, tiny_dataset) -> None:
    coverage = SqlCoverageReader(db_engine).join_coverage()
    assert any("bottleneck" in w for w in coverage.warnings)


def test_coverage_window_bounds_are_applied(db_engine, tiny_dataset) -> None:
    coverage = SqlCoverageReader(db_engine).join_coverage(
        window_start=T0,
        window_end=T0 + timedelta(hours=5),
    )
    assert coverage.row("complaint -> cell").total == 5
    assert coverage.row("complaint -> cell").resolved == 5


def test_coverage_on_empty_database_is_honest(db_engine) -> None:
    coverage = SqlCoverageReader(db_engine).join_coverage()
    assert coverage.rows == ()
    assert any("No complaints" in w for w in coverage.warnings)


def test_coverage_renders_a_readable_report(db_engine, tiny_dataset) -> None:
    rendered = SqlCoverageReader(db_engine).join_coverage().render()
    assert "complaint -> cell" in rendered
    assert "60.00%" in rendered
    assert "time alignment" in rendered
