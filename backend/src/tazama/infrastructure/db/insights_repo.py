"""SQLAlchemy adapters for the insights read ports.

Four rules this file exists to obey:

* **Aggregate in SQL.** The seeded dataset holds ~835k `kpi_cell` rows and ~20k
  complaints. Nothing here ever pulls a raw KPI table into the process: a series is one
  row per bucket, a matrix is one row per bucket with the KPI families averaged by the
  database, and complaints are counted per hour by a `GROUP BY` on a truncated timestamp.
* **Bucket complaints in SQL, dialect-aware.** `complaint` stores an instant, not a
  bucket, so the hourly grid has to be produced by the database. SQLite needs `strftime`
  over its text timestamps; Oracle needs `TRUNC(ts, 'HH')`. That split is the only place
  this module cares which backend it is on, and it is isolated in `_hour_bucket`.
* **Named columns, bound parameters, time first.** Every statement names its columns,
  binds every value, and filters on the time range before anything else, which is what
  keeps it partition-friendly on Oracle. No `SELECT *` on a large table, ever.
* **A session when there is one, an engine otherwise.** Reads taken inside the caller's
  transaction see the caller's uncommitted writes, which the tests rely on; with no
  session a short-lived connection is used instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, Select, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tazama.core.logging import get_logger
from tazama.domain.value_objects import ComplaintCategory, KpiName, TimeWindow
from tazama.infrastructure.db.engine import get_engine
from tazama.infrastructure.db.models import Cell, Complaint, KpiCell
from tazama.modules.insights.models import KpiMatrix, SeriesPoint

_log = get_logger(__name__)

#: KPI name -> the `kpi_cell` column it lives in. Identifiers come from our own metadata,
#: never from input, which is what makes the generated SQL safe by construction.
KPI_COLUMNS: dict[KpiName, Any] = {
    KpiName.AVAILABILITY: KpiCell.availability_pct,
    KpiName.RRC_SETUP_SUCCESS: KpiCell.rrc_setup_success_pct,
    KpiName.ERAB_SETUP_SUCCESS: KpiCell.erab_setup_success_pct,
    KpiName.DROP_RATE: KpiCell.drop_rate_pct,
    KpiName.PRB_UTILISATION: KpiCell.prb_utilisation_pct,
    KpiName.DL_THROUGHPUT: KpiCell.dl_throughput_mbps,
    KpiName.UL_THROUGHPUT: KpiCell.ul_throughput_mbps,
    KpiName.LATENCY: KpiCell.latency_ms,
    KpiName.ACTIVE_USERS: KpiCell.active_users,
    KpiName.SINR: KpiCell.sinr_db,
}

#: The event time of a complaint: when the customer suffered, not when we heard.
_event_time = func.coalesce(Complaint.source_ts, Complaint.received_at)

#: How late a complaint may arrive and still be found by an event-time query. The band on
#: `received_at` keeps that index usable while the exact filter runs on event time.
COMPLAINT_MAX_LAG = timedelta(hours=36)

#: Categories that point at the network. Billing and SIM-swap complaints must not enter a
#: network complaint rate: the non-network surge scenario depends on the distinction.
NETWORK_CATEGORY_VALUES: tuple[str, ...] = tuple(
    sorted(
        category.value
        for category in (
            ComplaintCategory.NO_SERVICE,
            ComplaintCategory.SLOW_DATA,
            ComplaintCategory.CALL_DROPS,
            ComplaintCategory.VOICE_QUALITY,
            ComplaintCategory.NETWORK_CONGESTION,
        )
    ),
)

_SQLITE_BUCKET_FORMAT = "%Y-%m-%d %H:00:00"


class _SqlAdapter:
    """Shared plumbing: run a statement on the caller's session, or on a connection."""

    __slots__ = ("_engine", "_session")

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        self._session = session
        self._engine = engine

    def _rows(self, statement: Select[Any]) -> Sequence[Row[Any]]:
        if self._session is not None:
            return self._session.execute(statement).all()
        with (self._engine or get_engine()).connect() as connection:
            return connection.execute(statement).all()

    def _scalar(self, statement: Select[Any]) -> Any:
        if self._session is not None:
            return self._session.execute(statement).scalar_one_or_none()
        with (self._engine or get_engine()).connect() as connection:
            return connection.execute(statement).scalar_one_or_none()

    def _dialect(self) -> str:
        if self._session is not None:
            return str(self._session.get_bind().dialect.name)
        return str((self._engine or get_engine()).dialect.name)


def _hour_bucket(dialect: str, column: ColumnElement[Any]) -> ColumnElement[Any]:
    """`column` truncated to the start of its hour, as a SQL expression.

    Oracle truncates a timestamp natively. SQLite stores timestamps as text and has no
    date type, so `strftime` rebuilds the bucket as a string — which is still a group key
    the database computes, and `_as_bucket` converts back on the way out.
    """
    if dialect == "oracle":
        return func.trunc(column, "HH")
    return func.strftime(_SQLITE_BUCKET_FORMAT, column)


def _as_bucket(value: Any) -> datetime:
    """Normalise whatever the backend returned for a truncated hour into aware UTC."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


# ===================================================================== KPI reads


class SqlKpiSeriesReader(_SqlAdapter):
    """Hourly KPI series for one cell, and a bucket-aligned matrix of all families."""

    def hourly_series(
        self,
        *,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[SeriesPoint, ...]:
        column = KPI_COLUMNS[kpi]
        statement = (
            select(KpiCell.bucket_start, column)
            .where(
                KpiCell.granularity == granularity,
                KpiCell.bucket_start >= window.start,
                KpiCell.bucket_start < window.end,
                KpiCell.cell_id == cell_id,
                column.is_not(None),
            )
            .order_by(KpiCell.bucket_start)
        )
        return tuple(
            SeriesPoint(bucket_start=_as_bucket(row[0]), value=float(row[1]))
            for row in self._rows(statement)
        )

    def hourly_matrix(
        self,
        *,
        window: TimeWindow,
        kpis: Sequence[KpiName],
        cell_ids: Sequence[str] | None = None,
        region: str | None = None,
        granularity: str = "1h",
    ) -> KpiMatrix:
        """All requested families on one bucket grid, averaged over the matching cells.

        The average is a plain unweighted mean over the cells present in each bucket. That
        is deliberate: weighting by subscribers would turn a KPI average into an exposure
        measure, and exposure is what the complaint-rate column already carries.
        """
        families = tuple(kpis)
        if not families:
            return KpiMatrix(window=window, bucket_starts=(), kpis=(), rows=(), region=region)
        if cell_ids is not None and not cell_ids:
            return KpiMatrix(
                window=window,
                bucket_starts=(),
                kpis=families,
                rows=(),
                region=region,
            )

        columns: list[Any] = [KpiCell.bucket_start.label("bucket_start")]
        columns += [func.avg(KPI_COLUMNS[kpi]).label(f"v_{kpi.name.lower()}") for kpi in families]
        columns.append(func.count(func.distinct(KpiCell.cell_id)).label("cells"))
        statement = (
            select(*columns)
            .where(
                KpiCell.granularity == granularity,
                KpiCell.bucket_start >= window.start,
                KpiCell.bucket_start < window.end,
            )
            .group_by(KpiCell.bucket_start)
            .order_by(KpiCell.bucket_start)
        )
        if region is not None:
            statement = statement.where(KpiCell.region == region)
        if cell_ids is not None:
            statement = statement.where(KpiCell.cell_id.in_(list(cell_ids)))

        buckets: list[datetime] = []
        rows: list[tuple[float | None, ...]] = []
        widest = 0
        for row in self._rows(statement):
            mapping = row._mapping
            buckets.append(_as_bucket(mapping["bucket_start"]))
            rows.append(
                tuple(
                    None
                    if mapping[f"v_{kpi.name.lower()}"] is None
                    else float(mapping[f"v_{kpi.name.lower()}"])
                    for kpi in families
                ),
            )
            widest = max(widest, int(mapping["cells"] or 0))
        return KpiMatrix(
            window=window,
            bucket_starts=tuple(buckets),
            kpis=families,
            rows=tuple(rows),
            cells=widest,
            region=region,
        )


# =============================================================== complaint reads


class SqlComplaintSeriesReader(_SqlAdapter):
    """Hourly complaint counts, bucketed by the database, by cell or by region."""

    def hourly_counts_by_cell(
        self,
        *,
        cell_id: str,
        window: TimeWindow,
        network_only: bool = True,
    ) -> tuple[SeriesPoint, ...]:
        return self._counts(
            window=window,
            network_only=network_only,
            cell_id=cell_id,
            region=None,
        )

    def hourly_counts_by_region(
        self,
        *,
        window: TimeWindow,
        region: str | None = None,
        network_only: bool = True,
    ) -> tuple[SeriesPoint, ...]:
        return self._counts(
            window=window,
            network_only=network_only,
            cell_id=None,
            region=region,
        )

    def _counts(
        self,
        *,
        window: TimeWindow,
        network_only: bool,
        cell_id: str | None,
        region: str | None,
    ) -> tuple[SeriesPoint, ...]:
        bucket = _hour_bucket(self._dialect(), _event_time).label("bucket_start")
        statement = (
            select(bucket, func.count().label("complaints"))
            .where(
                # Index-usable band on received_at first, then the exact event-time filter.
                Complaint.received_at >= window.start,
                Complaint.received_at < window.end + COMPLAINT_MAX_LAG,
                _event_time >= window.start,
                _event_time < window.end,
            )
            .group_by(bucket)
            .order_by(bucket)
        )
        if network_only:
            statement = statement.where(Complaint.category.in_(NETWORK_CATEGORY_VALUES))
        if cell_id is not None:
            statement = statement.where(Complaint.cell_id == cell_id)
        if region is not None:
            statement = statement.where(Complaint.region == region)
        return tuple(
            SeriesPoint(bucket_start=_as_bucket(row[0]), value=float(row[1] or 0))
            for row in self._rows(statement)
        )


# ============================================================ exposure reads


class SqlCellExposureReader(_SqlAdapter):
    """`cell.subscribers_est` — the denominator that makes counts comparable.

    Per-cell estimates are memoised per adapter instance: they do not change inside an
    analysis, and the alternative is one single-row query per cell while an event study
    walks a few hundred of them.
    """

    __slots__ = ("_cache",)

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        super().__init__(session, engine=engine)
        self._cache: dict[str, int | None] = {}

    def subscribers_est(self, cell_id: str) -> int | None:
        if cell_id not in self._cache:
            statement = select(Cell.subscribers_est).where(Cell.id == cell_id)
            value = self._scalar(statement)
            self._cache[cell_id] = None if value is None else int(value)
        return self._cache[cell_id]

    def total_subscribers_est(self, *, region: str | None = None) -> int:
        statement = select(func.sum(Cell.subscribers_est)).where(Cell.status == "active")
        if region is not None:
            statement = statement.where(Cell.region == region)
        return int(self._scalar(statement) or 0)

    def cell_ids(self, *, region: str | None = None, limit: int = 400) -> tuple[str, ...]:
        statement = (
            select(Cell.id).where(Cell.status == "active").order_by(Cell.id).limit(max(1, limit))
        )
        if region is not None:
            statement = statement.where(Cell.region == region)
        return tuple(str(row[0]) for row in self._rows(statement))
