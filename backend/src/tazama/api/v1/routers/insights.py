"""Insights: the exploratory analytics, exposed with their caveats attached.

Every response here carries `caveats` and the `inputs` the analysis ran on, and the UI is
expected to show them. That is not decoration: a correlation over 400 hourly buckets with
30 gaps is a different claim from the same number over a clean series, and an insight
without its qualifications is how an analyst gets talked into a wrong decision.

The window is always explicit. Defaulting to "the last N days" relative to *now* would make
these endpoints non-reproducible, so the default is the last N days of **data** — the
latest KPI bucket in the database — and the resolved window is returned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from tazama.api.deps import ClockDep, SessionDep, requires
from tazama.core.errors import NotFound
from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.infrastructure.db.insights_repo import (
    SqlCellExposureReader,
    SqlComplaintSeriesReader,
    SqlKpiSeriesReader,
)
from tazama.infrastructure.db.models import Cell, KpiCell
from tazama.modules.insights.service import InsightsService

router = APIRouter(prefix="/insights", tags=["insights"])

#: Analysis window when the caller does not pin one. Two weeks of hourly buckets is 336
#: points: enough for an hour-of-week profile, short enough to answer quickly.
DEFAULT_DAYS = 14


class WindowOut(BaseModel):
    start: str
    end: str
    hours: int


class RateSeriesOut(BaseModel):
    scope: str
    window: WindowOut
    metric: str
    bucket_starts: list[str]
    counts: list[float]
    rate_per_1k: list[float]
    deseasonalised: list[float]
    subscribers_est: int
    per_subscribers: float
    gaps: int
    n_points: int
    caveats: list[str]


class LeadLagOut(BaseModel):
    scope: str
    kpi: str
    window: WindowOut
    lags: list[int]
    correlations: list[float | None]
    best_lag_hours: int | None
    best_correlation: float | None
    kpi_leads: bool
    zero_lag_correlation: float | None
    n_points: int
    caveats: list[str]
    #: Plain-language reading of the numbers above, so the panel does not require the
    #: reader to remember the sign convention.
    reading: str


class CorrelationPairOut(BaseModel):
    left: str
    right: str
    pearson: float | None
    spearman: float | None


class CorrelationOut(BaseModel):
    window: WindowOut
    region: str | None
    labels: list[str]
    pearson: list[list[float | None]]
    spearman: list[list[float | None]]
    top: list[CorrelationPairOut]
    n_points: int
    caveats: list[str]


class DriverOut(BaseModel):
    kpi: str
    score: float
    rank: int
    n_points: int
    caveats: list[str]


class DriversOut(BaseModel):
    window: WindowOut
    region: str | None
    drivers: list[DriverOut]


class PcaOut(BaseModel):
    window: WindowOut
    region: str | None
    labels: list[str]
    explained_variance_ratio: list[float]
    cumulative_variance: list[float]
    loadings: list[list[float]]
    dropped_labels: list[str]
    n_points: int
    caveats: list[str]


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _window_out(window: TimeWindow) -> WindowOut:
    return WindowOut(
        start=_iso(window.start),
        end=_iso(window.end),
        hours=int((window.end - window.start).total_seconds() // 3600),
    )


def _service(session: SessionDep, clock: ClockDep) -> InsightsService:
    """Built per request, like every other read path: the readers are session-scoped."""
    return InsightsService(
        kpis=SqlKpiSeriesReader(session),
        complaints=SqlComplaintSeriesReader(session),
        cells=SqlCellExposureReader(session),
        clock=clock,
    )


def _resolve_window(session: SessionDep, days: int, until: datetime | None) -> TimeWindow:
    """Anchor the window on the data, not on the wall clock.

    A demo database is seeded once and then sits still; anchoring on `now` would return
    empty series a day later and make every panel look broken.
    """
    end = until
    if end is None:
        latest = session.execute(select(func.max(KpiCell.bucket_start))).scalar_one_or_none()
        if latest is None:
            raise NotFound("There is no KPI data to analyse. Run the seed first.")
        end = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
        # The latest bucket is partial by definition; end the window at its start.
        end = end.replace(minute=0, second=0, microsecond=0)
    return TimeWindow(start=end - timedelta(days=days), end=end)


DaysQuery = Annotated[int, Query(ge=2, le=60, description="Window length in days")]
RegionQuery = Annotated[str | None, Query(max_length=32)]


@router.get(
    "/window",
    response_model=WindowOut,
    dependencies=[Depends(requires("insights:read"))],
    summary="The window the other endpoints default to",
)
async def default_window(
    session: SessionDep,
    days: DaysQuery = DEFAULT_DAYS,
) -> WindowOut:
    return _window_out(_resolve_window(session, days, None))


@router.get(
    "/complaint-rate",
    response_model=RateSeriesOut,
    dependencies=[Depends(requires("insights:read"))],
    summary="Complaints per 1,000 subscribers per hour",
)
async def complaint_rate(
    session: SessionDep,
    clock: ClockDep,
    days: DaysQuery = DEFAULT_DAYS,
    region: RegionQuery = None,
    cell_id: Annotated[str | None, Query(max_length=40)] = None,
) -> RateSeriesOut:
    window = _resolve_window(session, days, None)
    result = _service(session, clock).complaint_rate(
        window=window,
        region=region,
        cell_id=cell_id,
    )
    return RateSeriesOut(
        scope=result.scope,
        window=_window_out(result.window),
        metric=result.metric,
        bucket_starts=[_iso(value) for value in result.bucket_starts],
        counts=list(result.counts),
        rate_per_1k=list(result.rate_per_1k),
        deseasonalised=list(result.deseasonalised),
        subscribers_est=result.subscribers_est,
        per_subscribers=result.per_subscribers,
        gaps=result.gaps,
        n_points=result.n_points,
        caveats=list(result.caveats),
    )


@router.get(
    "/lead-lag",
    response_model=LeadLagOut,
    dependencies=[Depends(requires("insights:read"))],
    summary="Does a KPI move before the complaints?",
)
async def lead_lag(
    session: SessionDep,
    clock: ClockDep,
    cell_id: Annotated[str, Query(min_length=1, max_length=40)],
    kpi: KpiName = KpiName.AVAILABILITY,
    days: DaysQuery = DEFAULT_DAYS,
    max_lag_hours: Annotated[int, Query(ge=1, le=24)] = 6,
) -> LeadLagOut:
    # The enum is the query type, so an unknown KPI name is a 422 from FastAPI rather than
    # a stringly-typed value that only fails deep inside the service.
    window = _resolve_window(session, days, None)
    result = _service(session, clock).lead_lag(
        cell_id=cell_id,
        kpi=kpi,
        window=window,
        max_lag_hours=max_lag_hours,
    )

    if result.best_lag_hours is None or result.best_correlation is None:
        reading = "No usable relationship in this window."
    elif result.best_lag_hours > 0:
        reading = (
            f"{result.kpi} moves about {result.best_lag_hours}h before the complaint rate "
            f"(r={result.best_correlation:.2f}) — there is warning time here."
        )
    elif result.best_lag_hours == 0:
        reading = (
            f"{result.kpi} and the complaint rate move together with no measurable lead "
            f"(r={result.best_correlation:.2f})."
        )
    else:
        reading = (
            f"The complaint rate moves about {abs(result.best_lag_hours)}h before "
            f"{result.kpi} (r={result.best_correlation:.2f}) — customers notice first."
        )

    return LeadLagOut(
        scope=result.scope,
        kpi=str(result.kpi),
        window=_window_out(result.window),
        lags=list(result.lags),
        correlations=list(result.correlations),
        best_lag_hours=result.best_lag_hours,
        best_correlation=result.best_correlation,
        kpi_leads=result.kpi_leads,
        zero_lag_correlation=result.zero_lag_correlation,
        n_points=result.n_points,
        caveats=list(result.caveats),
        reading=reading,
    )


@router.get(
    "/correlations",
    response_model=CorrelationOut,
    dependencies=[Depends(requires("insights:read"))],
    summary="KPI/complaint correlation heatmap and ranked pairs",
)
async def correlations(
    session: SessionDep,
    clock: ClockDep,
    days: DaysQuery = DEFAULT_DAYS,
    region: RegionQuery = None,
) -> CorrelationOut:
    window = _resolve_window(session, days, None)
    result = _service(session, clock).correlation_matrix(window=window, region=region)
    return CorrelationOut(
        window=_window_out(result.window),
        region=result.region,
        labels=list(result.labels),
        pearson=[list(row) for row in result.pearson],
        spearman=[list(row) for row in result.spearman],
        top=[
            CorrelationPairOut(
                left=pair.left,
                right=pair.right,
                pearson=pair.pearson,
                spearman=pair.spearman,
            )
            for pair in result.top
        ],
        n_points=result.n_points,
        caveats=list(result.caveats),
    )


@router.get(
    "/drivers",
    response_model=DriversOut,
    dependencies=[Depends(requires("insights:read"))],
    summary="Mutual-information shortlist of candidate drivers",
)
async def drivers(
    session: SessionDep,
    clock: ClockDep,
    days: DaysQuery = DEFAULT_DAYS,
    region: RegionQuery = None,
) -> DriversOut:
    window = _resolve_window(session, days, None)
    scores = _service(session, clock).driver_candidates(window=window, region=region)
    return DriversOut(
        window=_window_out(window),
        region=region,
        drivers=[
            DriverOut(
                kpi=str(score.kpi),
                score=score.score,
                rank=score.rank,
                n_points=score.n_points,
                caveats=list(score.caveats),
            )
            for score in scores
        ],
    )


@router.get(
    "/pca",
    response_model=PcaOut,
    dependencies=[Depends(requires("insights:read"))],
    summary="Scree and loadings over the standardised KPI matrix",
)
async def pca(
    session: SessionDep,
    clock: ClockDep,
    days: DaysQuery = DEFAULT_DAYS,
    region: RegionQuery = None,
) -> PcaOut:
    window = _resolve_window(session, days, None)
    result = _service(session, clock).pca(window=window, region=region)
    return PcaOut(
        window=_window_out(result.window),
        region=result.region,
        labels=list(result.labels),
        explained_variance_ratio=list(result.explained_variance_ratio),
        cumulative_variance=list(result.cumulative_variance),
        loadings=[list(row) for row in result.loadings],
        dropped_labels=list(result.dropped_labels),
        n_points=result.n_points,
        caveats=list(result.caveats),
    )


class BusyCellOut(BaseModel):
    cell_id: str
    name: str
    region: str
    tech: str
    complaints: int


@router.get(
    "/busiest-cells",
    response_model=list[BusyCellOut],
    dependencies=[Depends(requires("insights:read"))],
    summary="Cells with the most complaints — the lead-lag picker's shortlist",
)
async def busiest_cells(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
) -> list[BusyCellOut]:
    """Which cells are worth running a lead-lag on.

    A per-cell analysis on a quiet cell is noise, so the UI needs somewhere sensible to
    start rather than a free-text cell id the reviewer has to guess.
    """
    from tazama.infrastructure.db.models import Complaint

    rows: Any = session.execute(
        select(
            Complaint.cell_id,
            Cell.name,
            Cell.region,
            Cell.tech,
            func.count().label("complaints"),
        )
        .join(Cell, Cell.id == Complaint.cell_id)
        .where(Complaint.cell_id.is_not(None))
        .group_by(Complaint.cell_id, Cell.name, Cell.region, Cell.tech)
        .order_by(func.count().desc(), Complaint.cell_id)
        .limit(limit),
    ).all()
    return [
        BusyCellOut(
            cell_id=row[0],
            name=row[1],
            region=row[2],
            tech=row[3],
            complaints=int(row[4]),
        )
        for row in rows
    ]
