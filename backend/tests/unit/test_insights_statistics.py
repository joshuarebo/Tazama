"""Pure-function tests for the insights statistics, against known answers.

Every series here is constructed so the right answer is known before the function runs: a
lag of exactly two hours, a step change of exactly ten points, a matrix whose first
principal component is exactly the two collinear columns. That is the only way a wrong
sign convention or an off-by-one lag fails loudly instead of looking plausible.
"""

from __future__ import annotations

import importlib
import math
import random
import sys
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tazama.core.errors import UpstreamUnavailable
from tazama.modules.insights import statistics as stats

#: 2026-01-05 is a Monday, so hour-of-week slot 0 starts here.
MONDAY = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)

#: A plain daily shape: quiet at night, busy in the evening.
DIURNAL: tuple[float, ...] = tuple(
    round(0.5 * (1.0 + math.sin(2.0 * math.pi * (hour - 13) / 24.0)), 4) for hour in range(24)
)


def _hours(count: int, *, start: datetime = MONDAY) -> tuple[datetime, ...]:
    return tuple(start + index * HOUR for index in range(count))


# =========================================================== normalisation


def test_normalise_per_1k_is_exact() -> None:
    assert stats.normalise_per_1k([0, 1, 2, 4], 2000) == (0.0, 0.5, 1.0, 2.0)


def test_normalise_per_1k_honours_a_different_denominator() -> None:
    assert stats.normalise_per_1k([3], 1500, per_subscribers=100.0) == (0.2,)


def test_normalise_makes_two_differently_sized_cells_comparable() -> None:
    """Identical customer experience, 10x the subscribers: the rates must match."""
    big = stats.normalise_per_1k([40], 20_000)
    small = stats.normalise_per_1k([4], 2_000)
    assert big == small == (2.0,)


def test_normalise_refuses_a_missing_denominator() -> None:
    with pytest.raises(ValueError, match="subscribers_est must be positive"):
        stats.normalise_per_1k([1, 2], 0)


def test_normalise_refuses_a_non_positive_scale() -> None:
    with pytest.raises(ValueError, match="per_subscribers must be positive"):
        stats.normalise_per_1k([1], 100, per_subscribers=0.0)


# ======================================================== de-seasonalisation


def test_hour_of_week_slots_are_monday_first() -> None:
    assert stats.hour_of_week(MONDAY) == 0
    assert stats.hour_of_week(MONDAY + 19 * HOUR) == 19
    assert stats.hour_of_week(MONDAY + 6 * 24 * HOUR + 23 * HOUR) == 167


def test_hour_of_week_profile_recovers_an_injected_daily_shape() -> None:
    timestamps = _hours(3 * 168)
    values = [DIURNAL[moment.hour] for moment in timestamps]
    profile = stats.hour_of_week_profile(timestamps, values)
    assert profile.slots_covered == 168
    assert profile.observations == 3 * 168
    assert profile.value_for(MONDAY + 19 * HOUR) == pytest.approx(DIURNAL[19])


def test_hour_of_week_profile_uses_the_median_not_the_mean() -> None:
    """One 100x spike must not bend the profile it is supposed to describe."""
    timestamps = _hours(3 * 168)
    values = [1.0] * len(timestamps)
    values[19] = 100.0  # Monday 19:00 in the first week only
    profile = stats.hour_of_week_profile(timestamps, values)
    assert profile.medians[19] == 1.0


def test_hour_of_week_profile_leaves_unobserved_slots_none() -> None:
    profile = stats.hour_of_week_profile(_hours(24), [1.0] * 24)
    assert profile.slots_covered == 24
    assert profile.medians[0] == 1.0
    assert profile.medians[25] is None


def test_deseasonalise_removes_the_daily_profile_exactly() -> None:
    timestamps = _hours(3 * 168)
    values = [DIURNAL[moment.hour] for moment in timestamps]
    result = stats.deseasonalise(timestamps, values)
    assert len(result.deseasonalised) == len(values)
    assert all(abs(value) < 1e-12 for value in result.deseasonalised)
    assert result.slots_missing == 0


def test_deseasonalise_keeps_a_real_anomaly() -> None:
    timestamps = _hours(3 * 168)
    values = [DIURNAL[moment.hour] for moment in timestamps]
    spike_index = 168 + 19  # second week, Monday 19:00
    values[spike_index] += 7.0
    result = stats.deseasonalise(timestamps, values)
    assert result.deseasonalised[spike_index] == pytest.approx(7.0)
    assert sum(1 for value in result.deseasonalised if abs(value) > 1e-9) == 1


def test_deseasonalisation_stops_evening_congestion_looking_like_a_trend() -> None:
    """Raw, the evening peak correlates with any evening indicator. Adjusted, it does not."""
    timestamps = _hours(3 * 168)
    evening = [1.0 if 17 <= moment.hour <= 21 else 0.0 for moment in timestamps]
    raw = [DIURNAL[moment.hour] for moment in timestamps]
    raw_correlation = stats.pearson(raw, evening)
    adjusted = stats.deseasonalise(timestamps, raw).deseasonalised
    assert raw_correlation is not None
    assert raw_correlation > 0.6
    # Nothing is left to correlate with: a perfectly seasonal series is entirely season.
    assert stats.pearson(adjusted, evening) is None


def test_deseasonalise_reports_slots_the_baseline_never_saw() -> None:
    baseline = stats.hour_of_week_profile(_hours(24), [1.0] * 24)
    later = _hours(24, start=MONDAY + 3 * 24 * HOUR)
    result = stats.deseasonalise(later, [5.0] * 24, profile=baseline)
    assert result.slots_missing == 24
    assert result.deseasonalised == tuple([5.0] * 24)  # left unadjusted, not zeroed


def test_deseasonalise_optional_preserves_gaps() -> None:
    timestamps = _hours(3 * 168)
    values: list[float | None] = [DIURNAL[moment.hour] for moment in timestamps]
    values[100] = None
    adjusted, profile = stats.deseasonalise_optional(timestamps, values)
    assert adjusted[100] is None
    assert profile.observations == len(values) - 1
    assert len(adjusted) == len(values)


# ============================================================== correlation


def test_pearson_known_values() -> None:
    assert stats.pearson([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0]) == pytest.approx(1.0)
    assert stats.pearson([1.0, 2.0, 3.0, 4.0], [8.0, 6.0, 4.0, 2.0]) == pytest.approx(-1.0)
    assert stats.pearson([1.0, 2.0, 3.0], [1.0, 3.0, 2.0]) == pytest.approx(0.5)


def test_pearson_is_none_for_a_constant_series() -> None:
    assert stats.pearson([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0]) is None


def test_pearson_is_none_for_too_few_points() -> None:
    assert stats.pearson([1.0, 2.0], [2.0, 4.0]) is None


def test_pearson_drops_nans_and_nones_pairwise() -> None:
    left = [1.0, 2.0, float("nan"), 3.0, 4.0]
    right = [2.0, 4.0, 99.0, None, 8.0]
    # Only (1,2), (2,4) and (4,8) survive, which is still a perfect line.
    assert stats.pearson(left, right) == pytest.approx(1.0)


def test_pearson_handles_an_all_zero_series() -> None:
    assert stats.pearson([0.0] * 10, list(range(10))) is None


def test_average_ranks_shares_ties() -> None:
    assert stats.average_ranks([10.0, 20.0, 20.0, 30.0]) == (1.0, 2.5, 2.5, 4.0)


def test_spearman_sees_a_monotone_non_linear_relationship() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [1.0, 8.0, 27.0, 64.0, 125.0]
    pearson_value = stats.pearson(x, y)
    assert stats.spearman(x, y) == pytest.approx(1.0)
    assert pearson_value is not None
    assert pearson_value < 0.98


def test_spearman_is_none_for_too_few_points() -> None:
    assert stats.spearman([1.0], [1.0]) is None


# ================================================== lagged cross-correlation


def test_cross_correlation_finds_complaints_lagging_the_kpi_by_two_hours() -> None:
    """The sign-convention pin: complaints that follow the KPI by 2h give lag **+2**."""
    kpi = [math.sin(2.0 * math.pi * index / 24.0) for index in range(96)]
    # complaints[t] reproduces kpi[t - 2]: the network moved first.
    complaints = [0.0, 0.0, *kpi[:-2]]
    outcome = stats.cross_correlation(kpi, complaints, max_lag=6)
    assert outcome.best_lag == 2
    assert outcome.best_correlation == pytest.approx(1.0, abs=1e-9)
    assert outcome.leads is True


def test_cross_correlation_reports_a_negative_lag_when_complaints_move_first() -> None:
    kpi = [math.sin(2.0 * math.pi * index / 24.0) for index in range(96)]
    complaints = [*kpi[2:], 0.0, 0.0]
    outcome = stats.cross_correlation(kpi, complaints, max_lag=6)
    assert outcome.best_lag == -2
    assert outcome.leads is False


def test_cross_correlation_reports_zero_lag_for_simultaneous_series() -> None:
    kpi = [math.sin(2.0 * math.pi * index / 24.0) for index in range(96)]
    outcome = stats.cross_correlation(kpi, list(kpi), max_lag=6)
    assert outcome.best_lag == 0
    assert outcome.best_correlation == pytest.approx(1.0)
    assert outcome.leads is False
    assert outcome.zero_lag_correlation == pytest.approx(1.0)


def test_cross_correlation_keeps_the_lag_when_the_relationship_is_negative() -> None:
    """Availability falls while complaints rise: the lead must still be found at +2."""
    kpi = [0.0] * 40
    kpi[10] = -10.0
    complaints = [0.0] * 40
    complaints[12] = 4.0
    outcome = stats.cross_correlation(kpi, complaints, max_lag=6)
    assert outcome.best_lag == 2
    assert outcome.best_correlation == pytest.approx(-1.0)
    assert outcome.leads is True


def test_cross_correlation_scans_the_whole_requested_range() -> None:
    outcome = stats.cross_correlation(list(range(50)), list(range(50)), max_lag=4)
    assert outcome.lags == (-4, -3, -2, -1, 0, 1, 2, 3, 4)
    assert len(outcome.correlations) == 9


def test_cross_correlation_of_a_constant_series_is_all_none() -> None:
    outcome = stats.cross_correlation([1.0] * 30, list(range(30)), max_lag=3)
    assert set(outcome.correlations) == {None}
    assert outcome.best_lag is None
    assert outcome.best_correlation is None
    assert outcome.leads is False


def test_cross_correlation_of_a_single_point_is_undefined() -> None:
    outcome = stats.cross_correlation([1.0], [2.0], max_lag=3)
    assert outcome.best_lag is None
    assert outcome.n_points == 1


def test_cross_correlation_of_an_all_zero_complaint_series_is_undefined() -> None:
    outcome = stats.cross_correlation(list(range(40)), [0.0] * 40, max_lag=4)
    assert outcome.best_correlation is None


def test_cross_correlation_refuses_a_negative_lag_range() -> None:
    with pytest.raises(ValueError, match="max_lag must be non-negative"):
        stats.cross_correlation([1.0, 2.0], [1.0, 2.0], max_lag=-1)


# ============================================================ correlation table


def test_correlation_table_is_symmetric_with_a_unit_diagonal() -> None:
    x = [float(index) for index in range(20)]
    table = stats.correlation_table(
        ["x", "two_x", "minus_x"], [x, [2 * v for v in x], [-v for v in x]]
    )
    assert table.pearson[0][0] == 1.0
    assert table.pearson[0][1] == pytest.approx(1.0)
    assert table.pearson[0][2] == pytest.approx(-1.0)
    assert table.pearson[1][0] == table.pearson[0][1]
    assert table.n_points == 20


def test_correlation_table_ranks_by_absolute_strength() -> None:
    x = [float(index) for index in range(20)]
    noisy = [float((index * 7) % 5) for index in range(20)]
    table = stats.correlation_table(["x", "minus_x", "noisy"], [x, [-v for v in x], noisy], top=2)
    assert len(table.ranked) == 2
    assert table.ranked[0][:2] == ("x", "minus_x")
    assert table.ranked[0][2] == pytest.approx(-1.0)


def test_correlation_table_refuses_mismatched_labels() -> None:
    with pytest.raises(ValueError, match="same length"):
        stats.correlation_table(["a"], [[1.0], [2.0]])


# ============================================================ Granger-style test


def _granger_pair(*, related: bool, n: int = 300) -> tuple[list[float], list[float]]:
    rng = random.Random(11)
    cause = [rng.gauss(0.0, 1.0) for _ in range(n)]
    effect = [rng.gauss(0.0, 1.0)]
    for index in range(1, n):
        driver = 0.8 * cause[index - 1] if related else 0.0
        effect.append(driver + 0.3 * rng.gauss(0.0, 1.0))
    return cause, effect


def test_granger_detects_a_known_predictive_relationship() -> None:
    cause, effect = _granger_pair(related=True)
    outcome = stats.granger_f_test(cause, effect, max_lag=4)
    assert outcome.usable
    assert outcome.p_value is not None
    assert outcome.p_value < 1e-6
    assert outcome.f_statistic is not None
    assert outcome.f_statistic > 20.0
    assert outcome.lag in {1, 2, 3, 4}


def test_granger_finds_nothing_between_independent_series() -> None:
    cause, effect = _granger_pair(related=False)
    outcome = stats.granger_f_test(cause, effect, max_lag=4)
    assert outcome.usable
    assert outcome.p_value is not None
    assert outcome.p_value > 0.05


def test_granger_returns_not_enough_signal_for_a_constant_series() -> None:
    outcome = stats.granger_f_test([1.0] * 100, list(range(100)), max_lag=3)
    assert not outcome.usable
    assert "constant" in outcome.reason


def test_granger_returns_not_enough_signal_for_too_few_points() -> None:
    outcome = stats.granger_f_test([1.0, 2.0], [2.0, 1.0], max_lag=2)
    assert not outcome.usable
    assert "fewer than 4 points" in outcome.reason


def test_granger_returns_not_enough_signal_for_perfectly_collinear_inputs() -> None:
    """cause == effect makes the unrestricted design singular; that is not evidence."""
    rng = random.Random(3)
    series = [rng.gauss(0.0, 1.0) for _ in range(120)]
    outcome = stats.granger_f_test(series, list(series), max_lag=3)
    assert not outcome.usable
    assert "not enough signal" in outcome.reason


def test_granger_returns_not_enough_signal_when_no_lag_fits() -> None:
    rng = random.Random(5)
    short = [rng.gauss(0.0, 1.0) for _ in range(6)]
    outcome = stats.granger_f_test(short, [rng.gauss(0.0, 1.0) for _ in range(6)], max_lag=6)
    assert not outcome.usable
    assert "not enough signal" in outcome.reason


def test_granger_drops_gaps_and_says_so() -> None:
    cause, effect = _granger_pair(related=True)
    cause_with_gap: list[float | None] = list(cause)
    cause_with_gap[17] = None
    outcome = stats.granger_f_test(cause_with_gap, effect, max_lag=3)
    assert outcome.n_used == len(cause) - 1
    assert "gaps" in outcome.reason


def test_granger_refuses_a_zero_lag_range() -> None:
    with pytest.raises(ValueError, match="max_lag must be at least 1"):
        stats.granger_f_test([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], max_lag=0)


# ================================================================ event study


def test_event_study_finds_a_known_step_change() -> None:
    offsets = tuple(range(-24, 25))
    row = tuple(100.0 if offset < 0 else 90.0 for offset in offsets)
    profile = stats.event_study(offsets, [row])
    assert profile.pre_event_baseline == pytest.approx(100.0)
    assert profile.peak_deviation == pytest.approx(-10.0)
    assert profile.peak_offset == 0
    assert profile.series_used == 1


def test_event_study_averages_several_series() -> None:
    offsets = (-2, -1, 0, 1)
    rows = [(1.0, 1.0, 5.0, 1.0), (3.0, 3.0, 7.0, 3.0)]
    profile = stats.event_study(offsets, rows)
    assert profile.mean_series == (2.0, 2.0, 6.0, 2.0)
    assert profile.pre_event_baseline == pytest.approx(2.0)
    assert profile.peak_deviation == pytest.approx(4.0)
    assert profile.peak_offset == 0


def test_event_study_counts_observations_per_offset() -> None:
    offsets = (-1, 0, 1)
    rows: list[tuple[float | None, ...]] = [(1.0, None, 3.0), (None, None, 5.0)]
    profile = stats.event_study(offsets, rows)
    assert profile.counts == (1, 0, 2)
    assert profile.mean_series[1] is None


def test_event_study_without_a_pre_event_baseline_reports_none() -> None:
    offsets = (0, 1, 2)
    profile = stats.event_study(offsets, [(1.0, 2.0, 3.0)])
    assert profile.pre_event_baseline is None
    assert profile.peak_deviation is None
    assert profile.peak_offset is None


def test_event_study_can_use_the_whole_window_as_a_baseline() -> None:
    offsets = (0, 1, 2)
    profile = stats.event_study(offsets, [(1.0, 2.0, 3.0)], pre_event_only=False)
    assert profile.pre_event_baseline == pytest.approx(2.0)
    assert profile.peak_offset in {0, 2}


# ======================================================================= PCA


def test_pca_finds_a_known_single_principal_component() -> None:
    """Two collinear columns: one component explains everything, loadings are 1/sqrt(2)."""
    x = [float(index) for index in range(30)]
    decomposition = stats.principal_components(["x", "two_x"], [x, [2.0 * v for v in x]])
    assert decomposition.explained_variance_ratio[0] == pytest.approx(1.0, abs=1e-12)
    assert decomposition.cumulative_variance[0] == pytest.approx(1.0)
    first = decomposition.loadings[0]
    assert first[0] == pytest.approx(1 / math.sqrt(2), abs=1e-9)
    assert first[1] == pytest.approx(1 / math.sqrt(2), abs=1e-9)
    assert decomposition.components_for(0.8) == 1
    assert decomposition.components_for(0.9) == 1
    assert decomposition.n_used == 30


def test_pca_splits_variance_evenly_between_orthogonal_columns() -> None:
    x = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    y = [1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0]
    decomposition = stats.principal_components(["x", "y"], [x, y])
    assert decomposition.explained_variance_ratio == pytest.approx((0.5, 0.5))
    assert decomposition.components_for(0.9) == 2


def test_pca_drops_a_constant_column() -> None:
    x = [float(index) for index in range(20)]
    decomposition = stats.principal_components(["x", "flat"], [x, [7.0] * 20])
    assert decomposition.dropped_labels == ("flat",)
    assert decomposition.loadings[0][1] == 0.0


def test_pca_refuses_too_few_complete_rows() -> None:
    decomposition = stats.principal_components(["x", "y"], [[1.0, 2.0], [2.0, 4.0]])
    assert decomposition.explained_variance_ratio == ()
    assert "fewer than 3 complete observations" in decomposition.reason


def test_pca_reports_an_entirely_constant_matrix() -> None:
    decomposition = stats.principal_components(["a", "b"], [[1.0] * 10, [2.0] * 10])
    assert "every column is constant" in decomposition.reason
    assert decomposition.dropped_labels == ("a", "b")


def test_pca_ignores_rows_with_a_gap() -> None:
    x: list[float | None] = [float(index) for index in range(20)]
    y: list[float | None] = [2.0 * index for index in range(20)]
    x[5] = None
    decomposition = stats.principal_components(["x", "y"], [x, y])
    assert decomposition.n_used == 19


def test_pca_refuses_mismatched_labels() -> None:
    with pytest.raises(ValueError, match="same length"):
        stats.principal_components(["only_one"], [[1.0], [2.0]])


def test_pca_of_no_columns_is_not_enough_signal() -> None:
    decomposition = stats.principal_components([], [])
    assert "no columns" in decomposition.reason


# ====================================================== mutual information


def test_mutual_information_catches_a_non_linear_relationship() -> None:
    """A squared relationship has ~zero correlation and clearly non-zero MI."""
    rng = random.Random(7)
    target = [rng.uniform(-3.0, 3.0) for _ in range(300)]
    squared = [value**2 for value in target]
    noise = [rng.uniform(-3.0, 3.0) for _ in range(300)]
    scores = stats.mutual_information([squared, noise], target)
    linear = stats.pearson(squared, target)
    assert scores[0] > scores[1]
    assert scores[0] > 0.1
    assert linear is not None
    assert abs(linear) < 0.2


def test_mutual_information_of_a_constant_target_is_zero() -> None:
    assert stats.mutual_information([[1.0, 2.0, 3.0, 4.0, 5.0]], [2.0] * 5) == (0.0,)


def test_mutual_information_of_too_few_points_is_zero() -> None:
    assert stats.mutual_information([[1.0, 2.0]], [1.0, 3.0]) == (0.0,)


def test_mutual_information_of_no_columns_is_empty() -> None:
    assert stats.mutual_information([], [1.0, 2.0]) == ()


def test_mutual_information_is_deterministic() -> None:
    rng = random.Random(2)
    target = [rng.uniform(0.0, 1.0) for _ in range(120)]
    column = [value * 2.0 for value in target]
    first = stats.mutual_information([column], target)
    second = stats.mutual_information([column], target)
    assert first == second


# ============================================ optional-dependency guarding


class _BlockingFinder:
    """A meta-path hook that makes the `ml` extra look uninstalled."""

    def __init__(self, blocked: frozenset[str]) -> None:
        self._blocked = blocked

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname.split(".")[0] in self._blocked:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)


def test_insights_imports_and_degrades_without_the_ml_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module must import on a bare install and fail only when an analysis needs numpy."""
    blocked = frozenset({"numpy", "scipy", "sklearn", "pandas", "statsmodels"})
    for name in [n for n in sys.modules if n.split(".")[0] in blocked]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [n for n in sys.modules if n.startswith("tazama.modules.insights")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockingFinder(blocked), *sys.meta_path])

    bare = importlib.import_module("tazama.modules.insights.statistics")
    # Pure-Python analyses keep working: the lead-lag measurement needs no numpy at all.
    assert bare.normalise_per_1k([2], 1000) == (2.0,)
    assert (
        bare.cross_correlation([1.0, 2.0, 3.0, 2.0], [1.0, 2.0, 3.0, 2.0], max_lag=1).best_lag == 0
    )

    with pytest.raises(UpstreamUnavailable, match="bootstrap-ml"):
        bare.granger_f_test([1.0, 2.0, 3.0, 4.0] * 20, [2.0, 1.0, 4.0, 3.0] * 20, max_lag=2)
    with pytest.raises(UpstreamUnavailable, match="bootstrap-ml"):
        bare.principal_components(["a", "b"], [[1.0, 2.0, 3.0], [2.0, 1.0, 5.0]])
    with pytest.raises(UpstreamUnavailable, match="bootstrap-ml"):
        bare.mutual_information([[1.0, 2.0, 3.0]], [1.0, 2.0, 3.0])


# ====================================================== property-based checks


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
            st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        ),
        min_size=3,
        max_size=80,
    ),
)
def test_property_correlation_is_bounded(pairs: list[tuple[float, float]]) -> None:
    """A correlation coefficient outside [-1, 1] is always a bug, for any input."""
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    for value in (stats.pearson(left, right), stats.spearman(left, right)):
        assert value is None or -1.0 <= value <= 1.0
    outcome = stats.cross_correlation(left, right, max_lag=3)
    assert all(value is None or -1.0 <= value <= 1.0 for value in outcome.correlations)


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=200,
    ),
)
def test_property_deseasonalisation_preserves_length_and_finiteness(values: list[float]) -> None:
    timestamps = _hours(len(values))
    result = stats.deseasonalise(timestamps, values)
    assert len(result.deseasonalised) == len(values)
    assert all(math.isfinite(value) for value in result.deseasonalised)
    assert len(result.profile.medians) == 168
