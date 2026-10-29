"""Service tests over a small hand-built dataset, through the real SQL adapters.

The dataset is constructed rather than seeded so that every number asserted here can be
worked out on paper: 2,000 subscribers and one complaint is 0.5 per 1,000, a perfectly
repeating daily profile de-seasonalises to exactly zero, and a KPI dip at 12:00 followed
by a complaint spike at 14:00 must come back as a lead of exactly +2 hours.

Three weeks of hourly data are inserted because the de-seasonalisation baseline needs
three observations per hour-of-week slot: with two, the median of a slot containing an
injected spike is the midpoint of the spike and the normal value, and the injected
anomaly would partly remove itself.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from tazama.core.clock import FixedClock
from tazama.core.errors import BadRequest
from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.infrastructure.db.insights_repo import (
    SqlCellExposureReader,
    SqlComplaintSeriesReader,
    SqlKpiSeriesReader,
)
from tazama.infrastructure.db.models import Cell, Complaint, KpiCell, Site
from tazama.modules.insights.models import (
    COMPLAINT_RATE_LABEL,
    DEFAULT_KPI_FAMILIES,
    InsightsConfig,
)
from tazama.modules.insights.service import InsightsService

HOUR = timedelta(hours=1)

#: Three weeks of margin before the analysis window, plus a day of slack.
SEED_START = datetime(2026, 2, 24, 0, 0, tzinfo=UTC)
SEED_HOURS = 22 * 24

WINDOW = TimeWindow(
    start=datetime(2026, 3, 15, 0, 0, tzinfo=UTC),
    end=datetime(2026, 3, 18, 0, 0, tzinfo=UTC),
)
#: The injected outage: availability dips here, complaints spike two hours later.
EVENT_AT = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
SPIKE_AT = EVENT_AT + 2 * HOUR
SPIKE_COMPLAINTS = 4

BIG_CELL, SMALL_CELL, DRIVEN_CELL = "CELL-TST-001", "CELL-TST-002", "CELL-TST-003"
BIG_SUBS, SMALL_SUBS, DRIVEN_SUBS = 2_000, 500, 1_000
NORMAL_AVAILABILITY, DIP_AVAILABILITY = 99.9, 90.0


def _daily_complaints(hour: int) -> int:
    """A clean, perfectly repeating daily profile: four complaints every evening."""
    return 1 if 18 <= hour <= 21 else 0


def _jitter(index: int, offset: int) -> float:
    """Deterministic wobble, so a de-seasonalised KPI column still has variance.

    Its period (23) does not divide 168, so it does not fold into the weekly profile.
    """
    return ((index * 37 + offset * 101) % 23 - 11) / 100.0


class _RecordingDecisionLog:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def record(self, **kwargs: object) -> None:
        self.entries.append(kwargs)


@pytest.fixture
def dataset(db_session):
    """3 cells, 22 days of hourly KPIs, an evening complaint profile and one outage."""
    session = db_session
    session.add(
        Site(
            id="SITE-TST-001",
            name="Test Macro",
            region="Nairobi",
            county="Nairobi",
            lat=-1.2653,
            lon=36.8039,
            site_type="macro",
            power_backup=True,
        ),
    )
    session.flush()
    for cell_id, subscribers, region in (
        (BIG_CELL, BIG_SUBS, "Nairobi"),
        (SMALL_CELL, SMALL_SUBS, "Nairobi"),
        (DRIVEN_CELL, DRIVEN_SUBS, "Mombasa"),
    ):
        session.add(
            Cell(
                id=cell_id,
                site_id="SITE-TST-001",
                name=cell_id,
                tech="4G",
                band="L1800",
                azimuth_deg=0,
                subscribers_est=subscribers,
                status="active",
                region=region,
                lat=-1.2653,
                lon=36.8039,
                created_at=SEED_START - timedelta(days=400),
            ),
        )
    session.flush()

    # A pseudo-random driver for the third cell: its KPI determines the complaint count
    # two hours later, which is a known Granger relationship end to end.
    rng = random.Random(13)
    driver = [rng.choice((0, 0, 1, 2, 3)) for _ in range(SEED_HOURS)]

    kpi_rows: list[dict[str, object]] = []
    complaint_rows: list[dict[str, object]] = []
    for index in range(SEED_HOURS):
        moment = SEED_START + index * HOUR
        for cell_id, region in (
            (BIG_CELL, "Nairobi"),
            (SMALL_CELL, "Nairobi"),
            (DRIVEN_CELL, "Mombasa"),
        ):
            availability = NORMAL_AVAILABILITY
            if cell_id == BIG_CELL and moment == EVENT_AT:
                availability = DIP_AVAILABILITY
            if cell_id == DRIVEN_CELL:
                availability = NORMAL_AVAILABILITY - 3.0 * driver[index]
            kpi_rows.append(
                {
                    "cell_id": cell_id,
                    "site_id": "SITE-TST-001",
                    "region": region,
                    "bucket_start": moment,
                    "granularity": "1h",
                    "availability_pct": availability,
                    "rrc_setup_success_pct": 99.0 + _jitter(index, 1),
                    "erab_setup_success_pct": 98.5,
                    "drop_rate_pct": 0.3 + 0.2 * _jitter(index, 2),
                    "prb_utilisation_pct": 40.0 + 2.0 * moment.hour + _jitter(index, 3),
                    "dl_throughput_mbps": 20.0 - 0.3 * moment.hour + _jitter(index, 4),
                    "ul_throughput_mbps": 5.0,
                    "latency_ms": 30.0 + 1.5 * moment.hour + _jitter(index, 5),
                    "active_users": 100.0 + 8.0 * moment.hour + _jitter(index, 6),
                    "sinr_db": 12.0 - 0.1 * moment.hour + _jitter(index, 7),
                },
            )

            if cell_id == DRIVEN_CELL:
                count = driver[index - 2] if index >= 2 else 0
            else:
                count = _daily_complaints(moment.hour)
                if cell_id == BIG_CELL and moment == SPIKE_AT:
                    count += SPIKE_COMPLAINTS
            for number in range(count):
                complaint_rows.append(
                    {
                        "id": f"CMP-{cell_id[-3:]}-{index:05d}-{number}",
                        "received_at": moment,
                        "source_ts": moment,
                        "channel": "call_centre",
                        "language": "en",
                        "raw_text": "no network",
                        "region": region,
                        "cell_id": cell_id,
                        "site_id": "SITE-TST-001",
                        "category": "no_service",
                        "is_network": True,
                        "status": "received",
                    },
                )

    session.execute(insert(KpiCell), kpi_rows)
    session.execute(insert(Complaint), complaint_rows)
    session.flush()
    return session


@pytest.fixture
def decisions() -> _RecordingDecisionLog:
    return _RecordingDecisionLog()


@pytest.fixture
def service(dataset, frozen_clock, decisions) -> InsightsService:
    return InsightsService(
        kpis=SqlKpiSeriesReader(dataset),
        complaints=SqlComplaintSeriesReader(dataset),
        cells=SqlCellExposureReader(dataset),
        clock=frozen_clock,
        decisions=decisions,
        # 21 days = exactly three observations per hour-of-week slot.
        config=InsightsConfig(baseline_days=21),
    )


def _at(series, moment: datetime, values):
    return values[series.bucket_starts.index(moment)]


# ================================================================ construction


def test_service_refuses_a_non_hourly_granularity(frozen_clock) -> None:
    with pytest.raises(BadRequest, match="hourly buckets"):
        InsightsService(
            kpis=SqlKpiSeriesReader(None),
            complaints=SqlComplaintSeriesReader(None),
            cells=SqlCellExposureReader(None),
            clock=frozen_clock,
            config=InsightsConfig(granularity="15m"),
        )


def test_config_validates_its_thresholds() -> None:
    with pytest.raises(ValueError, match="max_lag_hours"):
        InsightsConfig(max_lag_hours=0)
    with pytest.raises(ValueError, match="significance"):
        InsightsConfig(significance=1.0)


# =============================================================== normalisation


def test_complaint_rate_is_exactly_per_1000_subscribers(service: InsightsService) -> None:
    """One complaint on a 2,000-subscriber cell is 0.5 per 1,000 subscribers per hour."""
    series = service.complaint_rate(window=WINDOW, cell_id=BIG_CELL)
    evening = datetime(2026, 3, 16, 19, 0, tzinfo=UTC)
    assert series.subscribers_est == BIG_SUBS
    assert _at(series, evening, series.counts) == 1.0
    assert _at(series, evening, series.rate_per_1k) == pytest.approx(0.5)
    assert _at(series, SPIKE_AT, series.counts) == float(SPIKE_COMPLAINTS)
    assert _at(series, SPIKE_AT, series.rate_per_1k) == pytest.approx(2.0)


def test_complaint_rate_spans_the_whole_window_with_zero_filled_buckets(
    service: InsightsService,
) -> None:
    series = service.complaint_rate(window=WINDOW, cell_id=BIG_CELL)
    assert series.n_points == 72
    assert series.bucket_starts[0] == WINDOW.start
    assert series.bucket_starts[-1] == WINDOW.end - HOUR
    quiet = datetime(2026, 3, 16, 3, 0, tzinfo=UTC)
    assert _at(series, quiet, series.counts) == 0.0
    assert series.gaps == 0


def test_complaint_rate_makes_two_cell_sizes_comparable(service: InsightsService) -> None:
    """The same evening complaint is 0.5 per 1k on the big cell and 2.0 on the small one."""
    big = service.complaint_rate(window=WINDOW, cell_id=BIG_CELL)
    small = service.complaint_rate(window=WINDOW, cell_id=SMALL_CELL)
    evening = datetime(2026, 3, 16, 19, 0, tzinfo=UTC)
    assert _at(big, evening, big.counts) == _at(small, evening, small.counts) == 1.0
    assert _at(big, evening, big.rate_per_1k) == pytest.approx(0.5)
    assert _at(small, evening, small.rate_per_1k) == pytest.approx(2.0)


def test_complaint_rate_by_region_sums_the_exposure(service: InsightsService) -> None:
    series = service.complaint_rate(window=WINDOW, region="Nairobi")
    assert series.scope == "region:Nairobi"
    assert series.subscribers_est == BIG_SUBS + SMALL_SUBS
    evening = datetime(2026, 3, 16, 19, 0, tzinfo=UTC)
    # Two Nairobi cells, one complaint each, 2,500 subscribers between them.
    assert _at(series, evening, series.counts) == 2.0
    assert _at(series, evening, series.rate_per_1k) == pytest.approx(0.8)


def test_complaint_rate_without_a_denominator_says_so(service: InsightsService) -> None:
    series = service.complaint_rate(window=WINDOW, cell_id="CELL-DOES-NOT-EXIST")
    assert series.subscribers_est == 0
    assert set(series.rate_per_1k) == {0.0}
    assert any("no subscriber estimate" in caveat for caveat in series.caveats)


def test_complaint_rate_flags_an_all_zero_series(service: InsightsService) -> None:
    quiet_window = TimeWindow(
        start=datetime(2026, 3, 16, 0, 0, tzinfo=UTC),
        end=datetime(2026, 3, 16, 6, 0, tzinfo=UTC),
    )
    series = service.complaint_rate(window=quiet_window, cell_id=BIG_CELL)
    assert not any(series.counts)
    assert any("all zero" in caveat for caveat in series.caveats)
    assert any("below the" in caveat for caveat in series.caveats)  # thin-n caveat


def test_complaint_rate_rejects_both_scopes(service: InsightsService) -> None:
    with pytest.raises(BadRequest, match="not both"):
        service.complaint_rate(window=WINDOW, cell_id=BIG_CELL, region="Nairobi")


# =========================================================== de-seasonalisation


def test_deseasonalisation_removes_the_injected_daily_profile(
    service: InsightsService,
) -> None:
    """A perfectly repeating evening profile must adjust to exactly zero."""
    series = service.complaint_rate(window=WINDOW, cell_id=SMALL_CELL)
    assert all(abs(value) < 1e-9 for value in series.deseasonalised)
    assert series.profile_slots_covered == 168
    profile = series.hour_of_week_profile
    # Monday 2026-03-16 19:00 UTC is hour-of-week slot 19; one complaint on 500 subs = 2.0.
    assert profile[19] == pytest.approx(2.0)
    assert profile[3] == pytest.approx(0.0)


def test_deseasonalisation_keeps_the_injected_spike(service: InsightsService) -> None:
    series = service.complaint_rate(window=WINDOW, cell_id=BIG_CELL)
    adjusted = _at(series, SPIKE_AT, series.deseasonalised)
    assert adjusted == pytest.approx(SPIKE_COMPLAINTS * 1000.0 / BIG_SUBS)
    assert sum(1 for value in series.deseasonalised if abs(value) > 1e-9) == 1


def test_a_short_baseline_is_reported_as_a_caveat(dataset, frozen_clock) -> None:
    service = InsightsService(
        kpis=SqlKpiSeriesReader(dataset),
        complaints=SqlComplaintSeriesReader(dataset),
        cells=SqlCellExposureReader(dataset),
        clock=frozen_clock,
        config=InsightsConfig(baseline_days=2),
    )
    series = service.complaint_rate(window=WINDOW, cell_id=BIG_CELL)
    assert series.profile_slots_covered < 168
    assert any("hour-of-week slots" in caveat for caveat in series.caveats)


# ================================================================== lead / lag


def test_lead_lag_sign_convention_end_to_end(service: InsightsService) -> None:
    """Availability dips at 12:00, complaints spike at 14:00: the lead is exactly +2h."""
    result = service.lead_lag(cell_id=BIG_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    assert result.best_lag_hours == 2
    assert result.best_correlation == pytest.approx(-1.0, abs=1e-6)
    assert result.kpi_leads is True
    assert result.n_points == 72
    assert result.lags == (-6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6)
    assert result.correlation_at(2) == pytest.approx(-1.0, abs=1e-6)


def test_lead_lag_finds_the_same_lead_on_a_driven_cell(service: InsightsService) -> None:
    result = service.lead_lag(cell_id=DRIVEN_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    assert result.best_lag_hours == 2
    assert result.kpi_leads is True
    assert result.best_correlation is not None
    assert result.best_correlation < -0.8


def test_lead_lag_reports_no_lead_for_a_constant_kpi(service: InsightsService) -> None:
    result = service.lead_lag(
        cell_id=BIG_CELL,
        kpi=KpiName.UL_THROUGHPUT,  # seeded flat, so it de-seasonalises to zero
        window=WINDOW,
    )
    assert result.best_correlation is None
    assert result.kpi_leads is False
    assert any("constant" in caveat for caveat in result.caveats)


def test_lead_lag_always_warns_about_the_lag_scan(service: InsightsService) -> None:
    result = service.lead_lag(cell_id=BIG_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    assert any("lags were scanned" in caveat for caveat in result.caveats)
    assert result.inputs["kpi"] == KpiName.AVAILABILITY.value
    assert result.inputs["max_lag_hours"] == 6
    assert result.inputs["deseasonalised"] is True


def test_lead_lag_honours_a_narrower_lag_range(service: InsightsService) -> None:
    result = service.lead_lag(
        cell_id=BIG_CELL,
        kpi=KpiName.AVAILABILITY,
        window=WINDOW,
        max_lag_hours=1,
    )
    assert result.lags == (-1, 0, 1)
    assert result.best_lag_hours != 2  # the true lead is outside the range it was given


def test_lead_lag_refuses_a_zero_lag_range(service: InsightsService) -> None:
    with pytest.raises(BadRequest, match="max_lag_hours"):
        service.lead_lag(
            cell_id=BIG_CELL,
            kpi=KpiName.AVAILABILITY,
            window=WINDOW,
            max_lag_hours=0,
        )


# ==================================================================== Granger


def test_granger_finds_the_driven_cell(service: InsightsService) -> None:
    result = service.granger(cell_id=DRIVEN_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    assert result.p_value is not None
    assert result.p_value < 0.05
    assert result.significant is True
    assert result.lag_hours is not None
    assert "not proof of causation" in result.verdict


def test_granger_always_states_that_precedence_is_not_causation(
    service: InsightsService,
) -> None:
    result = service.granger(cell_id=DRIVEN_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    assert any("not causation" in caveat for caveat in result.caveats)


def test_granger_returns_not_enough_signal_for_a_flat_kpi(service: InsightsService) -> None:
    result = service.granger(
        cell_id=BIG_CELL,
        kpi=KpiName.ERAB_SETUP_SUCCESS,  # seeded flat
        window=WINDOW,
    )
    assert result.f_statistic is None
    assert result.significant is False
    assert "not enough signal" in result.verdict


def test_granger_warns_when_the_series_is_short(service: InsightsService) -> None:
    short = TimeWindow(
        start=datetime(2026, 3, 17, 0, 0, tzinfo=UTC),
        end=datetime(2026, 3, 18, 0, 0, tzinfo=UTC),
    )
    result = service.granger(cell_id=DRIVEN_CELL, kpi=KpiName.AVAILABILITY, window=short)
    assert result.n_points <= 24
    assert any("usable points is below" in caveat for caveat in result.caveats)


def test_granger_refuses_a_zero_lag_range(service: InsightsService) -> None:
    with pytest.raises(BadRequest, match="max_lag_hours"):
        service.granger(
            cell_id=BIG_CELL,
            kpi=KpiName.AVAILABILITY,
            window=WINDOW,
            max_lag_hours=0,
        )


# ================================================================ event study


def test_event_study_finds_the_injected_kpi_outage(service: InsightsService) -> None:
    result = service.event_study(
        cell_ids=[BIG_CELL],
        window=WINDOW,
        event_at=EVENT_AT,
        kpi=KpiName.AVAILABILITY,
    )
    assert result.offsets_hours == tuple(range(-24, 25))
    assert result.pre_event_baseline == pytest.approx(NORMAL_AVAILABILITY)
    assert result.peak_offset_hours == 0
    assert result.peak_deviation == pytest.approx(DIP_AVAILABILITY - NORMAL_AVAILABILITY)
    assert result.metric == KpiName.AVAILABILITY.value
    assert result.series_used == 1


def test_event_study_finds_the_injected_complaint_spike(service: InsightsService) -> None:
    result = service.event_study(cell_ids=[BIG_CELL], window=WINDOW, event_at=EVENT_AT)
    assert result.metric == COMPLAINT_RATE_LABEL
    # 24 pre-event buckets, four of them an evening complaint at 0.5 per 1,000.
    assert result.pre_event_baseline == pytest.approx(4 * 0.5 / 24)
    assert result.peak_offset_hours == 2
    assert result.peak_deviation == pytest.approx(2.0 - 4 * 0.5 / 24)


def test_event_study_dilutes_the_outage_across_healthy_cells(
    service: InsightsService,
) -> None:
    result = service.event_study(
        cell_ids=[BIG_CELL, SMALL_CELL],
        window=WINDOW,
        event_at=EVENT_AT,
        kpi=KpiName.AVAILABILITY,
    )
    assert result.series_used == 2
    assert result.peak_deviation == pytest.approx((DIP_AVAILABILITY - NORMAL_AVAILABILITY) / 2)


def test_event_study_warns_when_the_window_clips_the_offsets(
    service: InsightsService,
) -> None:
    narrow = TimeWindow(
        start=datetime(2026, 3, 16, 0, 0, tzinfo=UTC),
        end=datetime(2026, 3, 17, 0, 0, tzinfo=UTC),
    )
    result = service.event_study(
        cell_ids=[BIG_CELL],
        window=narrow,
        event_at=EVENT_AT,
        kpi=KpiName.AVAILABILITY,
    )
    assert any("event offsets" in caveat for caveat in result.caveats)
    assert result.observation_counts[0] == 0


def test_event_study_reports_cells_with_no_data(service: InsightsService) -> None:
    result = service.event_study(
        cell_ids=["CELL-NOPE"],
        window=WINDOW,
        event_at=EVENT_AT,
        kpi=KpiName.AVAILABILITY,
    )
    assert result.cell_ids == ()
    assert any("none of the requested cells" in caveat for caveat in result.caveats)


def test_event_study_refuses_an_event_outside_the_window(service: InsightsService) -> None:
    with pytest.raises(BadRequest, match="inside the supplied window"):
        service.event_study(
            cell_ids=[BIG_CELL],
            window=WINDOW,
            event_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_event_study_refuses_an_empty_cell_list(service: InsightsService) -> None:
    with pytest.raises(BadRequest, match="at least one cell"):
        service.event_study(cell_ids=[], window=WINDOW, event_at=EVENT_AT)


# =========================================================== correlation / PCA


def test_correlation_matrix_is_square_symmetric_and_labelled(
    service: InsightsService,
) -> None:
    result = service.correlation_matrix(window=WINDOW, region="Nairobi")
    assert result.labels[-1] == COMPLAINT_RATE_LABEL
    assert len(result.labels) == len(DEFAULT_KPI_FAMILIES) + 1
    assert len(result.pearson) == len(result.labels)
    assert all(len(row) == len(result.labels) for row in result.pearson)
    for index, label in enumerate(result.labels):
        assert result.pearson[index][index] == 1.0
        assert result.spearman[index][index] == 1.0
        assert result.value(label, label) == 1.0
    assert result.value("latency_ms", "active_users") == result.value(
        "active_users",
        "latency_ms",
    )
    assert result.n_points == 72


def test_correlation_matrix_ranks_the_strongest_pairs_first(
    service: InsightsService,
) -> None:
    result = service.correlation_matrix(window=WINDOW, region="Nairobi")
    strengths = [pair.abs_pearson for pair in result.top]
    assert strengths == sorted(strengths, reverse=True)
    assert len(result.top) <= InsightsConfig().top_correlations
    assert any("de-seasonalised" in caveat for caveat in result.caveats)


def test_pca_reports_scree_cumulative_variance_and_loadings(
    service: InsightsService,
) -> None:
    result = service.pca(window=WINDOW, region="Nairobi")
    assert result.n_components > 0
    assert result.labels == tuple(kpi.value for kpi in DEFAULT_KPI_FAMILIES)
    assert result.cumulative_variance[-1] == pytest.approx(1.0)
    assert result.cumulative_variance == tuple(sorted(result.cumulative_variance))
    assert sum(result.explained_variance_ratio) == pytest.approx(1.0)
    assert all(len(row) == len(result.labels) for row in result.loadings)
    assert set(result.components_for) == {"80%", "90%"}
    assert 1 <= result.components_for["80%"] <= result.components_for["90%"]
    assert result.n_points == 72


def test_pca_drops_the_flat_kpi_families(service: InsightsService) -> None:
    result = service.pca(window=WINDOW, region="Nairobi")
    assert KpiName.ERAB_SETUP_SUCCESS.value in result.dropped_labels
    assert KpiName.UL_THROUGHPUT.value in result.dropped_labels
    assert any("constant over this window" in caveat for caveat in result.caveats)


def test_pca_always_says_variance_is_not_relevance(service: InsightsService) -> None:
    result = service.pca(window=WINDOW, region="Nairobi")
    assert any("supervised importance" in caveat for caveat in result.caveats)


# ========================================================= driver candidates


def test_driver_candidates_rank_every_kpi_family(service: InsightsService) -> None:
    scores = service.driver_candidates(window=WINDOW, region="Nairobi")
    assert len(scores) == len(DEFAULT_KPI_FAMILIES)
    assert [score.rank for score in scores] == list(range(1, len(scores) + 1))
    assert {score.kpi for score in scores} == set(DEFAULT_KPI_FAMILIES)
    assert all(score.score >= 0.0 for score in scores)
    assert [score.score for score in scores] == sorted(
        (score.score for score in scores),
        reverse=True,
    )
    assert all(score.n_points == 72 for score in scores)


def test_driver_candidates_always_state_that_mi_is_unsigned(
    service: InsightsService,
) -> None:
    scores = service.driver_candidates(window=WINDOW, region="Nairobi")
    assert any("unsigned" in caveat for caveat in scores[0].caveats)


# ============================================================ audit / caveats


def test_every_analysis_records_one_decision(
    service: InsightsService,
    decisions: _RecordingDecisionLog,
) -> None:
    service.complaint_rate(window=WINDOW, cell_id=BIG_CELL)
    service.lead_lag(cell_id=BIG_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    service.granger(cell_id=BIG_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW)
    service.event_study(cell_ids=[BIG_CELL], window=WINDOW, event_at=EVENT_AT)
    service.correlation_matrix(window=WINDOW, region="Nairobi")
    service.pca(window=WINDOW, region="Nairobi")
    service.driver_candidates(window=WINDOW, region="Nairobi")

    actions = {str(entry["action"]) for entry in decisions.entries}
    assert actions >= {
        "insights.complaint_rate",
        "insights.lead_lag",
        "insights.granger",
        "insights.event_study",
        "insights.correlation_matrix",
        "insights.pca",
        "insights.driver_candidates",
    }
    assert all(entry["entity_type"] == "analysis" for entry in decisions.entries)
    assert all(entry["rule_version"] == "insights-1.0.0" for entry in decisions.entries)


def test_the_service_works_without_a_decision_log(dataset, frozen_clock) -> None:
    service = InsightsService(
        kpis=SqlKpiSeriesReader(dataset),
        complaints=SqlComplaintSeriesReader(dataset),
        cells=SqlCellExposureReader(dataset),
        clock=frozen_clock,
        config=InsightsConfig(baseline_days=21),
    )
    assert service.complaint_rate(window=WINDOW, cell_id=BIG_CELL).n_points == 72


def test_every_result_carries_its_inputs_and_caveats(service: InsightsService) -> None:
    results = (
        service.complaint_rate(window=WINDOW, cell_id=BIG_CELL),
        service.lead_lag(cell_id=BIG_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW),
        service.granger(cell_id=BIG_CELL, kpi=KpiName.AVAILABILITY, window=WINDOW),
        service.event_study(cell_ids=[BIG_CELL], window=WINDOW, event_at=EVENT_AT),
        service.correlation_matrix(window=WINDOW, region="Nairobi"),
        service.pca(window=WINDOW, region="Nairobi"),
    )
    for result in results:
        assert result.caveats, f"{type(result).__name__} carries no caveats"
        assert result.inputs["window_start"] == WINDOW.start.isoformat()
        assert result.inputs["granularity"] == "1h"
        # The clock is injected, never read ambiently.
        assert (
            result.inputs["computed_at"]
            == FixedClock(
                datetime(2026, 3, 17, 11, 30, tzinfo=UTC),
            )
            .now()
            .isoformat()
        )


# ====================================================== adapter-level checks


def test_complaints_are_bucketed_by_the_hour_in_sql(dataset) -> None:
    reader = SqlComplaintSeriesReader(dataset)
    points = reader.hourly_counts_by_cell(cell_id=BIG_CELL, window=WINDOW)
    assert all(point.bucket_start.minute == 0 for point in points)
    assert all(point.bucket_start.second == 0 for point in points)
    assert all(point.bucket_start.tzinfo is UTC for point in points)
    spike = next(point for point in points if point.bucket_start == SPIKE_AT)
    assert spike.value == float(SPIKE_COMPLAINTS)


def test_non_network_complaints_are_excluded(dataset) -> None:
    dataset.add(
        Complaint(
            id="CMP-BILLING-1",
            received_at=datetime(2026, 3, 16, 3, 0, tzinfo=UTC),
            source_ts=datetime(2026, 3, 16, 3, 0, tzinfo=UTC),
            channel="call_centre",
            language="en",
            raw_text="bundle question",
            region="Nairobi",
            cell_id=BIG_CELL,
            category="billing",
            status="received",
        ),
    )
    dataset.flush()
    reader = SqlComplaintSeriesReader(dataset)
    at_three = datetime(2026, 3, 16, 3, 0, tzinfo=UTC)
    network = reader.hourly_counts_by_cell(cell_id=BIG_CELL, window=WINDOW)
    everything = reader.hourly_counts_by_cell(
        cell_id=BIG_CELL,
        window=WINDOW,
        network_only=False,
    )
    assert all(point.bucket_start != at_three for point in network)
    assert any(point.bucket_start == at_three for point in everything)


def test_kpi_matrix_averages_the_cells_in_a_region(dataset) -> None:
    reader = SqlKpiSeriesReader(dataset)
    matrix = reader.hourly_matrix(
        window=TimeWindow(start=EVENT_AT, end=EVENT_AT + HOUR),
        kpis=[KpiName.AVAILABILITY],
        region="Nairobi",
    )
    assert matrix.bucket_starts == (EVENT_AT,)
    assert matrix.cells == 2
    assert matrix.column(KpiName.AVAILABILITY)[0] == pytest.approx(
        (DIP_AVAILABILITY + NORMAL_AVAILABILITY) / 2,
    )


def test_kpi_matrix_with_no_families_is_empty(dataset) -> None:
    reader = SqlKpiSeriesReader(dataset)
    matrix = reader.hourly_matrix(window=WINDOW, kpis=[])
    assert matrix.rows == ()
    assert matrix.n_points == 0


def test_kpi_matrix_with_an_empty_cell_list_is_empty(dataset) -> None:
    reader = SqlKpiSeriesReader(dataset)
    matrix = reader.hourly_matrix(window=WINDOW, kpis=[KpiName.LATENCY], cell_ids=[])
    assert matrix.rows == ()
    assert matrix.kpis == (KpiName.LATENCY,)


def test_exposure_reader_caches_and_sums(dataset) -> None:
    reader = SqlCellExposureReader(dataset)
    assert reader.subscribers_est(BIG_CELL) == BIG_SUBS
    assert reader.subscribers_est(BIG_CELL) == BIG_SUBS  # second call is memoised
    assert reader.subscribers_est("CELL-NOPE") is None
    assert reader.total_subscribers_est(region="Nairobi") == BIG_SUBS + SMALL_SUBS
    assert reader.total_subscribers_est() == BIG_SUBS + SMALL_SUBS + DRIVEN_SUBS
    assert reader.cell_ids(region="Mombasa") == (DRIVEN_CELL,)
    assert reader.cell_ids(limit=2) == (BIG_CELL, SMALL_CELL)
