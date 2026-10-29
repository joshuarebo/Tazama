"""Ports for the insights module.

Everything the service needs from the outside world is declared here as a Protocol and
satisfied structurally by an adapter in `infrastructure/db/insights_repo.py`. The module
therefore never imports infrastructure, and the service can be tested against in-memory
fakes built from a list of numbers.

The read ports are deliberately coarse: each method returns an *already aggregated*
series or matrix. `kpi_cell` holds ~835k rows in the seeded dataset, and a port that
handed back raw samples would make it impossible for an adapter to keep the aggregation
in SQL. "One hourly series" and "one bucket-aligned matrix" are the only shapes the
statistics need, so they are the only shapes the ports offer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.modules.insights.models import KpiMatrix, SeriesPoint


@runtime_checkable
class KpiSeriesReader(Protocol):
    """Hourly KPI series for one cell, and a bucket-aligned matrix of all families."""

    def hourly_series(
        self,
        *,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[SeriesPoint, ...]:
        """One KPI for one cell over `[window.start, window.end)`, ordered by bucket.

        Buckets with no sample are absent rather than null-filled: the caller compares the
        count against the expected bucket count and reports the gap as a caveat.
        """
        ...

    def hourly_matrix(
        self,
        *,
        window: TimeWindow,
        kpis: Sequence[KpiName],
        cell_ids: Sequence[str] | None = None,
        region: str | None = None,
        granularity: str = "1h",
    ) -> KpiMatrix:
        """All requested KPI families on one bucket grid, averaged over the cells.

        With neither `cell_ids` nor `region` the whole network is averaged. The average is
        taken in SQL; the adapter never returns a per-cell sample.
        """
        ...


@runtime_checkable
class ComplaintSeriesReader(Protocol):
    """Hourly complaint counts, bucketed in SQL, by cell or by region."""

    def hourly_counts_by_cell(
        self,
        *,
        cell_id: str,
        window: TimeWindow,
        network_only: bool = True,
    ) -> tuple[SeriesPoint, ...]:
        """Complaints per hour for one cell. `network_only` excludes billing and SIM swap.

        Buckets with no complaints are absent, not zero: only the caller knows the bucket
        grid it wants, and a complaint series is mostly empty for most cells.
        """
        ...

    def hourly_counts_by_region(
        self,
        *,
        window: TimeWindow,
        region: str | None = None,
        network_only: bool = True,
    ) -> tuple[SeriesPoint, ...]:
        """Complaints per hour across a region, or the whole network when region is None."""
        ...


@runtime_checkable
class CellExposureReader(Protocol):
    """`cell.subscribers_est` — the denominator that makes counts comparable."""

    def subscribers_est(self, cell_id: str) -> int | None:
        """Estimated subscribers on one cell, or None when the cell is unknown."""
        ...

    def total_subscribers_est(self, *, region: str | None = None) -> int:
        """Summed estimate over a region, or the whole network when region is None."""
        ...

    def cell_ids(self, *, region: str | None = None, limit: int = 400) -> tuple[str, ...]:
        """Active cells, in a stable order, capped so a region-wide call stays bounded."""
        ...


@runtime_checkable
class DecisionLog(Protocol):
    """Append-only "why" record. Identical signature in every module."""

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
    ) -> None: ...
