"""Pure statistics for the insights module: no I/O, no database, no clock.

Everything here takes plain sequences of floats (and, where seasonality is involved,
plain `datetime`s) and returns plain numbers or small frozen records. That is the point:
a cross-correlation that gets its sign convention backwards, or a de-seasonalisation that
is off by one hour-of-week slot, is a bug you can only catch by feeding the function a
series whose answer you already know. `tests/unit/test_insights_statistics.py` does
exactly that, and nothing in this file needs a fixture to run.

**On UMAP and t-SNE: deliberately not implemented.** PRD section 9 allows them for
visualisation only and forbids them for feature selection, and that restriction is hard to
enforce once the function exists. Both are non-parametric embeddings with no `transform`
you can trust on new data, no stable axes between runs, and distances that are not
metric-preserving — so a "distance" in a UMAP plot cannot be fed to a model, used to rank
drivers, or compared across two windows. Anything a UMAP plot would show about the KPI
families is already available here in a form that can be audited: PCA gives orthogonal
axes with interpretable loadings and an explained-variance figure, the correlation matrix
gives the pairwise structure, and mutual information gives the non-linear dependence on
the complaint rate. If a visual embedding is wanted later it belongs in a notebook under
`docs/`, not in a module the pipeline imports.

**Optional dependencies.** numpy, scipy and scikit-learn live in the `ml` extra, so this
module imports with none of them installed. The functions that genuinely need them
(`granger_f_test`, `principal_components`, `mutual_information`) import on call and raise
`UpstreamUnavailable` naming `make bootstrap-ml`. Normalisation, de-seasonalisation,
cross-correlation, the correlation table and the event study are pure Python and always
available — which also means the "the network sees it first" measurement keeps working on
a bare install.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tazama.core.errors import UpstreamUnavailable

#: Slots in the hour-of-week profile. Mirrors `models.HOURS_PER_WEEK`; duplicated as a
#: literal here so this file depends on nothing but the standard library.
HOURS_PER_WEEK = 168

_BOOTSTRAP_HINT = "run `make bootstrap-ml` to install the optional `ml` extra"


def _require(module: str) -> Any:
    """Import an `ml`-extra dependency on first use, or explain how to install it."""
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via monkeypatch
        raise UpstreamUnavailable(
            f"this insights analysis needs {module}, which is not installed: {_BOOTSTRAP_HINT}.",
        ) from exc


# --------------------------------------------------------------------- helpers


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def _finite(value: float | None) -> bool:
    return value is not None and not math.isnan(value) and not math.isinf(value)


def _clean_pairs(
    left: Sequence[float | None],
    right: Sequence[float | None],
) -> tuple[list[float], list[float]]:
    """Keep only the index positions where both series carry a finite number."""
    kept_left: list[float] = []
    kept_right: list[float] = []
    for a, b in zip(left, right, strict=False):
        if _finite(a) and _finite(b):
            kept_left.append(float(a))  # type: ignore[arg-type]
            kept_right.append(float(b))  # type: ignore[arg-type]
    return kept_left, kept_right


# -------------------------------------------------------------- normalisation


def normalise_per_1k(
    counts: Sequence[float],
    subscribers_est: int,
    *,
    per_subscribers: float = 1000.0,
) -> tuple[float, ...]:
    """Counts per `per_subscribers` subscribers, bucket by bucket.

    **A raw count is never comparable across cells.** A Westlands macro cell carrying
    4,000 subscribers and a rural small cell carrying 120 will produce wildly different
    complaint counts for identical customer experience, and any model, heatmap or ranking
    built on raw counts simply rediscovers cell size. Dividing by `cell.subscribers_est`
    is what makes "this cell is unusually bad" a statement about the network rather than
    about the population behind it. The per-hour part is the bucket grid: each element of
    `counts` is one hourly bucket, so the result is already a per-hour rate.

    `subscribers_est` must be positive; a cell with no estimate is a data-quality problem
    for the caller to record as a caveat, not something to paper over with a zero.
    """
    if subscribers_est <= 0:
        raise ValueError("subscribers_est must be positive to normalise a count series")
    if per_subscribers <= 0:
        raise ValueError("per_subscribers must be positive")
    scale = per_subscribers / float(subscribers_est)
    return tuple(float(count) * scale for count in counts)


# ------------------------------------------------------------ de-seasonalisation


def hour_of_week(moment: datetime) -> int:
    """Slot 0..167 for an instant: Monday 00:00 UTC is 0, Sunday 23:00 UTC is 167.

    Indexed in UTC because every stored timestamp is UTC. Africa/Nairobi is a fixed
    UTC+3 with no DST, so a UTC hour-of-week profile and a local one differ only by a
    constant rotation of the index — the shape the de-seasonalisation removes is the same.
    """
    return moment.weekday() * 24 + moment.hour


@dataclass(frozen=True, slots=True)
class SeasonalProfile:
    """Per-hour-of-week medians, with the slots that had no data left as None."""

    medians: tuple[float | None, ...]
    slots_covered: int
    observations: int

    def value_for(self, moment: datetime) -> float | None:
        return self.medians[hour_of_week(moment)]


def hour_of_week_profile(
    timestamps: Sequence[datetime],
    values: Sequence[float | None],
) -> SeasonalProfile:
    """Median value per hour-of-week slot over a baseline window.

    The **median**, not the mean: the baseline window contains the very outages whose
    effect is being measured, and a mean would let a single 10x spike bend the profile it
    is supposed to describe away from the typical day.
    """
    buckets: list[list[float]] = [[] for _ in range(HOURS_PER_WEEK)]
    observations = 0
    for moment, value in zip(timestamps, values, strict=False):
        if not _finite(value):
            continue
        buckets[hour_of_week(moment)].append(float(value))  # type: ignore[arg-type]
        observations += 1
    medians = tuple(_median(bucket) if bucket else None for bucket in buckets)
    return SeasonalProfile(
        medians=medians,
        slots_covered=sum(1 for bucket in buckets if bucket),
        observations=observations,
    )


@dataclass(frozen=True, slots=True)
class DeseasonalisedSeries:
    """Raw and de-seasonalised values over the same bucket grid, plus the profile used."""

    values: tuple[float, ...]
    deseasonalised: tuple[float, ...]
    profile: SeasonalProfile
    slots_missing: int


def deseasonalise(
    timestamps: Sequence[datetime],
    values: Sequence[float],
    *,
    profile: SeasonalProfile | None = None,
) -> DeseasonalisedSeries:
    """Remove the daily *and* weekly shape before anything is correlated.

    Both cycles are removed at once by subtracting a per-hour-of-week median: slot 0 is
    Monday 00:00, slot 139 is Saturday 19:00, and the 168 medians therefore encode the
    evening peak, the 03:00 trough and the quiet weekend in a single profile. Removing a
    daily profile alone would leave the weekly one behind, and vice versa.

    Why this matters: traffic, complaints, PRB utilisation and latency all peak in the
    evening. Correlate them raw and every pair is strongly related — you have measured
    the clock, not the network. After subtraction, the surviving co-movement is the part
    that is *not* explained by the time of day or the day of week, which is the only part
    that can point at an incident. **Evening congestion must not look like a weekly
    trend.**

    Subtraction (additive), not division (multiplicative): the series this is applied to
    are rates that are legitimately zero in quiet hours, and dividing by a near-zero
    median turns one complaint at 04:00 into a 40-sigma event.

    Slots the baseline never observed are left at their raw value and counted in
    `slots_missing`, so the caller can say so in a caveat instead of silently mixing
    adjusted and unadjusted points.
    """
    effective = profile if profile is not None else hour_of_week_profile(timestamps, values)
    adjusted: list[float] = []
    missing = 0
    for moment, value in zip(timestamps, values, strict=False):
        median = effective.medians[hour_of_week(moment)]
        if median is None:
            missing += 1
            adjusted.append(float(value))
        else:
            adjusted.append(float(value) - median)
    return DeseasonalisedSeries(
        values=tuple(float(value) for value in values),
        deseasonalised=tuple(adjusted),
        profile=effective,
        slots_missing=missing,
    )


# ------------------------------------------------------------------ correlation


def pearson(left: Sequence[float | None], right: Sequence[float | None]) -> float | None:
    """Pearson correlation over the positions where both series are finite.

    Returns None rather than raising when the overlap is shorter than three points or
    either series is constant: a constant series has no variance to share, and a
    correlation of 0.0 would be a lie about a situation where the answer is "undefined".
    """
    x, y = _clean_pairs(left, right)
    if len(x) < 3:
        return None
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    dx = [value - mean_x for value in x]
    dy = [value - mean_y for value in y]
    var_x = sum(value * value for value in dx)
    var_y = sum(value * value for value in dy)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    covariance = sum(a * b for a, b in zip(dx, dy, strict=True))
    value = covariance / math.sqrt(var_x * var_y)
    # Floating point can push a perfect correlation a hair outside [-1, 1].
    return max(-1.0, min(1.0, value))


def average_ranks(values: Sequence[float]) -> tuple[float, ...]:
    """Ranks 1..n with ties sharing their average rank (what Spearman requires)."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2.0 + 1.0
        for index in order[position : end + 1]:
            ranks[index] = shared
        position = end + 1
    return tuple(ranks)


def spearman(left: Sequence[float | None], right: Sequence[float | None]) -> float | None:
    """Spearman rank correlation: Pearson over average ranks, on the common support."""
    x, y = _clean_pairs(left, right)
    if len(x) < 3:
        return None
    return pearson(average_ranks(x), average_ranks(y))


@dataclass(frozen=True, slots=True)
class CrossCorrelation:
    """Correlation at every lag in the scanned range, plus the best one."""

    lags: tuple[int, ...]
    correlations: tuple[float | None, ...]
    best_lag: int | None
    best_correlation: float | None
    zero_lag_correlation: float | None
    n_points: int
    overlap_at_best: int

    @property
    def leads(self) -> bool:
        """True when the first series moved *before* the second."""
        return self.best_lag is not None and self.best_lag > 0


def cross_correlation(
    leading: Sequence[float | None],
    following: Sequence[float | None],
    *,
    max_lag: int = 6,
    min_overlap: int = 3,
) -> CrossCorrelation:
    """Lagged cross-correlation, scanning `-max_lag .. +max_lag`.

    **Sign convention.** At lag `L >= 0` the correlation is computed between
    `leading[t]` and `following[t + L]`: the second series is shifted *later* by L
    buckets before being compared. So

      * `L = +2` scoring highest means `following` reproduces `leading` two hours
        afterwards — the KPI moved first, complaints arrived two hours later. This is the
        "the network sees it first" case, and `CrossCorrelation.leads` is True.
      * `L = 0` highest means they move together, which usually means the KPI degradation
        and the complaints are both reactions to something already in progress.
      * `L = -2` highest means complaints preceded the KPI move — worth investigating as
        a measurement or aggregation artefact (or a KPI that only breaks once load from
        retrying customers arrives).

    Call it as `cross_correlation(kpi_series, complaint_rate_series)`; reversing the
    arguments reverses every sign, which is why the service always passes the KPI first.

    The best lag is the one with the largest **absolute** correlation, because a KPI whose
    polarity makes degradation a *fall* (availability, SINR, throughput) correlates
    negatively with complaints and that is the expected, meaningful direction.

    Lags whose shifted overlap falls below `min_overlap`, or where either overlapping
    window is constant, come back as None instead of a fabricated number.
    """
    if max_lag < 0:
        raise ValueError("max_lag must be non-negative")
    count = min(len(leading), len(following))
    lags = tuple(range(-max_lag, max_lag + 1))
    correlations: list[float | None] = []
    overlaps: list[int] = []
    for lag in lags:
        if lag >= 0:
            window_a = leading[: count - lag] if lag else leading[:count]
            window_b = following[lag:count]
        else:
            window_a = leading[-lag:count]
            window_b = following[: count + lag]
        overlaps.append(min(len(window_a), len(window_b)))
        if min(len(window_a), len(window_b)) < min_overlap:
            correlations.append(None)
            continue
        correlations.append(pearson(window_a, window_b))

    scored = [(index, value) for index, value in enumerate(correlations) if value is not None]
    if not scored:
        return CrossCorrelation(
            lags=lags,
            correlations=tuple(correlations),
            best_lag=None,
            best_correlation=None,
            zero_lag_correlation=None,
            n_points=count,
            overlap_at_best=0,
        )
    # Ties break towards the smaller |lag| and then the smaller lag, so a flat profile
    # reports "they move together" rather than an arbitrary offset.
    best_index, best_value = max(
        scored,
        key=lambda item: (abs(item[1]), -abs(lags[item[0]]), -lags[item[0]]),
    )
    return CrossCorrelation(
        lags=lags,
        correlations=tuple(correlations),
        best_lag=lags[best_index],
        best_correlation=best_value,
        zero_lag_correlation=correlations[lags.index(0)],
        n_points=count,
        overlap_at_best=overlaps[best_index],
    )


@dataclass(frozen=True, slots=True)
class CorrelationTable:
    """Square Pearson and Spearman matrices over labelled columns."""

    labels: tuple[str, ...]
    pearson: tuple[tuple[float | None, ...], ...]
    spearman: tuple[tuple[float | None, ...], ...]
    ranked: tuple[tuple[str, str, float | None, float | None], ...]
    n_points: int


def correlation_table(
    labels: Sequence[str],
    columns: Sequence[Sequence[float | None]],
    *,
    top: int = 10,
) -> CorrelationTable:
    """Pairwise Pearson and Spearman for every pair of columns, plus a ranked list.

    The ranked list exists so the UI never has to scan an n x n matrix to find the
    interesting cells: it is the upper triangle sorted by |Pearson| descending.
    """
    if len(labels) != len(columns):
        raise ValueError("labels and columns must be the same length")
    size = len(labels)
    pearson_rows: list[list[float | None]] = [[None] * size for _ in range(size)]
    spearman_rows: list[list[float | None]] = [[None] * size for _ in range(size)]
    pairs: list[tuple[str, str, float | None, float | None]] = []
    for i in range(size):
        pearson_rows[i][i] = 1.0
        spearman_rows[i][i] = 1.0
        for j in range(i + 1, size):
            p = pearson(columns[i], columns[j])
            s = spearman(columns[i], columns[j])
            pearson_rows[i][j] = pearson_rows[j][i] = p
            spearman_rows[i][j] = spearman_rows[j][i] = s
            pairs.append((labels[i], labels[j], p, s))
    pairs.sort(
        key=lambda item: (-(abs(item[2]) if item[2] is not None else -1.0), item[0], item[1])
    )
    n_points = min((len(column) for column in columns), default=0)
    return CorrelationTable(
        labels=tuple(labels),
        pearson=tuple(tuple(row) for row in pearson_rows),
        spearman=tuple(tuple(row) for row in spearman_rows),
        ranked=tuple(pairs[: max(0, top)]),
        n_points=n_points,
    )


# -------------------------------------------------------- Granger-style causality


@dataclass(frozen=True, slots=True)
class GrangerOutcome:
    """Result of the lagged-OLS F-test, or a stated reason why it could not be run."""

    f_statistic: float | None
    p_value: float | None
    lag: int | None
    n_used: int
    tested_lags: tuple[int, ...]
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.f_statistic is not None and self.p_value is not None


def _ols_residual_sum(design: Any, target: Any, numpy_module: Any) -> float | None:
    """Least-squares residual sum of squares, or None if the design is degenerate."""
    np = numpy_module
    rank = int(np.linalg.matrix_rank(design))
    if rank < design.shape[1]:
        # Perfectly collinear columns: the "improvement" from adding the candidate cause
        # would be an artefact of a singular design, not evidence.
        return None
    coefficients, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
    residuals = target - design @ coefficients
    return float(np.dot(residuals, residuals))


def granger_f_test(
    cause: Sequence[float | None],
    effect: Sequence[float | None],
    *,
    max_lag: int = 6,
) -> GrangerOutcome:
    """Granger-style test: does the past of `cause` improve a forecast of `effect`?

    **Granger "causality" is predictive precedence, not causation.** The test compares two
    autoregressions of `effect`:

        restricted:   effect[t] ~ c + effect[t-1..t-L]
        unrestricted: effect[t] ~ c + effect[t-1..t-L] + cause[t-1..t-L]

    and reports the F statistic for the joint hypothesis that the `cause` coefficients are
    all zero. A small p-value says the KPI's recent past carries information about
    complaints that complaints' own recent past does not. It does **not** rule out a
    common upstream driver (a transmission link failing degrades the KPI and generates
    complaints), reverse causation at a finer time resolution, or a confounder such as a
    mass-market event. It is a screening tool: it tells an analyst which KPI is worth
    opening next, and nothing more.

    The lag is chosen as the one with the smallest p-value over `1..max_lag`. That is a
    multiple-comparison search, so the reported p-value is optimistic; the caller records
    that as a caveat.

    Degenerate inputs return a "not enough signal" outcome rather than raising: a constant
    series (either one), fewer usable points than the regression needs, non-finite values,
    or a perfectly collinear design. A screening tool that explodes on a quiet cell is
    useless, because most cells are quiet.
    """
    if max_lag < 1:
        raise ValueError("max_lag must be at least 1")
    np = _require("numpy")
    stats = _require("scipy.stats")

    cause_clean, effect_clean = _clean_pairs(cause, effect)
    n = len(cause_clean)
    if n != len(cause) or n != len(effect):
        gaps_note = "series contained gaps or non-finite values; they were dropped"
    else:
        gaps_note = ""
    tested: list[int] = []
    if n < 4:
        return GrangerOutcome(None, None, None, n, (), "not enough signal: fewer than 4 points")
    x = np.asarray(cause_clean, dtype=float)
    y = np.asarray(effect_clean, dtype=float)
    if float(np.std(x)) <= 0.0 or float(np.std(y)) <= 0.0:
        return GrangerOutcome(
            None,
            None,
            None,
            n,
            (),
            "not enough signal: one of the series is constant",
        )

    best: tuple[float, float, int] | None = None
    for lag in range(1, max_lag + 1):
        rows = n - lag
        # Each lag costs 2*lag + 1 parameters in the unrestricted model; demand a few
        # residual degrees of freedom on top, or the F statistic is meaningless.
        if rows < 2 * lag + 5:
            continue
        target = y[lag:]
        intercept = np.ones((rows, 1))
        effect_lags = np.column_stack([y[lag - k : n - k] for k in range(1, lag + 1)])
        cause_lags = np.column_stack([x[lag - k : n - k] for k in range(1, lag + 1)])
        restricted = np.column_stack([intercept, effect_lags])
        unrestricted = np.column_stack([restricted, cause_lags])
        rss_r = _ols_residual_sum(restricted, target, np)
        rss_u = _ols_residual_sum(unrestricted, target, np)
        if rss_r is None or rss_u is None:
            continue
        df_denominator = rows - unrestricted.shape[1]
        if df_denominator <= 0 or rss_u <= 0.0:
            # A zero residual means the unrestricted model fits exactly: with this little
            # data that is interpolation, not evidence.
            continue
        tested.append(lag)
        f_statistic = ((rss_r - rss_u) / lag) / (rss_u / df_denominator)
        if f_statistic < 0.0:
            continue
        p_value = float(stats.f.sf(f_statistic, lag, df_denominator))
        if best is None or p_value < best[1]:
            best = (float(f_statistic), p_value, lag)

    if best is None:
        return GrangerOutcome(
            None,
            None,
            None,
            n,
            tuple(tested),
            "not enough signal: no lag had enough usable points for a stable regression",
        )
    return GrangerOutcome(
        f_statistic=best[0],
        p_value=best[1],
        lag=best[2],
        n_used=n,
        tested_lags=tuple(tested),
        reason=gaps_note,
    )


# ----------------------------------------------------------------- event study


@dataclass(frozen=True, slots=True)
class EventProfile:
    """Mean series relative to an event, with the pre-event baseline and the peak."""

    offsets: tuple[int, ...]
    mean_series: tuple[float | None, ...]
    counts: tuple[int, ...]
    pre_event_baseline: float | None
    peak_deviation: float | None
    peak_offset: int | None
    series_used: int


def event_study(
    offsets: Sequence[int],
    series: Sequence[Sequence[float | None]],
    *,
    pre_event_only: bool = True,
) -> EventProfile:
    """Average several event-aligned series and locate the peak deviation.

    Each row of `series` is one entity (a cell, usually) sampled at every offset in
    `offsets`, with None where that bucket had no data. Offsets are hours relative to the
    event: -24 is a day before, 0 is the bucket containing the event, +24 a day after.

    The baseline is the mean of the strictly-negative offsets (`pre_event_only`), because
    the whole purpose is to compare "before" with "during and after"; including post-event
    buckets in the baseline is the classic way to make a real outage disappear.

    `peak_deviation` is the largest **signed** departure from the baseline, selected by
    absolute size — a cell outage shows up as a fall in availability and a rise in
    complaints, and both must be found by the same function.
    """
    offset_list = tuple(offsets)
    means: list[float | None] = []
    counts: list[int] = []
    for index in range(len(offset_list)):
        column = [row[index] for row in series if index < len(row)]
        finite = [float(value) for value in column if value is not None and _finite(value)]
        counts.append(len(finite))
        means.append(sum(finite) / len(finite) if finite else None)

    baseline_values = [
        value
        for offset, value in zip(offset_list, means, strict=True)
        if value is not None and (offset < 0 or not pre_event_only)
    ]
    baseline = sum(baseline_values) / len(baseline_values) if baseline_values else None

    peak_offset: int | None = None
    peak_deviation: float | None = None
    if baseline is not None:
        for offset, value in zip(offset_list, means, strict=True):
            if value is None or (pre_event_only and offset < 0):
                continue
            deviation = value - baseline
            if peak_deviation is None or abs(deviation) > abs(peak_deviation):
                peak_deviation = deviation
                peak_offset = offset
    return EventProfile(
        offsets=offset_list,
        mean_series=tuple(means),
        counts=tuple(counts),
        pre_event_baseline=baseline,
        peak_deviation=peak_deviation,
        peak_offset=peak_offset,
        series_used=len(series),
    )


# ------------------------------------------------------------------------- PCA


@dataclass(frozen=True, slots=True)
class PcaDecomposition:
    """Scree, cumulative variance and loadings over the standardised matrix."""

    labels: tuple[str, ...]
    explained_variance_ratio: tuple[float, ...]
    cumulative_variance: tuple[float, ...]
    loadings: tuple[tuple[float, ...], ...]
    dropped_labels: tuple[str, ...]
    n_used: int
    reason: str = ""

    def components_for(self, target: float) -> int:
        """How many components are needed to reach `target` cumulative variance."""
        for index, cumulative in enumerate(self.cumulative_variance, start=1):
            if cumulative >= target - 1e-12:
                return index
        return len(self.cumulative_variance)


def principal_components(
    labels: Sequence[str],
    columns: Sequence[Sequence[float | None]],
) -> PcaDecomposition:
    """PCA over the KPI matrix, **standardised first**.

    Standardisation is not optional here: the KPI families have wildly different units and
    scales — availability sits between 99 and 100, latency between 20 and 200 ms, active
    users in the hundreds, SINR in single-digit dB. PCA maximises explained *variance*, so
    on raw columns the first component would simply be whichever KPI happens to be
    measured in the largest numbers (active users), and the loadings would describe the
    choice of units rather than the network. Z-scoring every column puts them on a common
    footing, which makes this a PCA of the correlation matrix.

    **Variance is not relevance.** The components rank directions of KPI variability, with
    no knowledge of complaints at all; a dominant component can be entirely irrelevant to
    customer experience. This is exploratory — it shows collinearity between KPI families
    and how many independent dimensions the ten families really contain. Ranking drivers
    is a supervised problem and belongs to LightGBM + SHAP, not here.

    Component signs are arbitrary in any PCA, so each component is flipped to make its
    largest-magnitude loading positive; otherwise two runs over the same data could print
    mirror-image loading tables.

    Rows with any missing value are dropped (complete-case), and constant columns are
    reported in `dropped_labels` rather than producing a divide-by-zero.
    """
    if len(labels) != len(columns):
        raise ValueError("labels and columns must be the same length")
    np = _require("numpy")
    if not columns:
        return PcaDecomposition((), (), (), (), (), 0, "not enough signal: no columns")

    height = min(len(column) for column in columns)
    raw = np.array(
        [
            [column[row] if column[row] is not None else np.nan for column in columns]
            for row in range(height)
        ],
        dtype=float,
    )
    complete = raw[~np.isnan(raw).any(axis=1)] if height else raw
    n_used = int(complete.shape[0])
    if n_used < 3:
        return PcaDecomposition(
            tuple(labels),
            (),
            (),
            (),
            tuple(labels),
            n_used,
            "not enough signal: fewer than 3 complete observations",
        )

    std = complete.std(axis=0, ddof=1)
    keep = std > 1e-12
    dropped = tuple(label for label, used in zip(labels, keep.tolist(), strict=True) if not used)
    if not bool(keep.any()):
        return PcaDecomposition(
            tuple(labels),
            (),
            (),
            (),
            dropped,
            n_used,
            "not enough signal: every column is constant",
        )
    kept_labels = [label for label, used in zip(labels, keep.tolist(), strict=True) if used]
    standardised = (complete[:, keep] - complete[:, keep].mean(axis=0)) / std[keep]

    _, singular, components = np.linalg.svd(standardised, full_matrices=False)
    variances = singular**2
    total = float(variances.sum())
    if total <= 0.0:  # pragma: no cover - guarded by the constant-column check above
        return PcaDecomposition(
            tuple(labels),
            (),
            (),
            (),
            dropped,
            n_used,
            "not enough signal: the standardised matrix has no variance",
        )
    ratios = (variances / total).tolist()
    cumulative: list[float] = []
    running = 0.0
    for ratio in ratios:
        running += float(ratio)
        cumulative.append(min(1.0, running))

    index_of = {label: position for position, label in enumerate(kept_labels)}
    loadings: list[tuple[float, ...]] = []
    for component in components.tolist():
        largest = max(range(len(component)), key=lambda i: abs(component[i]))
        sign = -1.0 if component[largest] < 0 else 1.0
        # Re-expand to the full label list so a dropped constant column still has a slot.
        loadings.append(
            tuple(
                sign * float(component[index_of[label]]) if label in index_of else 0.0
                for label in labels
            ),
        )
    return PcaDecomposition(
        labels=tuple(labels),
        explained_variance_ratio=tuple(float(ratio) for ratio in ratios),
        cumulative_variance=tuple(cumulative),
        loadings=tuple(loadings),
        dropped_labels=dropped,
        n_used=n_used,
    )


# ---------------------------------------------------------- mutual information


def mutual_information(
    columns: Sequence[Sequence[float | None]],
    target: Sequence[float | None],
    *,
    random_state: int = 0,
    n_neighbors: int = 3,
) -> tuple[float, ...]:
    """Mutual information between each column and the target, in nats.

    Why this and not just correlation: Pearson measures a *linear* relationship, so a KPI
    that only matters once it crosses a threshold — PRB utilisation that is harmless at
    40% and 70% and catastrophic at 95%, latency that customers do not notice until it
    doubles — can show a correlation near zero while fully determining the complaint rate.
    Mutual information is zero only under genuine independence, so it catches exactly
    those shapes.

    It is unsigned and unitless. A high score says "knowing this KPI tells you something
    about the complaint rate"; it does not say in which direction, and it is not
    comparable across windows of different length. The k-NN estimator is also biased
    upward on short series, which is why the caller refuses to report it below a minimum
    number of points.

    `random_state` is pinned: the estimator adds tiny noise to break ties, and an
    unpinned seed would make the same window produce a different ranking on each call.
    """
    np = _require("numpy")
    selection = _require("sklearn.feature_selection")
    if not columns:
        return ()
    height = min([len(column) for column in columns] + [len(target)])
    matrix = np.array(
        [
            [column[row] if column[row] is not None else np.nan for column in columns]
            for row in range(height)
        ],
        dtype=float,
    )
    outcome = np.array(
        [target[row] if target[row] is not None else np.nan for row in range(height)],
        dtype=float,
    )
    mask = ~np.isnan(matrix).any(axis=1) & ~np.isnan(outcome)
    matrix, outcome = matrix[mask], outcome[mask]
    if matrix.shape[0] <= n_neighbors or float(np.std(outcome)) <= 0.0:
        # No variation in the target, or fewer points than the estimator's neighbourhood.
        return tuple(0.0 for _ in columns)
    scores = selection.mutual_info_regression(
        matrix,
        outcome,
        random_state=random_state,
        n_neighbors=n_neighbors,
    )
    return tuple(max(0.0, float(score)) for score in scores.tolist())


def deseasonalise_optional(
    timestamps: Sequence[datetime],
    values: Sequence[float | None],
    *,
    profile: SeasonalProfile | None = None,
) -> tuple[tuple[float | None, ...], SeasonalProfile]:
    """`deseasonalise` for a series with holes: None in, None out, at the same positions.

    KPI series genuinely have gaps (a cell down reports nothing), and filling them with a
    zero would be a fabricated observation — the gap must survive into the correlation so
    that the pair is dropped there rather than correlated against a made-up value.
    """
    effective = profile if profile is not None else hour_of_week_profile(timestamps, values)
    adjusted: list[float | None] = []
    for moment, value in zip(timestamps, values, strict=False):
        if not _finite(value):
            adjusted.append(None)
            continue
        median = effective.medians[hour_of_week(moment)]
        adjusted.append(float(value) if median is None else float(value) - median)  # type: ignore[arg-type]
    return tuple(adjusted), effective
