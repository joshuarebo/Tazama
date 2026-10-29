"""DTOs and configuration for the insights module (PRD section 9, analytics half).

These are plain data: the service builds them from the pure functions in `statistics.py`
and the API serialises them. Nothing here touches a database, a clock or a model.

Two rules shape every DTO in this file:

* **Every result carries its inputs.** A correlation of 0.81 means nothing without the
  cell, the KPI, the window, the lag range and the number of points behind it. The
  dashboard shows the number; the DTO is what makes it defensible.
* **Every result carries `caveats`.** A statistic without its caveats is a trap. Small
  n, gaps in the series, a de-seasonalisation baseline too short to hold a full weekly
  profile, a lag chosen by scanning many lags — each of those is recorded as plain text
  so that whoever reads the number also reads why it might be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tazama.domain.value_objects import KpiName, TimeWindow

#: Slots in the hour-of-week seasonal profile: 7 days x 24 hours.
HOURS_PER_WEEK: int = 168

#: The KPI families fed into the exploratory analyses, in a stable order so that a
#: correlation matrix, a PCA loading table and a mutual-information ranking taken from
#: the same window can be read side by side.
DEFAULT_KPI_FAMILIES: tuple[KpiName, ...] = (
    KpiName.AVAILABILITY,
    KpiName.RRC_SETUP_SUCCESS,
    KpiName.ERAB_SETUP_SUCCESS,
    KpiName.DROP_RATE,
    KpiName.PRB_UTILISATION,
    KpiName.DL_THROUGHPUT,
    KpiName.UL_THROUGHPUT,
    KpiName.LATENCY,
    KpiName.ACTIVE_USERS,
    KpiName.SINR,
)

#: Label used for the complaint-rate column wherever it joins the KPI families in a
#: matrix. It is not a KPI, so it gets a name that cannot collide with `KpiName`.
COMPLAINT_RATE_LABEL: str = "complaints_per_1k_subs"


@dataclass(frozen=True, slots=True)
class InsightsConfig:
    """Every threshold the analytics half argues about, in one injectable place.

    Defaults are chosen for the seeded dataset (30 days of hourly KPI buckets, 600 cells)
    and are deliberately conservative: it is better to return a result stamped
    "not enough signal" than a confident number computed from nine points.
    """

    granularity: str = "1h"
    #: Lag range for cross-correlation and the Granger-style test, in hours.
    max_lag_hours: int = 6
    #: Below this, a correlation or a rate series is reported but flagged as thin.
    min_points: int = 24
    #: Lagged OLS burns `2 * lag + 1` degrees of freedom; refuse below this.
    min_points_for_granger: int = 48
    #: Days of history used to estimate the hour-of-week seasonal profile.
    baseline_days: int = 14
    #: Hour-of-week slots that must carry at least one observation before the profile is
    #: trusted. 84 of 168 is half a weekly cycle.
    min_profile_slots: int = 84
    #: Normalisation denominator: complaints per this many subscribers.
    per_subscribers: float = 1000.0
    #: Half-width of the event-study window, in hours (t-24h .. t+24h).
    event_window_hours: int = 24
    #: p-value below which the Granger-style test is called significant.
    significance: float = 0.05
    #: How many pairs the correlation heatmap returns as a ranked list.
    top_correlations: int = 10
    #: Hard cap on the cells pulled into one matrix, so a region-wide call stays bounded.
    max_cells: int = 400
    #: Mutual information uses a k-NN estimator; fewer points than this is noise.
    min_points_for_mi: int = 24
    #: Cumulative-variance targets reported by the PCA scree.
    variance_targets: tuple[float, ...] = (0.80, 0.90)

    def __post_init__(self) -> None:
        if self.granularity not in {"1h", "15m"}:
            raise ValueError(f"unsupported granularity {self.granularity!r}")
        if self.max_lag_hours < 1:
            raise ValueError("max_lag_hours must be at least 1")
        if self.min_points < 3:
            raise ValueError("min_points must be at least 3")
        if self.per_subscribers <= 0:
            raise ValueError("per_subscribers must be positive")
        if self.event_window_hours < 1:
            raise ValueError("event_window_hours must be at least 1")
        if not 0.0 < self.significance < 1.0:
            raise ValueError("significance must be in (0, 1)")
        if any(not 0.0 < target <= 1.0 for target in self.variance_targets):
            raise ValueError("variance targets must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One bucket of one series. `bucket_start` is the half-open bucket's left edge, UTC."""

    bucket_start: datetime
    value: float


@dataclass(frozen=True, slots=True)
class KpiMatrix:
    """KPI families as aligned columns over one bucket grid.

    `rows[i][j]` is the value of `kpis[j]` in the bucket starting at `bucket_starts[i]`,
    or None where the bucket had no sample. Rows, not columns, because that is the shape
    the adapter can produce with one `GROUP BY bucket_start` and the shape every library
    expects (observations x features).
    """

    window: TimeWindow
    bucket_starts: tuple[datetime, ...]
    kpis: tuple[KpiName, ...]
    rows: tuple[tuple[float | None, ...], ...]
    cells: int = 0
    region: str | None = None

    def column(self, kpi: KpiName) -> tuple[float | None, ...]:
        index = self.kpis.index(kpi)
        return tuple(row[index] for row in self.rows)

    @property
    def n_points(self) -> int:
        return len(self.bucket_starts)


@dataclass(frozen=True, slots=True)
class RateSeries:
    """Complaints per 1,000 subscribers per hour, raw and de-seasonalised.

    Both series are returned on purpose. The raw one is what an operations reviewer
    recognises; the de-seasonalised one is the only one that may be fed to a correlation,
    because otherwise the evening busy hour correlates with everything that also has an
    evening busy hour.
    """

    scope: str
    window: TimeWindow
    metric: str
    bucket_starts: tuple[datetime, ...]
    counts: tuple[float, ...]
    rate_per_1k: tuple[float, ...]
    deseasonalised: tuple[float, ...]
    subscribers_est: int
    per_subscribers: float
    hour_of_week_profile: tuple[float | None, ...] = ()
    profile_slots_covered: int = 0
    gaps: int = 0
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def n_points(self) -> int:
        return len(self.bucket_starts)


@dataclass(frozen=True, slots=True)
class LeadLagResult:
    """Lagged cross-correlation between a KPI series and a complaint-rate series.

    Sign convention (the whole point of this DTO, so it is stated three times in this
    codebase): a **positive** `best_lag_hours` of L means the complaint series had to be
    shifted L hours *later* to line up with the KPI series — the KPI moved first and
    complaints followed L hours afterwards. `kpi_leads` is exactly `best_lag_hours > 0`.
    """

    scope: str
    kpi: KpiName
    window: TimeWindow
    lags: tuple[int, ...]
    correlations: tuple[float | None, ...]
    best_lag_hours: int | None
    best_correlation: float | None
    kpi_leads: bool
    zero_lag_correlation: float | None = None
    n_points: int = 0
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)

    def correlation_at(self, lag: int) -> float | None:
        if lag not in self.lags:
            return None
        return self.correlations[self.lags.index(lag)]


@dataclass(frozen=True, slots=True)
class CausalityResult:
    """Granger-style predictive-precedence test, KPI -> complaint rate.

    Granger shows that the past of one series improves a forecast of another beyond the
    second series' own past. That is precedence, **not** causation: a common driver
    upstream of both produces the same result. The verdict text says so every time.
    """

    scope: str
    kpi: KpiName
    window: TimeWindow
    f_statistic: float | None
    p_value: float | None
    lag_hours: int | None
    significant: bool
    verdict: str
    n_points: int = 0
    tested_lags: tuple[int, ...] = ()
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventStudyResult:
    """Mean series relative to an event, from t-24h to t+24h.

    The event time is supplied by the caller. Production code never reads the seed's
    answer key: an event study is a tool an analyst points at a window, and the labelled
    scenarios are only used by the eval suite to check that the tool finds them.
    """

    event_at: datetime
    window: TimeWindow
    metric: str
    cell_ids: tuple[str, ...]
    offsets_hours: tuple[int, ...]
    mean_series: tuple[float | None, ...]
    observation_counts: tuple[int, ...]
    pre_event_baseline: float | None
    peak_deviation: float | None
    peak_offset_hours: int | None
    series_used: int = 0
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CorrelationPair:
    """One cell of the heatmap, promoted to a list entry so the UI need not scan."""

    left: str
    right: str
    pearson: float | None
    spearman: float | None

    @property
    def abs_pearson(self) -> float:
        return 0.0 if self.pearson is None else abs(self.pearson)


@dataclass(frozen=True, slots=True)
class CorrelationMatrix:
    """Pairwise Pearson and Spearman across the KPI families plus the complaint rate.

    Both coefficients, always: Pearson answers "do they move together linearly" and
    Spearman answers "do they move together at all monotonically". A large gap between
    them is itself a finding (it usually means an outlier or a saturating KPI).
    """

    window: TimeWindow
    labels: tuple[str, ...]
    pearson: tuple[tuple[float | None, ...], ...]
    spearman: tuple[tuple[float | None, ...], ...]
    top: tuple[CorrelationPair, ...]
    n_points: int = 0
    region: str | None = None
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)

    def value(self, left: str, right: str) -> float | None:
        return self.pearson[self.labels.index(left)][self.labels.index(right)]


@dataclass(frozen=True, slots=True)
class PcaResult:
    """Scree, cumulative variance and loadings over the standardised KPI matrix.

    **Variance is not relevance.** PCA finds the directions in which the KPI families
    vary most; it knows nothing about complaints. A component can explain 60% of KPI
    variance and have no bearing on customer experience. Use it to see structure and
    collinearity; rank drivers with supervised importance (LightGBM + SHAP) instead.
    """

    window: TimeWindow
    labels: tuple[str, ...]
    explained_variance_ratio: tuple[float, ...]
    cumulative_variance: tuple[float, ...]
    loadings: tuple[tuple[float, ...], ...]
    components_for: dict[str, int] = field(default_factory=dict)
    dropped_labels: tuple[str, ...] = ()
    n_points: int = 0
    region: str | None = None
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def n_components(self) -> int:
        return len(self.explained_variance_ratio)


@dataclass(frozen=True, slots=True)
class MutualInformationScore:
    """Mutual information between one KPI and the complaint rate.

    Mutual information is zero only for genuine independence, so it catches the
    threshold-shaped and saturating relationships a correlation coefficient reports as
    ~0. It is unsigned and unitless: it says "related", never "related this way".
    """

    kpi: KpiName
    score: float
    rank: int
    n_points: int = 0
    caveats: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
