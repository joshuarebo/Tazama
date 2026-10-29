"""Insights: the statistical analytics half of the ML work (PRD section 9).

What this service is for: deciding, from observations alone, whether the network saw a
problem before the customers did. Everything else in the module serves that question —
normalisation makes cells comparable, de-seasonalisation removes the clock, lagged
cross-correlation measures the lead, the Granger-style test asks whether the lead is
predictive, and the event study shows the shape of a known outage.

Order of operations, never reordered:

    1. read the series through a port (already aggregated in SQL)
    2. normalise counts to per-1,000-subscribers, per hour
    3. de-seasonalise both series against an hour-of-week median profile
    4. run the statistic
    5. attach the inputs and the caveats
    6. record one decision

Step 5 is not decoration. A lead of +2 hours computed over 31 points with a seasonal
baseline covering a third of the week is a very different claim from the same number over
700 points, and the DTO must carry enough to tell them apart.

This service never reads the seed's answer key. `event_study` takes the event time from
its caller, which is what lets an analyst point it at any window and what keeps the
labelled scenarios confined to the eval suite.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from tazama.core.clock import Clock, ensure_utc, floor_to
from tazama.core.errors import BadRequest
from tazama.core.logging import get_logger
from tazama.domain.value_objects import KPI_POLARITY, KpiName, TimeWindow
from tazama.modules.insights import statistics as stats
from tazama.modules.insights.models import (
    COMPLAINT_RATE_LABEL,
    DEFAULT_KPI_FAMILIES,
    HOURS_PER_WEEK,
    CausalityResult,
    CorrelationMatrix,
    CorrelationPair,
    EventStudyResult,
    InsightsConfig,
    KpiMatrix,
    LeadLagResult,
    MutualInformationScore,
    PcaResult,
    RateSeries,
    SeriesPoint,
)
from tazama.modules.insights.ports import (
    CellExposureReader,
    ComplaintSeriesReader,
    DecisionLog,
    KpiSeriesReader,
)

_log = get_logger(__name__)

#: Stamped on every decision row so an audit read years later still means something.
INSIGHTS_VERSION = "insights-1.0.0"

_HOUR = timedelta(hours=1)


class InsightsService:
    """Orchestrates the pure statistics over series pulled through the read ports."""

    def __init__(
        self,
        *,
        kpis: KpiSeriesReader,
        complaints: ComplaintSeriesReader,
        cells: CellExposureReader,
        clock: Clock,
        decisions: DecisionLog | None = None,
        config: InsightsConfig | None = None,
    ) -> None:
        self._kpis = kpis
        self._complaints = complaints
        self._cells = cells
        self._clock = clock
        self._decisions = decisions
        self._config = config or InsightsConfig()
        if self._config.granularity != "1h":
            # Lags, the hour-of-week profile and the event offsets are all expressed in
            # hours. 15-minute buckets are the ML feature builder's resolution, not this
            # service's, and silently mixing the two would mislabel every lag.
            raise BadRequest(
                "insights analyses run on hourly buckets; "
                f"granularity {self._config.granularity!r} is not supported here.",
            )

    # ------------------------------------------------------------- public API

    def complaint_rate(
        self,
        *,
        window: TimeWindow,
        cell_id: str | None = None,
        region: str | None = None,
    ) -> RateSeries:
        """Complaints per 1,000 subscribers per hour, raw and de-seasonalised.

        Exactly one of `cell_id` or `region` narrows the scope; with neither, the whole
        network is measured. The denominator is `cell.subscribers_est` for a cell and the
        sum of it for a region, because a raw count tracks cell size before it tracks
        anything about the network.
        """
        self._reject_both(cell_id, region)
        scope = self._scope(cell_id, region)
        baseline = self._baseline_window(window)
        grid = _hourly_grid(baseline)
        offset = _suffix_offset(baseline, window)

        points = self._read_complaints(cell_id=cell_id, region=region, window=baseline)
        counts = _align(points, grid, fill=0.0)
        subscribers, exposure_caveats = self._exposure(cell_id=cell_id, region=region)

        caveats = list(exposure_caveats)
        # Stated on every rate series, because the denominator is the weakest link: a
        # cell whose subscriber estimate is stale looks better or worse than it is, and
        # nothing downstream can detect that from the rate alone.
        caveats.append(
            f"normalised by cell.subscribers_est, an estimate, per "
            f"{int(self._config.per_subscribers)} subscribers per hour: the rate is only "
            "as good as that estimate",
        )
        if subscribers > 0:
            rates = stats.normalise_per_1k(
                [value or 0.0 for value in counts],
                subscribers,
                per_subscribers=self._config.per_subscribers,
            )
        else:
            # No denominator means no comparable rate. The counts are still returned so a
            # reviewer can see the raw signal, but the rate stays zero and says why.
            rates = tuple(0.0 for _ in counts)

        adjusted = stats.deseasonalise(grid, rates)
        window_grid = grid[offset:]
        window_counts = tuple(float(value or 0.0) for value in counts[offset:])
        caveats += self._series_caveats(
            n_points=len(window_grid),
            gaps=sum(1 for value in counts[offset:] if value is None),
            profile=adjusted.profile,
            baseline=baseline,
        )
        if not any(window_counts):
            caveats.append(
                "no complaints in the window: the rate series is all zero, so every "
                "correlation against it is undefined rather than weak",
            )

        result = RateSeries(
            scope=scope,
            window=window,
            metric=COMPLAINT_RATE_LABEL,
            bucket_starts=window_grid,
            counts=window_counts,
            rate_per_1k=rates[offset:],
            deseasonalised=adjusted.deseasonalised[offset:],
            subscribers_est=subscribers,
            per_subscribers=self._config.per_subscribers,
            hour_of_week_profile=adjusted.profile.medians,
            profile_slots_covered=adjusted.profile.slots_covered,
            gaps=sum(1 for value in counts[offset:] if value is None),
            caveats=tuple(caveats),
            inputs=self._inputs(
                window,
                scope=scope,
                subscribers_est=subscribers,
                baseline_days=self._config.baseline_days,
            ),
        )
        self._record("complaint_rate", scope, "computed", result.inputs, result.caveats)
        return result

    def lead_lag(
        self,
        *,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        max_lag_hours: int = 6,
    ) -> LeadLagResult:
        """Does this KPI move before the complaints do, and by how long?

        The sign convention is the one in `statistics.cross_correlation` and in
        `LeadLagResult`: a positive `best_lag_hours` of L means the KPI moved first and
        complaints followed L hours later, so `kpi_leads` is True. Both series are
        de-seasonalised first; correlating them raw would mostly measure the evening peak
        they share.
        """
        if max_lag_hours < 1:
            raise BadRequest("max_lag_hours must be at least 1")
        scope = self._scope(cell_id, None)
        baseline = self._baseline_window(window)
        grid = _hourly_grid(baseline)
        offset = _suffix_offset(baseline, window)

        kpi_series, kpi_gaps, kpi_profile = self._kpi_column(
            cell_id=cell_id,
            kpi=kpi,
            window=baseline,
            grid=grid,
        )
        rate = self.complaint_rate(window=window, cell_id=cell_id)
        complaint_series = self._deseasonalised_rate(
            cell_id=cell_id,
            region=None,
            window=baseline,
            grid=grid,
        )

        outcome = stats.cross_correlation(
            kpi_series[offset:],
            complaint_series[offset:],
            max_lag=max_lag_hours,
        )
        caveats = list(rate.caveats)
        caveats += self._series_caveats(
            n_points=len(grid) - offset,
            gaps=kpi_gaps,
            profile=kpi_profile,
            baseline=baseline,
        )
        caveats.append(
            f"{len(outcome.lags)} lags were scanned and the best was kept, which biases "
            "the reported correlation upward; treat it as a shortlist, not a measurement",
        )
        if outcome.best_correlation is None:
            caveats.append(
                "no lag produced a defined correlation: one of the series is constant or "
                "too short after de-seasonalisation",
            )
        lower_is_worse, _ = KPI_POLARITY[kpi]
        if outcome.best_correlation is not None:
            expected = -1.0 if lower_is_worse else 1.0
            if outcome.best_correlation * expected < 0:
                caveats.append(
                    f"the correlation sign is the opposite of {kpi.value}'s polarity: "
                    "degradation here should move with complaints, so this is more likely "
                    "a coincidence than a lead",
                )

        result = LeadLagResult(
            scope=scope,
            kpi=kpi,
            window=window,
            lags=outcome.lags,
            correlations=outcome.correlations,
            best_lag_hours=outcome.best_lag,
            best_correlation=outcome.best_correlation,
            kpi_leads=outcome.leads,
            zero_lag_correlation=outcome.zero_lag_correlation,
            n_points=outcome.n_points,
            caveats=tuple(caveats),
            inputs=self._inputs(
                window,
                scope=scope,
                kpi=kpi.value,
                max_lag_hours=max_lag_hours,
                deseasonalised=True,
                overlap_at_best=outcome.overlap_at_best,
            ),
        )
        self._record(
            "lead_lag",
            scope,
            "kpi_leads" if result.kpi_leads else "no_lead",
            result.inputs,
            result.caveats,
            confidence=abs(result.best_correlation) if result.best_correlation else None,
        )
        return result

    def granger(
        self,
        *,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        max_lag_hours: int = 6,
    ) -> CausalityResult:
        """Granger-style test: does the KPI's past predict complaints beyond their own?

        **Predictive precedence, not causation.** A significant result means the KPI
        carries information about the complaint rate that the complaint rate's own history
        does not. A shared upstream cause — a transmission link failing, a power cut —
        produces exactly the same signature. The verdict text says so every time, because
        the number will end up on a slide.
        """
        if max_lag_hours < 1:
            raise BadRequest("max_lag_hours must be at least 1")
        scope = self._scope(cell_id, None)
        baseline = self._baseline_window(window)
        grid = _hourly_grid(baseline)
        offset = _suffix_offset(baseline, window)

        kpi_series, kpi_gaps, kpi_profile = self._kpi_column(
            cell_id=cell_id,
            kpi=kpi,
            window=baseline,
            grid=grid,
        )
        complaint_series = self._deseasonalised_rate(
            cell_id=cell_id,
            region=None,
            window=baseline,
            grid=grid,
        )
        outcome = stats.granger_f_test(
            kpi_series[offset:],
            complaint_series[offset:],
            max_lag=max_lag_hours,
        )

        caveats = self._series_caveats(
            n_points=len(grid) - offset,
            gaps=kpi_gaps,
            profile=kpi_profile,
            baseline=baseline,
        )
        if outcome.n_used < self._config.min_points_for_granger:
            caveats.append(
                f"{outcome.n_used} usable points is below the "
                f"{self._config.min_points_for_granger} "
                "this test wants: lagged OLS spends degrees of freedom quickly and a short "
                "series produces a confident-looking p-value from noise",
            )
        if outcome.reason:
            caveats.append(outcome.reason)
        if len(outcome.tested_lags) > 1:
            caveats.append(
                f"the lag was chosen as the best of {len(outcome.tested_lags)} candidates, "
                "so the p-value is optimistic and uncorrected for multiple comparisons",
            )
        caveats.append(
            "Granger precedence is not causation: a common upstream driver of both series "
            "produces the same result",
        )

        significant = (
            outcome.usable
            and outcome.p_value is not None
            and outcome.p_value < self._config.significance
        )
        result = CausalityResult(
            scope=scope,
            kpi=kpi,
            window=window,
            f_statistic=outcome.f_statistic,
            p_value=outcome.p_value,
            lag_hours=outcome.lag,
            significant=bool(significant),
            verdict=self._verdict(kpi, outcome, significant=bool(significant)),
            n_points=outcome.n_used,
            tested_lags=outcome.tested_lags,
            caveats=tuple(caveats),
            inputs=self._inputs(
                window,
                scope=scope,
                kpi=kpi.value,
                max_lag_hours=max_lag_hours,
                significance=self._config.significance,
            ),
        )
        self._record(
            "granger",
            scope,
            "significant" if result.significant else "not_significant",
            result.inputs,
            result.caveats,
        )
        return result

    def event_study(
        self,
        *,
        cell_ids: Sequence[str],
        window: TimeWindow,
        event_at: datetime,
        kpi: KpiName | None = None,
    ) -> EventStudyResult:
        """Average the cells' series around a caller-supplied event time.

        `event_at` and `window` come from the caller — an analyst, an incident record, or
        the eval suite. Production code never looks up the seed's labels: this is a tool
        you point at a moment, and that is also what makes it usable on a live incident
        nobody has labelled.

        Offsets run from `-event_window_hours` to `+event_window_hours`, the baseline is
        the mean over the strictly-negative offsets, and the peak is the largest signed
        departure from it.
        """
        event_at = ensure_utc(event_at)
        if not window.contains(event_at):
            raise BadRequest("event_at must fall inside the supplied window")
        if not cell_ids:
            raise BadRequest("event_study needs at least one cell")

        span = self._config.event_window_hours
        anchor = floor_to(event_at, 60)
        offsets = tuple(range(-span, span + 1))
        caveats: list[str] = []
        covered = [
            offset for offset in offsets if window.start <= anchor + offset * _HOUR < window.end
        ]
        if len(covered) < len(offsets):
            caveats.append(
                f"the supplied window covers {len(covered)} of {len(offsets)} event offsets; "
                "the uncovered ones are reported as missing rather than read outside it",
            )

        metric = kpi.value if kpi is not None else COMPLAINT_RATE_LABEL
        rows: list[tuple[float | None, ...]] = []
        used: list[str] = []
        for cell_id in cell_ids:
            row = self._event_row(
                cell_id=cell_id,
                kpi=kpi,
                window=window,
                anchor=anchor,
                offsets=offsets,
            )
            if any(value is not None for value in row):
                rows.append(row)
                used.append(cell_id)
        if not rows:
            caveats.append("none of the requested cells had data in the event window")

        profile = stats.event_study(offsets, rows)
        if profile.pre_event_baseline is None:
            caveats.append(
                "no pre-event observations: without a baseline the peak deviation cannot "
                "be computed",
            )
        pre_count = sum(
            1 for offset, n in zip(offsets, profile.counts, strict=True) if offset < 0 and n
        )
        if 0 < pre_count < span:
            caveats.append(
                f"the pre-event baseline rests on {pre_count} of {span} hourly buckets",
            )
        caveats.append(
            "the series are raw, not de-seasonalised: over a symmetric 48-hour window the "
            "daily cycle is averaged into the baseline rather than removed, so compare "
            "the shape with lead_lag before concluding",
        )

        result = EventStudyResult(
            event_at=event_at,
            window=window,
            metric=metric,
            cell_ids=tuple(used),
            offsets_hours=offsets,
            mean_series=profile.mean_series,
            observation_counts=profile.counts,
            pre_event_baseline=profile.pre_event_baseline,
            peak_deviation=profile.peak_deviation,
            peak_offset_hours=profile.peak_offset,
            series_used=profile.series_used,
            caveats=tuple(caveats),
            inputs=self._inputs(
                window,
                scope=f"cells:{len(cell_ids)}",
                metric=metric,
                event_at=event_at.isoformat(),
                event_window_hours=span,
                cells_requested=len(cell_ids),
            ),
        )
        self._record(
            "event_study",
            f"event:{event_at.isoformat()}",
            "computed" if rows else "no_data",
            result.inputs,
            result.caveats,
        )
        return result

    def correlation_matrix(
        self,
        *,
        window: TimeWindow,
        region: str | None = None,
    ) -> CorrelationMatrix:
        """Pairwise Pearson and Spearman across the KPI families and the complaint rate.

        The matrix is returned for the heatmap and the ranked pairs for everything else:
        a 11x11 grid is a picture, not a finding, and the UI should not have to scan it to
        learn which two series actually move together.
        """
        labels, columns, caveats = self._analysis_columns(window=window, region=region)
        table = stats.correlation_table(labels, columns, top=self._config.top_correlations)
        gap = _largest_rank_gap(table.ranked)
        if gap is not None:
            caveats.append(
                f"the Pearson and Spearman coefficients disagree by {gap:.2f} on at least "
                "one pair, which usually means an outlier or a saturating KPI rather than "
                "a linear relationship",
            )

        result = CorrelationMatrix(
            window=window,
            labels=table.labels,
            pearson=table.pearson,
            spearman=table.spearman,
            top=tuple(
                CorrelationPair(left=left, right=right, pearson=p, spearman=s)
                for left, right, p, s in table.ranked
            ),
            n_points=table.n_points,
            region=region,
            caveats=tuple(caveats),
            inputs=self._inputs(
                window,
                scope=self._scope(None, region),
                series=len(labels),
                deseasonalised=True,
            ),
        )
        self._record(
            "correlation_matrix",
            self._scope(None, region),
            "computed",
            result.inputs,
            result.caveats,
        )
        return result

    def pca(self, *, window: TimeWindow, region: str | None = None) -> PcaResult:
        """PCA over the standardised KPI matrix: scree, cumulative variance, loadings.

        Standardised because the KPI families have wildly different units; exploratory
        because **variance is not relevance**. Both points are argued in
        `statistics.principal_components`, and both are repeated in the caveats so they
        travel with the numbers.
        """
        labels, columns, caveats = self._analysis_columns(
            window=window,
            region=region,
            include_complaints=False,
        )
        decomposition = stats.principal_components(labels, columns)
        if decomposition.reason:
            caveats.append(decomposition.reason)
        if decomposition.dropped_labels:
            caveats.append(
                "constant over this window, so excluded from the decomposition: "
                + ", ".join(decomposition.dropped_labels),
            )
        caveats.append(
            "PCA ranks directions of KPI variance and knows nothing about complaints: "
            "use supervised importance (LightGBM + SHAP) to rank drivers",
        )

        components_for = {
            f"{round(target * 100)}%": decomposition.components_for(target)
            for target in self._config.variance_targets
        }
        result = PcaResult(
            window=window,
            labels=decomposition.labels or tuple(labels),
            explained_variance_ratio=decomposition.explained_variance_ratio,
            cumulative_variance=decomposition.cumulative_variance,
            loadings=decomposition.loadings,
            components_for=components_for,
            dropped_labels=decomposition.dropped_labels,
            n_points=decomposition.n_used,
            region=region,
            caveats=tuple(caveats),
            inputs=self._inputs(
                window,
                scope=self._scope(None, region),
                standardised=True,
                features=len(labels),
                variance_targets=list(self._config.variance_targets),
            ),
        )
        self._record(
            "pca",
            self._scope(None, region),
            "computed" if result.n_components else "not_enough_signal",
            result.inputs,
            result.caveats,
        )
        return result

    def driver_candidates(
        self,
        *,
        window: TimeWindow,
        region: str | None = None,
    ) -> tuple[MutualInformationScore, ...]:
        """Mutual information between each KPI and the complaint rate, ranked.

        Candidates, not drivers: mutual information is unsigned and unconditional, so a
        high score can come from a genuine non-linear relationship or from two series
        sharing a third cause. It is the shortlist that a supervised model then ranks.
        """
        labels, columns, caveats = self._analysis_columns(
            window=window,
            region=region,
            include_complaints=False,
        )
        target = self._deseasonalised_rate_for(window=window, region=region)
        n_points = min([len(column) for column in columns] + [len(target)]) if columns else 0
        if n_points < self._config.min_points_for_mi:
            caveats.append(
                f"{n_points} points is below the {self._config.min_points_for_mi} the k-NN "
                "estimator needs: scores this short are biased upward and should not be ranked",
            )
        caveats.append(
            "mutual information is unsigned: a high score says the KPI and the complaint "
            "rate are related, never in which direction",
        )
        scores = stats.mutual_information(columns, target)
        ordered = sorted(
            zip(labels, scores, strict=True),
            key=lambda item: (-item[1], item[0]),
        )
        results = tuple(
            MutualInformationScore(
                kpi=KpiName(label),
                score=round(score, 6),
                rank=rank,
                n_points=n_points,
                caveats=tuple(caveats),
                inputs=self._inputs(
                    window,
                    scope=self._scope(None, region),
                    kpi=label,
                    estimator="sklearn.mutual_info_regression",
                ),
            )
            for rank, (label, score) in enumerate(ordered, start=1)
        )
        self._record(
            "driver_candidates",
            self._scope(None, region),
            "computed",
            {"window_start": window.start.isoformat(), "candidates": len(results)},
            tuple(caveats),
        )
        return results

    # ------------------------------------------------------------- internals

    def _analysis_columns(
        self,
        *,
        window: TimeWindow,
        region: str | None,
        include_complaints: bool = True,
    ) -> tuple[list[str], list[tuple[float | None, ...]], list[str]]:
        """De-seasonalised KPI columns (plus the complaint rate) on one bucket grid."""
        baseline = self._baseline_window(window)
        grid = _hourly_grid(baseline)
        offset = _suffix_offset(baseline, window)
        cell_ids = self._cells.cell_ids(region=region, limit=self._config.max_cells)
        matrix = self._kpis.hourly_matrix(
            window=baseline,
            kpis=DEFAULT_KPI_FAMILIES,
            region=region,
            granularity=self._config.granularity,
        )
        caveats: list[str] = [
            "every column is de-seasonalised against an hour-of-week median profile: "
            "correlated raw, any two series that peak in the evening look related",
        ]
        if len(cell_ids) >= self._config.max_cells:
            caveats.append(
                f"the matrix averages the first {self._config.max_cells} cells only, so a "
                "region-wide figure is a sample rather than a census",
            )

        labels: list[str] = []
        columns: list[tuple[float | None, ...]] = []
        thin: list[str] = []
        for kpi in DEFAULT_KPI_FAMILIES:
            raw = _matrix_column(matrix, kpi, grid)
            adjusted, profile = stats.deseasonalise_optional(grid, raw)
            labels.append(kpi.value)
            columns.append(adjusted[offset:])
            if profile.slots_covered < self._config.min_profile_slots:
                thin.append(kpi.value)
        if thin:
            caveats.append(
                f"the seasonal baseline covered fewer than {self._config.min_profile_slots} of "
                f"{HOURS_PER_WEEK} hour-of-week slots for: {', '.join(thin)}",
            )
        if include_complaints:
            labels.append(COMPLAINT_RATE_LABEL)
            columns.append(
                self._deseasonalised_rate(
                    cell_id=None,
                    region=region,
                    window=baseline,
                    grid=grid,
                )[offset:],
            )

        n_points = min([len(column) for column in columns], default=0)
        if n_points < self._config.min_points:
            caveats.append(
                f"{n_points} buckets is below the {self._config.min_points} this analysis "
                "wants: a correlation over less than a full day cannot separate signal "
                "from the diurnal cycle",
            )
        return labels, columns, caveats

    def _kpi_column(
        self,
        *,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        grid: Sequence[datetime],
    ) -> tuple[tuple[float | None, ...], int, stats.SeasonalProfile]:
        points = self._kpis.hourly_series(
            cell_id=cell_id,
            kpi=kpi,
            window=window,
            granularity=self._config.granularity,
        )
        raw = _align(points, grid, fill=None)
        adjusted, profile = stats.deseasonalise_optional(grid, raw)
        gaps = sum(1 for value in raw if value is None)
        return adjusted, gaps, profile

    def _deseasonalised_rate(
        self,
        *,
        cell_id: str | None,
        region: str | None,
        window: TimeWindow,
        grid: Sequence[datetime],
    ) -> tuple[float | None, ...]:
        points = self._read_complaints(cell_id=cell_id, region=region, window=window)
        counts = [float(value or 0.0) for value in _align(points, grid, fill=0.0)]
        subscribers, _ = self._exposure(cell_id=cell_id, region=region)
        rates = (
            stats.normalise_per_1k(
                counts, subscribers, per_subscribers=self._config.per_subscribers
            )
            if subscribers > 0
            else tuple(0.0 for _ in counts)
        )
        return tuple(stats.deseasonalise(grid, rates).deseasonalised)

    def _deseasonalised_rate_for(
        self,
        *,
        window: TimeWindow,
        region: str | None,
    ) -> tuple[float | None, ...]:
        baseline = self._baseline_window(window)
        grid = _hourly_grid(baseline)
        offset = _suffix_offset(baseline, window)
        return self._deseasonalised_rate(
            cell_id=None,
            region=region,
            window=baseline,
            grid=grid,
        )[offset:]

    def _event_row(
        self,
        *,
        cell_id: str,
        kpi: KpiName | None,
        window: TimeWindow,
        anchor: datetime,
        offsets: Sequence[int],
    ) -> tuple[float | None, ...]:
        """One cell's series sampled at every offset, None where the data is absent."""
        grid = tuple(anchor + offset * _HOUR for offset in offsets)
        if kpi is not None:
            points = self._kpis.hourly_series(
                cell_id=cell_id,
                kpi=kpi,
                window=window,
                granularity=self._config.granularity,
            )
            return _align(points, grid, fill=None)
        points = self._read_complaints(cell_id=cell_id, region=None, window=window)
        subscribers, _ = self._exposure(cell_id=cell_id, region=None)
        observed = {point.bucket_start: float(point.value) for point in points}
        if subscribers <= 0:
            return tuple(None for _ in grid)
        scale = self._config.per_subscribers / float(subscribers)
        # Inside the caller's window a missing complaint bucket is a real zero; outside it
        # we never read, so it stays None and the event study drops it.
        return tuple(
            observed.get(moment, 0.0) * scale if window.contains(moment) else None
            for moment in grid
        )

    def _read_complaints(
        self,
        *,
        cell_id: str | None,
        region: str | None,
        window: TimeWindow,
    ) -> tuple[SeriesPoint, ...]:
        if cell_id is not None:
            return self._complaints.hourly_counts_by_cell(cell_id=cell_id, window=window)
        return self._complaints.hourly_counts_by_region(window=window, region=region)

    def _exposure(
        self,
        *,
        cell_id: str | None,
        region: str | None,
    ) -> tuple[int, list[str]]:
        caveats: list[str] = []
        if cell_id is not None:
            estimate = self._cells.subscribers_est(cell_id)
            if not estimate:
                caveats.append(
                    f"cell {cell_id} has no subscriber estimate, so the rate cannot be "
                    "normalised and is reported as zero; the raw counts are still shown",
                )
                return 0, caveats
            return int(estimate), caveats
        total = self._cells.total_subscribers_est(region=region)
        if total <= 0:
            caveats.append(
                "no subscriber estimate for this scope, so the rate cannot be normalised",
            )
            return 0, caveats
        return int(total), caveats

    def _baseline_window(self, window: TimeWindow) -> TimeWindow:
        """`window`, extended backwards far enough to estimate a weekly profile."""
        start = min(window.start, window.end - timedelta(days=self._config.baseline_days))
        return TimeWindow(start=start, end=window.end)

    def _series_caveats(
        self,
        *,
        n_points: int,
        gaps: int,
        profile: stats.SeasonalProfile,
        baseline: TimeWindow,
    ) -> list[str]:
        caveats: list[str] = []
        if n_points < self._config.min_points:
            caveats.append(
                f"{n_points} hourly buckets is below the {self._config.min_points} this "
                "analysis wants; the result is indicative only",
            )
        if gaps:
            caveats.append(
                f"{gaps} of {n_points} buckets had no sample; those positions are dropped "
                "from the statistic rather than interpolated",
            )
        if profile.slots_covered < self._config.min_profile_slots:
            caveats.append(
                f"the de-seasonalisation baseline covered {profile.slots_covered} of "
                f"{HOURS_PER_WEEK} hour-of-week slots over "
                f"{baseline.duration.days} days: the weekly profile is incomplete and "
                "the uncovered hours were left unadjusted",
            )
        return caveats

    def _verdict(
        self,
        kpi: KpiName,
        outcome: stats.GrangerOutcome,
        *,
        significant: bool,
    ) -> str:
        if not outcome.usable:
            return (
                f"not enough signal to test {kpi.value} against the complaint rate: "
                f"{outcome.reason or 'the series could not support a lagged regression'}"
            )
        lag = outcome.lag
        p_value = outcome.p_value or 1.0
        if significant:
            return (
                f"{kpi.value} helps predict the complaint rate {lag}h ahead "
                f"(F={outcome.f_statistic:.2f}, p={p_value:.4f}): predictive precedence, "
                "not proof of causation — a shared upstream fault looks the same"
            )
        return (
            f"{kpi.value} adds no measurable predictive power over the complaint rate's own "
            f"history at lags up to {lag}h (F={outcome.f_statistic:.2f}, p={p_value:.4f})"
        )

    def _scope(self, cell_id: str | None, region: str | None) -> str:
        if cell_id is not None:
            return f"cell:{cell_id}"
        if region is not None:
            return f"region:{region}"
        return "network"

    @staticmethod
    def _reject_both(cell_id: str | None, region: str | None) -> None:
        if cell_id is not None and region is not None:
            raise BadRequest("give either cell_id or region, not both")

    def _inputs(self, window: TimeWindow, **extra: Any) -> dict[str, Any]:
        inputs: dict[str, Any] = {
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "granularity": self._config.granularity,
            "computed_at": self._clock.now().isoformat(),
        }
        inputs.update(extra)
        return inputs

    def _record(
        self,
        action: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        caveats: Sequence[str],
        *,
        confidence: float | None = None,
    ) -> None:
        if self._decisions is None:
            return
        self._decisions.record(
            action=f"insights.{action}",
            entity_type="analysis",
            entity_id=entity_id,
            outcome=outcome,
            inputs=inputs,
            evidence=list(caveats),
            confidence=confidence,
            rule_version=INSIGHTS_VERSION,
        )


# ------------------------------------------------------------ grid utilities


def _hourly_grid(window: TimeWindow) -> tuple[datetime, ...]:
    """Every hourly bucket start in `[window.start, window.end)`, floored to the hour."""
    moment = floor_to(window.start, 60)
    grid: list[datetime] = []
    while moment < window.end:
        grid.append(moment)
        moment += _HOUR
    return tuple(grid)


def _suffix_offset(baseline: TimeWindow, window: TimeWindow) -> int:
    """Index in the baseline grid at which the requested window begins.

    Both grids are floored to the hour and step by an hour, so the requested window's
    grid is exactly a suffix of the baseline's and the offset is a subtraction.
    """
    delta = floor_to(window.start, 60) - floor_to(baseline.start, 60)
    return max(0, int(delta.total_seconds() // 3600))


def _align(
    points: Sequence[SeriesPoint],
    grid: Sequence[datetime],
    *,
    fill: float | None,
) -> tuple[float | None, ...]:
    """Project an irregular series onto the bucket grid.

    `fill=0.0` for counts, where an absent bucket means "nothing happened"; `fill=None`
    for KPIs, where an absent bucket means "we did not measure" and must not become a
    fabricated observation.
    """
    observed = {point.bucket_start: float(point.value) for point in points}
    return tuple(observed.get(moment, fill) for moment in grid)


def _matrix_column(
    matrix: KpiMatrix,
    kpi: KpiName,
    grid: Sequence[datetime],
) -> tuple[float | None, ...]:
    """One KPI family from a matrix, re-aligned onto the service's own grid."""
    if kpi not in matrix.kpis:
        return tuple(None for _ in grid)
    index = matrix.kpis.index(kpi)
    observed = {
        bucket: row[index] for bucket, row in zip(matrix.bucket_starts, matrix.rows, strict=False)
    }
    return tuple(observed.get(moment) for moment in grid)


def _largest_rank_gap(
    ranked: Sequence[tuple[str, str, float | None, float | None]],
) -> float | None:
    """Largest |Pearson - Spearman| among the ranked pairs, when it is worth saying."""
    gaps = [
        abs(pearson_value - spearman_value)
        for _, _, pearson_value, spearman_value in ranked
        if pearson_value is not None and spearman_value is not None
    ]
    if not gaps:
        return None
    largest = max(gaps)
    return largest if largest >= 0.2 else None
