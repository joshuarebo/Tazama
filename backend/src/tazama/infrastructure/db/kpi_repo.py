"""SQLAlchemy adapters for the correlation and impact read ports.

Three rules this file exists to obey:

* **Aggregate in SQL.** `kpi_cell` holds ~835k rows. Breach detection is a two-level
  aggregate (per bucket, then per cell) that returns one row per degraded cell, so the
  engine never sees a KPI sample it did not ask for. The same goes for complaint counts
  and enterprise exposure.
* **Bound parameters, named columns, time-bounded.** Every threshold and id is bound;
  every statement names its columns; every statement filters on a time range first, which
  is what keeps it partition-friendly on Oracle.
* **A session when there is one, an engine otherwise.** Reads taken inside the caller's
  transaction see the caller's uncommitted writes, which the pipeline relies on; with no
  session, a short-lived connection is used instead.

Reference data (cell geography, topology edges) is memoised per adapter instance. It does
not change inside a pipeline run, and the alternative is thousands of identical
single-row queries while clustering complaints.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import Row, Select, and_, case, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tazama.core.logging import get_logger
from tazama.domain.value_objects import (
    KPI_POLARITY,
    AlarmSeverity,
    ComplaintCategory,
    KpiName,
    NodeKind,
    TimeWindow,
)
from tazama.infrastructure.db.engine import get_engine
from tazama.infrastructure.db.models import (
    Alarm,
    Cell,
    Complaint,
    KpiCell,
    MaintenanceWindow,
    Subscriber,
    TopologyEdge,
    TopologyNode,
)
from tazama.modules.correlation.models import (
    AlarmRecord,
    AlarmTransition,
    CellInfo,
    ComplaintCount,
    ComplaintObservation,
    KpiBreach,
    KpiSample,
    MaintenanceCover,
    TopologyNodeInfo,
)
from tazama.modules.impact.models import CellFacts, EnterpriseExposure

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

GRANULARITY_MINUTES: dict[str, int] = {"15m": 15, "1h": 60}

#: How far before a window the alarm reader looks for alarms that are still open. An
#: alarm raised earlier and never cleared is live evidence; an unbounded scan is not.
ALARM_OPEN_LOOKBACK = timedelta(days=7)

#: How late a complaint may arrive and still be found by an event-time query. The seed's
#: worst case is ~9 hours; a day of slack keeps the `received_at` index usable while the
#: exact filter still runs on event time.
COMPLAINT_MAX_LAG = timedelta(hours=36)

#: Topology is at most core -> aggregation -> link -> site -> cell.
MAX_TOPOLOGY_DEPTH = 6

#: Guard against a pathological fan-out when walking descendants.
MAX_DESCENDANTS = 5000


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


# ===================================================================== KPI reads


def _add_all(terms: Sequence[ColumnElement[int]]) -> ColumnElement[int]:
    """Sum SQL expressions without a Python `0` sneaking into the expression tree."""
    total = terms[0]
    for term in terms[1:]:
        total = total + term
    return total


def _granularity_step(granularity: str) -> timedelta:
    minutes = GRANULARITY_MINUTES.get(granularity)
    if minutes is None:
        raise ValueError(f"unknown KPI granularity {granularity!r}")
    return timedelta(minutes=minutes)


def _breach_condition(kpi: KpiName) -> ColumnElement[bool]:
    """`value < threshold` or `value > threshold`, per the KPI's polarity."""
    column = KPI_COLUMNS[kpi]
    lower_is_worse, threshold = KPI_POLARITY[kpi]
    comparison = column < threshold if lower_is_worse else column > threshold
    return and_(column.is_not(None), comparison)


class SqlKpiReader(_SqlAdapter):
    """Cell KPIs, with breach detection pushed into the database."""

    def series(
        self,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[KpiSample, ...]:
        column = KPI_COLUMNS[kpi]
        statement = (
            select(KpiCell.bucket_start, column)
            .where(
                KpiCell.cell_id == cell_id,
                KpiCell.granularity == granularity,
                KpiCell.bucket_start >= window.start,
                KpiCell.bucket_start < window.end,
            )
            .order_by(KpiCell.bucket_start)
        )
        return tuple(
            KpiSample(cell_id=cell_id, bucket_start=row[0], value=row[1])
            for row in self._rows(statement)
        )

    def baseline(
        self,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> float | None:
        column = KPI_COLUMNS[kpi]
        statement = select(func.avg(column)).where(
            KpiCell.cell_id == cell_id,
            KpiCell.granularity == granularity,
            KpiCell.bucket_start >= window.start,
            KpiCell.bucket_start < window.end,
        )
        value = self._scalar(statement)
        return None if value is None else float(value)

    def breaches(
        self,
        window: TimeWindow,
        granularity: str = "1h",
        *,
        cell_ids: Sequence[str] | None = None,
        min_severity: float = 0.0,
    ) -> tuple[KpiBreach, ...]:
        """One row per cell whose KPIs breached, aggregated entirely in SQL.

        The inner query scores each (cell, bucket): how many KPI families breached and how
        many were present. The outer query keeps only the buckets whose breach share
        reaches `min_severity` and collapses them to one row per cell — so a cell that
        merely touched its PRB threshold in the busy hour never reaches the engine, while
        a cell whose accessibility, retainability and integrity all collapsed does.
        """
        step = _granularity_step(granularity)
        breach_flags = {kpi: _breach_condition(kpi) for kpi in KPI_COLUMNS}
        breached_count = _add_all(
            [case((flag, 1), else_=0) for flag in breach_flags.values()],
        )
        present_count = _add_all(
            [case((column.is_not(None), 1), else_=0) for column in KPI_COLUMNS.values()],
        )
        inner_columns: list[Any] = [
            KpiCell.cell_id.label("cell_id"),
            KpiCell.site_id.label("site_id"),
            KpiCell.region.label("region"),
            KpiCell.bucket_start.label("bucket_start"),
            breached_count.label("breached"),
            present_count.label("present"),
        ]
        for kpi, column in KPI_COLUMNS.items():
            inner_columns.append(column.label(f"v_{kpi.name.lower()}"))
            inner_columns.append(
                case((breach_flags[kpi], 1), else_=0).label(f"b_{kpi.name.lower()}"),
            )
        inner = select(*inner_columns).where(
            KpiCell.granularity == granularity,
            KpiCell.bucket_start >= window.start,
            KpiCell.bucket_start < window.end,
        )
        if cell_ids is not None:
            if not cell_ids:
                return ()
            inner = inner.where(KpiCell.cell_id.in_(list(cell_ids)))
        sub = inner.subquery("scored")

        degraded = and_(
            sub.c.present > 0,
            sub.c.breached >= sub.c.present * float(min_severity),
            sub.c.breached > 0,
        )
        outer_columns: list[Any] = [
            sub.c.cell_id,
            sub.c.site_id,
            sub.c.region,
            func.count().label("buckets"),
            func.sum(case((degraded, 1), else_=0)).label("bad_buckets"),
            func.min(case((degraded, sub.c.bucket_start), else_=None)).label("first_bad"),
            func.max(case((degraded, sub.c.bucket_start), else_=None)).label("last_bad"),
            func.sum(case((degraded, sub.c.breached), else_=0)).label("bad_breached"),
            func.sum(case((degraded, sub.c.present), else_=0)).label("bad_present"),
        ]
        for kpi in KPI_COLUMNS:
            value_column = sub.c[f"v_{kpi.name.lower()}"]
            lower_is_worse, _ = KPI_POLARITY[kpi]
            aggregate = func.min if lower_is_worse else func.max
            outer_columns.append(
                aggregate(case((degraded, value_column), else_=None)).label(
                    f"worst_{kpi.name.lower()}",
                ),
            )
            outer_columns.append(
                func.sum(
                    case((degraded, sub.c[f"b_{kpi.name.lower()}"]), else_=0),
                ).label(f"n_{kpi.name.lower()}"),
            )
        statement = (
            select(*outer_columns)
            .group_by(sub.c.cell_id, sub.c.site_id, sub.c.region)
            .having(func.sum(case((degraded, 1), else_=0)) > 0)
            .order_by(sub.c.cell_id)
        )

        results: list[KpiBreach] = []
        for row in self._rows(statement):
            mapping = row._mapping
            bad_buckets = int(mapping["bad_buckets"] or 0)
            bad_present = int(mapping["bad_present"] or 0)
            bad_breached = int(mapping["bad_breached"] or 0)
            severity = (bad_breached / bad_present) if bad_present else 0.0
            first_bad = mapping["first_bad"]
            last_bad = mapping["last_bad"]
            if first_bad is None or last_bad is None:  # pragma: no cover - HAVING excludes
                continue
            breached_kpis = tuple(
                kpi for kpi in KPI_COLUMNS if int(mapping[f"n_{kpi.name.lower()}"] or 0) > 0
            )
            worst = {
                kpi.value: float(mapping[f"worst_{kpi.name.lower()}"])
                for kpi in KPI_COLUMNS
                if mapping[f"worst_{kpi.name.lower()}"] is not None
            }
            results.append(
                KpiBreach(
                    cell_id=str(mapping["cell_id"]),
                    site_id=str(mapping["site_id"]),
                    region=str(mapping["region"]),
                    window=TimeWindow(start=first_bad, end=last_bad + step),
                    buckets=int(mapping["buckets"] or 0),
                    breached_buckets=bad_buckets,
                    severity=round(severity, 4),
                    breached_kpis=breached_kpis,
                    worst=worst,
                ),
            )
        return tuple(results)


class SqlKpiFacts(_SqlAdapter):
    """The impact module's narrow KPI port: which families broke, nothing else."""

    def breached_kpis(
        self,
        cell_ids: Sequence[str],
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[KpiName, ...]:
        if not cell_ids:
            return ()
        columns = [
            func.sum(case((_breach_condition(kpi), 1), else_=0)).label(kpi.name.lower())
            for kpi in KPI_COLUMNS
        ]
        statement = select(*columns).where(
            KpiCell.cell_id.in_(list(cell_ids)),
            KpiCell.granularity == granularity,
            KpiCell.bucket_start >= window.start,
            KpiCell.bucket_start < window.end,
        )
        rows = self._rows(statement)
        if not rows:
            return ()
        mapping = rows[0]._mapping
        return tuple(kpi for kpi in KPI_COLUMNS if int(mapping[kpi.name.lower()] or 0) > 0)


# =================================================================== alarm reads


def _alarm_record(row: Row[Any]) -> AlarmRecord:
    mapping = row._mapping
    return AlarmRecord(
        alarm_id=str(mapping["id"]),
        node_id=str(mapping["node_id"]),
        node_kind=str(mapping["node_kind"]),
        alarm_type=str(mapping["alarm_type"]),
        severity=AlarmSeverity(mapping["severity"]),
        raised_at=mapping["raised_at"],
        cleared_at=mapping["cleared_at"],
        cell_id=mapping["cell_id"],
        site_id=mapping["site_id"],
        is_flapping=bool(mapping["is_flapping"]),
    )


_ALARM_COLUMNS = (
    Alarm.id,
    Alarm.node_id,
    Alarm.node_kind,
    Alarm.alarm_type,
    Alarm.severity,
    Alarm.raised_at,
    Alarm.cleared_at,
    Alarm.cell_id,
    Alarm.site_id,
    Alarm.is_flapping,
)


class SqlAlarmReader(_SqlAdapter):
    def active(self, window: TimeWindow) -> tuple[AlarmRecord, ...]:
        statement = (
            select(*_ALARM_COLUMNS)
            .where(
                # Lower bound keeps the index on (node_id, raised_at) usable; the OR picks
                # up alarms raised earlier that were still open during the window.
                Alarm.raised_at >= window.start - ALARM_OPEN_LOOKBACK,
                Alarm.raised_at < window.end,
                or_(Alarm.cleared_at.is_(None), Alarm.cleared_at > window.start),
            )
            .order_by(Alarm.raised_at, Alarm.id)
        )
        return tuple(_alarm_record(row) for row in self._rows(statement))

    def for_element(self, element_id: str, window: TimeWindow) -> tuple[AlarmRecord, ...]:
        statement = (
            select(*_ALARM_COLUMNS)
            .where(
                Alarm.node_id == element_id,
                Alarm.raised_at >= window.start - ALARM_OPEN_LOOKBACK,
                Alarm.raised_at < window.end,
                or_(Alarm.cleared_at.is_(None), Alarm.cleared_at > window.start),
            )
            .order_by(Alarm.raised_at, Alarm.id)
        )
        return tuple(_alarm_record(row) for row in self._rows(statement))

    def transitions(self, element_id: str, window: TimeWindow) -> tuple[AlarmTransition, ...]:
        """Each alarm contributes a raise and, if it cleared in the window, a clear."""
        statement = (
            select(Alarm.raised_at, Alarm.cleared_at)
            .where(
                Alarm.node_id == element_id,
                Alarm.raised_at >= window.start - ALARM_OPEN_LOOKBACK,
                Alarm.raised_at < window.end,
            )
            .order_by(Alarm.raised_at)
        )
        found: list[AlarmTransition] = []
        for raised_at, cleared_at in self._rows(statement):
            if window.contains(raised_at):
                found.append(
                    AlarmTransition(node_id=element_id, at=raised_at, kind="raise"),
                )
            if cleared_at is not None and window.contains(cleared_at):
                found.append(
                    AlarmTransition(node_id=element_id, at=cleared_at, kind="clear"),
                )
        found.sort(key=lambda item: (item.at, item.kind))
        return tuple(found)


# ============================================================= maintenance reads


class SqlMaintenanceReader(_SqlAdapter):
    def covering(self, element_id: str, window: TimeWindow) -> tuple[MaintenanceCover, ...]:
        statement = (
            select(
                MaintenanceWindow.id,
                MaintenanceWindow.node_id,
                MaintenanceWindow.node_kind,
                MaintenanceWindow.starts_at,
                MaintenanceWindow.ends_at,
                MaintenanceWindow.approved_by,
                MaintenanceWindow.description,
            )
            .where(
                MaintenanceWindow.node_id == element_id,
                MaintenanceWindow.starts_at < window.end,
                MaintenanceWindow.ends_at > window.start,
            )
            .order_by(MaintenanceWindow.starts_at)
        )
        return tuple(
            MaintenanceCover(
                maintenance_id=str(row[0]),
                node_id=str(row[1]),
                node_kind=str(row[2]),
                starts_at=row[3],
                ends_at=row[4],
                approved_by=row[5],
                description=row[6] or "",
            )
            for row in self._rows(statement)
        )


# ============================================================== complaint reads

#: The event time of a complaint: when the customer suffered, not when we heard.
_event_time = func.coalesce(Complaint.source_ts, Complaint.received_at)


class SqlComplaintReader(_SqlAdapter):
    """Located complaints only: an unlocated complaint is site identification's problem."""

    def in_window(
        self,
        window: TimeWindow,
        cell_ids: Sequence[str] | None = None,
    ) -> tuple[ComplaintObservation, ...]:
        statement = (
            select(
                Complaint.id,
                Complaint.cell_id,
                Complaint.site_id,
                Complaint.region,
                Complaint.category,
                Complaint.received_at,
                Complaint.source_ts,
                Complaint.dedupe_key,
                Complaint.subscriber_id,
                Complaint.channel,
            )
            .where(
                Complaint.cell_id.is_not(None),
                # Index-usable band on received_at, plus the exact event-time filter.
                Complaint.received_at >= window.start,
                Complaint.received_at < window.end + COMPLAINT_MAX_LAG,
                _event_time >= window.start,
                _event_time < window.end,
            )
            .order_by(_event_time, Complaint.id)
        )
        if cell_ids is not None:
            if not cell_ids:
                return ()
            statement = statement.where(Complaint.cell_id.in_(list(cell_ids)))
        return tuple(_observation(row) for row in self._rows(statement))

    def counts_by_cell(self, window: TimeWindow) -> tuple[ComplaintCount, ...]:
        """Raw and distinct-by-dedupe-key counts per cell, for network categories only."""
        statement = (
            select(
                Complaint.cell_id,
                func.count().label("total"),
                func.count(func.distinct(func.coalesce(Complaint.dedupe_key, Complaint.id))).label(
                    "distinct_keys",
                ),
            )
            .where(
                Complaint.cell_id.is_not(None),
                Complaint.category.in_(_NETWORK_CATEGORY_VALUES),
                Complaint.received_at >= window.start,
                Complaint.received_at < window.end + COMPLAINT_MAX_LAG,
                _event_time >= window.start,
                _event_time < window.end,
            )
            .group_by(Complaint.cell_id)
            .order_by(Complaint.cell_id)
        )
        return tuple(
            ComplaintCount(
                cell_id=str(row[0]),
                total=int(row[1] or 0),
                distinct=int(row[2] or 0),
            )
            for row in self._rows(statement)
        )

    def baseline_rate(self, cell_id: str, window: TimeWindow) -> float:
        """Network complaints per hour for this cell over the reference window."""
        hours = window.duration.total_seconds() / 3600.0
        if hours <= 0:  # pragma: no cover - TimeWindow forbids it
            return 0.0
        statement = select(func.count()).where(
            Complaint.cell_id == cell_id,
            Complaint.category.in_(_NETWORK_CATEGORY_VALUES),
            Complaint.received_at >= window.start,
            Complaint.received_at < window.end + COMPLAINT_MAX_LAG,
            _event_time >= window.start,
            _event_time < window.end,
        )
        total = self._scalar(statement) or 0
        return round(float(total) / hours, 4)


_NETWORK_CATEGORY_VALUES: tuple[str, ...] = tuple(
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


def _observation(row: Row[Any]) -> ComplaintObservation:
    mapping = row._mapping
    return ComplaintObservation(
        complaint_id=str(mapping["id"]),
        cell_id=str(mapping["cell_id"]),
        site_id=str(mapping["site_id"] or ""),
        region=str(mapping["region"] or ""),
        category=ComplaintCategory(mapping["category"] or ComplaintCategory.OTHER.value),
        received_at=mapping["received_at"],
        source_ts=mapping["source_ts"],
        dedupe_key=str(mapping["dedupe_key"] or ""),
        subscriber_id=mapping["subscriber_id"],
        channel=str(mapping["channel"] or ""),
    )


# =================================================================== cell reads


class SqlCellDirectory(_SqlAdapter):
    """Cell reference data. Memoised: it does not change inside a pipeline run."""

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        super().__init__(session, engine=engine)
        self._by_id: dict[str, CellInfo | None] = {}
        self._by_region: dict[str, tuple[CellInfo, ...]] = {}

    def get(self, cell_id: str) -> CellInfo | None:
        if cell_id not in self._by_id:
            rows = self._rows(self._select().where(Cell.id == cell_id))
            self._by_id[cell_id] = _cell_info(rows[0]) if rows else None
        return self._by_id[cell_id]

    def by_region(self, region: str) -> tuple[CellInfo, ...]:
        cached = self._by_region.get(region)
        if cached is None:
            cached = tuple(
                _cell_info(row) for row in self._rows(self._select().where(Cell.region == region))
            )
            self._by_region[region] = cached
            for cell in cached:
                self._by_id.setdefault(cell.cell_id, cell)
        return cached

    def with_geo(self, cell_ids: Sequence[str]) -> tuple[CellInfo, ...]:
        wanted = list(dict.fromkeys(cell_ids))
        if not wanted:
            return ()
        missing = [cell_id for cell_id in wanted if cell_id not in self._by_id]
        if missing:
            for row in self._rows(self._select().where(Cell.id.in_(missing))):
                info = _cell_info(row)
                self._by_id[info.cell_id] = info
            for cell_id in missing:
                self._by_id.setdefault(cell_id, None)
        found: list[CellInfo] = []
        for cell_id in wanted:
            cached = self._by_id.get(cell_id)
            if cached is not None:
                found.append(cached)
        return tuple(found)

    def _select(self) -> Select[Any]:
        return select(
            Cell.id,
            Cell.site_id,
            Cell.region,
            Cell.lat,
            Cell.lon,
            Cell.subscribers_est,
            Cell.tech,
        ).order_by(Cell.id)


def _cell_info(row: Row[Any]) -> CellInfo:
    return CellInfo(
        cell_id=str(row[0]),
        site_id=str(row[1]),
        region=str(row[2]),
        lat=float(row[3]),
        lon=float(row[4]),
        subscribers_est=int(row[5] or 0),
        tech=str(row[6] or ""),
    )


class SqlCellStats(_SqlAdapter):
    """The impact module's cell port: dimensioning figures for the subscriber estimate."""

    def for_cells(self, cell_ids: Sequence[str]) -> tuple[CellFacts, ...]:
        if not cell_ids:
            return ()
        statement = (
            select(Cell.id, Cell.site_id, Cell.region, Cell.subscribers_est, Cell.tech)
            .where(Cell.id.in_(list(cell_ids)))
            .order_by(Cell.id)
        )
        return tuple(
            CellFacts(
                cell_id=str(row[0]),
                site_id=str(row[1]),
                region=str(row[2]),
                subscribers_est=int(row[3] or 0),
                tech=str(row[4] or ""),
            )
            for row in self._rows(statement)
        )


class SqlSubscriberStats(_SqlAdapter):
    """Enterprise exposure, counted in SQL: 30k subscriber rows stay in the database."""

    def enterprise_exposure(self, cell_ids: Sequence[str]) -> EnterpriseExposure:
        if not cell_ids:
            return EnterpriseExposure()
        ids = list(cell_ids)
        homed = or_(Subscriber.home_cell_id.in_(ids), Subscriber.last_cell_id.in_(ids))
        totals = self._rows(
            select(
                func.count().label("subscribers"),
                func.count(func.distinct(Subscriber.enterprise_ref)).label("accounts"),
            ).where(Subscriber.is_enterprise.is_(True), homed),
        )
        mapping = totals[0]._mapping if totals else {"subscribers": 0, "accounts": 0}
        tiers = tuple(
            str(row[0])
            for row in self._rows(
                select(Subscriber.sla_tier)
                .where(
                    Subscriber.is_enterprise.is_(True),
                    Subscriber.sla_tier.is_not(None),
                    homed,
                )
                .group_by(Subscriber.sla_tier)
                .order_by(Subscriber.sla_tier),
            )
        )
        refs = tuple(
            str(row[0])
            for row in self._rows(
                select(Subscriber.enterprise_ref)
                .where(
                    Subscriber.is_enterprise.is_(True),
                    Subscriber.enterprise_ref.is_not(None),
                    homed,
                )
                .group_by(Subscriber.enterprise_ref)
                .order_by(Subscriber.enterprise_ref)
                .limit(50),
            )
        )
        return EnterpriseExposure(
            accounts=int(mapping["accounts"] or 0),
            subscribers=int(mapping["subscribers"] or 0),
            tiers=tiers,
            refs=refs,
        )


# =============================================================== topology reads


class SqlTopologyReader(_SqlAdapter):
    """Walks `topology_edge`. Chains and descendant sets are memoised per instance."""

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        super().__init__(session, engine=engine)
        self._parents: dict[str, tuple[str, str] | None] = {}
        self._chains: dict[str, tuple[TopologyNodeInfo, ...]] = {}
        self._descendants: dict[tuple[str, str | None], tuple[str, ...]] = {}
        self._names: dict[str, tuple[str, str | None]] = {}

    def upstream_of(self, node_id: str) -> tuple[TopologyNodeInfo, ...]:
        cached = self._chains.get(node_id)
        if cached is not None:
            return cached
        chain: list[TopologyNodeInfo] = []
        current = node_id
        for _ in range(MAX_TOPOLOGY_DEPTH):
            parent = self._parent_of(current)
            if parent is None:
                break
            parent_id, parent_kind = parent
            name, region = self._describe(parent_id)
            chain.append(
                TopologyNodeInfo(
                    node_id=parent_id,
                    kind=NodeKind(parent_kind),
                    name=name,
                    region=region,
                ),
            )
            current = parent_id
        result = tuple(chain)
        self._chains[node_id] = result
        return result

    def descendants_of(self, node_id: str, kind: NodeKind | None = None) -> tuple[str, ...]:
        key = (node_id, kind.value if kind else None)
        cached = self._descendants.get(key)
        if cached is not None:
            return cached
        found: list[str] = []
        frontier = [node_id]
        seen = {node_id}
        while frontier and len(found) < MAX_DESCENDANTS:
            statement = select(TopologyEdge.child_id, TopologyEdge.child_kind).where(
                TopologyEdge.parent_id.in_(frontier),
            )
            next_frontier: list[str] = []
            for child_id, child_kind in self._rows(statement):
                child = str(child_id)
                if child in seen:
                    continue
                seen.add(child)
                next_frontier.append(child)
                if kind is None or str(child_kind) == kind.value:
                    found.append(child)
            frontier = next_frontier
        result = tuple(sorted(found))
        self._descendants[key] = result
        return result

    def shared_upstream(self, cell_ids: Sequence[str]) -> TopologyNodeInfo | None:
        """The nearest element every cell hangs off, or None when they do not share one."""
        chains = [self.upstream_of(cell_id) for cell_id in cell_ids]
        if not chains or any(not chain for chain in chains):
            return None
        common = {node.node_id for node in chains[0]}
        for chain in chains[1:]:
            common &= {node.node_id for node in chain}
        if not common:
            return None
        # Nearest-first ordering means the first match in the first chain is the deepest
        # shared element, which is the most specific explanation available.
        for node in chains[0]:
            if node.node_id in common:
                return node
        return None  # pragma: no cover - unreachable given `common` is non-empty

    def _parent_of(self, node_id: str) -> tuple[str, str] | None:
        if node_id not in self._parents:
            rows = self._rows(
                select(TopologyEdge.parent_id, TopologyEdge.parent_kind)
                .where(TopologyEdge.child_id == node_id)
                .order_by(TopologyEdge.parent_id)
                .limit(1),
            )
            self._parents[node_id] = (str(rows[0][0]), str(rows[0][1])) if rows else None
        return self._parents[node_id]

    def _describe(self, node_id: str) -> tuple[str, str | None]:
        if node_id not in self._names:
            rows = self._rows(
                select(TopologyNode.name, TopologyNode.region).where(
                    TopologyNode.id == node_id,
                ),
            )
            self._names[node_id] = (str(rows[0][0]), rows[0][1]) if rows else (node_id, None)
        return self._names[node_id]


__all__ = [
    "KPI_COLUMNS",
    "SqlAlarmReader",
    "SqlCellDirectory",
    "SqlCellStats",
    "SqlComplaintReader",
    "SqlKpiFacts",
    "SqlKpiReader",
    "SqlMaintenanceReader",
    "SqlSubscriberStats",
    "SqlTopologyReader",
]
