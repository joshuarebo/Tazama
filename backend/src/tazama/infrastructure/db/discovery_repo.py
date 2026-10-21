"""SQLAlchemy adapters for the discovery ports.

All SQL here is built from our own metadata (table and column objects), never from user
input: identifiers come from `Base.metadata`, values are always bound. The explorer's
user-supplied statement arrives already parsed, validated and rewritten by the SQL guard,
and is executed verbatim.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, delete, func, insert, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from tazama.core.logging import get_logger
from tazama.core.security import mask_value
from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.engine import apply_statement_timeout, get_engine
from tazama.infrastructure.db.models import PII_COLUMNS, QueryAudit, SavedQuery, SeedRun
from tazama.modules.discovery.models import (
    ColumnProfile,
    CoverageRow,
    JoinCoverage,
    QueryAuditRecord,
    SavedQueryRecord,
    TableProfile,
)

_log = get_logger(__name__)

#: Column used to judge "how fresh is this table?", per table.
FRESHNESS_COLUMNS: dict[str, str] = {
    "alarm": "raised_at",
    "kpi_cell": "bucket_start",
    "complaint": "received_at",
    "care_call": "started_at",
    "incident": "detected_at",
    "diagnosis": "created_at",
    "notification": "proposed_at",
    "audit_log": "occurred_at",
    "outbox_event": "occurred_at",
    "query_audit": "executed_at",
    "prediction": "created_at",
    "eval_run": "started_at",
    "maintenance_window": "starts_at",
    "ground_truth": "created_at",
    "seed_run": "created_at",
    "report_delivery": "created_at",
}

#: Beyond this row count the profiler skips exact DISTINCT counts: on a multi-million-row
#: table that single query dominates the whole catalog run.
DISTINCT_LIMIT = 400_000

#: Complaint-to-cell coverage below this is reported as the limiting factor.
COVERAGE_WARN_PCT = 80.0

MAX_SAMPLE_LEN = 40


class SqlCatalogReader:
    """Schema metadata and profiling via the SQLAlchemy inspector plus aggregates."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()

    def list_tables(self) -> tuple[str, ...]:
        inspector = inspect(self._engine)
        present = set(inspector.get_table_names())
        return tuple(name for name in sorted(Base.metadata.tables) if name in present)

    def canonical_columns(self) -> dict[str, frozenset[str]]:
        return {
            name: frozenset(column.name for column in table.columns)
            for name, table in Base.metadata.tables.items()
        }

    def profile_table(
        self,
        table_name: str,
        *,
        deep: bool = True,
        sample_rows: int = 3,
    ) -> TableProfile:
        table = Base.metadata.tables[table_name]
        inspector = inspect(self._engine)
        indexed = {
            column
            for index in inspector.get_indexes(table_name)
            for column in index.get("column_names", [])
            if column
        }
        for constraint in inspector.get_unique_constraints(table_name) or []:
            indexed.update(constraint.get("column_names", []))

        with self._engine.connect() as connection:
            row_count = int(
                connection.execute(select(func.count()).select_from(table)).scalar_one(),
            )
            freshness_column = FRESHNESS_COLUMNS.get(table_name)
            freshness: datetime | None = None
            if freshness_column and freshness_column in table.c and row_count:
                freshness = connection.execute(
                    select(func.max(table.c[freshness_column])),
                ).scalar_one_or_none()
                if freshness is not None and freshness.tzinfo is None:
                    freshness = freshness.replace(tzinfo=UTC)

            columns: list[ColumnProfile] = []
            for column in table.columns:
                is_pii = column.name in PII_COLUMNS
                profile = ColumnProfile(
                    name=column.name,
                    type=str(column.type),
                    nullable=bool(column.nullable),
                    primary_key=bool(column.primary_key),
                    indexed=column.name in indexed or bool(column.primary_key),
                    is_pii=is_pii,
                )
                if deep and row_count:
                    profile = self._deepen(
                        connection,
                        table,
                        column.name,
                        profile,
                        row_count=row_count,
                        sample_rows=sample_rows,
                        is_pii=is_pii,
                    )
                columns.append(profile)

        return TableProfile(
            name=table_name,
            row_count=row_count,
            columns=tuple(columns),
            freshness=freshness,
            freshness_column=freshness_column,
        )

    def _deepen(
        self,
        connection: Any,
        table: Table,
        column_name: str,
        profile: ColumnProfile,
        *,
        row_count: int,
        sample_rows: int,
        is_pii: bool,
    ) -> ColumnProfile:
        column = table.c[column_name]
        try:
            null_count = int(
                connection.execute(
                    select(func.count()).select_from(table).where(column.is_(None)),
                ).scalar_one(),
            )
            distinct: int | None = None
            if row_count <= DISTINCT_LIMIT:
                distinct = int(
                    connection.execute(
                        select(func.count(func.distinct(column))),
                    ).scalar_one(),
                )

            minimum = maximum = None
            samples: tuple[str, ...] = ()
            # min/max on a CLOB is both meaningless and expensive; skip free text.
            if not _is_textual(column.type):
                bounds = connection.execute(
                    select(func.min(column), func.max(column)),
                ).one()
                minimum, maximum = (_fmt(bounds[0]), _fmt(bounds[1]))
            if sample_rows and not is_pii:
                values = connection.execute(
                    select(column).where(column.is_not(None)).limit(sample_rows),
                ).scalars()
                samples = tuple(_fmt(v) or "" for v in values)
            elif sample_rows and is_pii:
                values = connection.execute(
                    select(column).where(column.is_not(None)).limit(sample_rows),
                ).scalars()
                # Even a catalog preview masks PII: the catalog is shared widely.
                samples = tuple(mask_value(_fmt(v) or "", keep=2) for v in values)
        except SQLAlchemyError as exc:  # a profile is informative, never load-bearing
            _log.warning(
                "profile_column_failed",
                extra={"table": table.name, "column": column_name, "error": str(exc)},
            )
            return profile

        return ColumnProfile(
            name=profile.name,
            type=profile.type,
            nullable=profile.nullable,
            primary_key=profile.primary_key,
            indexed=profile.indexed,
            is_pii=profile.is_pii,
            null_count=null_count,
            distinct_count=distinct,
            min_value=minimum,
            max_value=maximum,
            sample=samples,
        )


class SqlCoverageReader:
    """Join coverage: the day-one measure of the complaint -> network-element problem."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()

    def join_coverage(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> JoinCoverage:
        tables = Base.metadata.tables
        complaint = tables["complaint"]
        care_call = tables["care_call"]
        kpi = tables["kpi_cell"]
        cell = tables["cell"]
        ground_truth = tables["ground_truth"]

        def _bounded(stmt: Any, column: Any) -> Any:
            if window_start is not None:
                stmt = stmt.where(column >= window_start)
            if window_end is not None:
                stmt = stmt.where(column < window_end)
            return stmt

        rows: list[CoverageRow] = []
        alignment: list[CoverageRow] = []
        warnings: list[str] = []

        with self._engine.connect() as connection:

            def count(stmt: Any) -> int:
                return int(connection.execute(stmt).scalar_one())

            total = count(
                _bounded(select(func.count()).select_from(complaint), complaint.c.received_at)
            )

            if not total:
                warnings.append("No complaints in the window: coverage is undefined.")
                return JoinCoverage(
                    measured_at=datetime.now(tz=UTC),
                    window_start=window_start,
                    window_end=window_end,
                    rows=(),
                    warnings=tuple(warnings),
                )

            rows.append(
                CoverageRow(
                    question="complaint -> cell",
                    resolved=count(
                        _bounded(
                            select(func.count())
                            .select_from(complaint)
                            .where(complaint.c.cell_id.is_not(None)),
                            complaint.c.received_at,
                        ),
                    ),
                    total=total,
                    detail="complaints carrying a resolved serving cell",
                ),
            )
            rows.append(
                CoverageRow(
                    question="complaint -> known cell",
                    resolved=count(
                        _bounded(
                            select(func.count()).select_from(
                                complaint.join(cell, complaint.c.cell_id == cell.c.id),
                            ),
                            complaint.c.received_at,
                        ),
                    ),
                    total=total,
                    detail="cell id also exists in the cell inventory (no dangling ids)",
                ),
            )
            rows.append(
                CoverageRow(
                    question="complaint -> site",
                    resolved=count(
                        _bounded(
                            select(func.count())
                            .select_from(complaint)
                            .where(complaint.c.site_id.is_not(None)),
                            complaint.c.received_at,
                        ),
                    ),
                    total=total,
                ),
            )
            rows.append(
                CoverageRow(
                    question="complaint -> incident",
                    resolved=count(
                        _bounded(
                            select(func.count())
                            .select_from(complaint)
                            .where(complaint.c.incident_id.is_not(None)),
                            complaint.c.received_at,
                        ),
                    ),
                    total=total,
                    detail="correlated to an incident by the pipeline",
                ),
            )
            rows.append(
                CoverageRow(
                    question="complaint -> subscriber",
                    resolved=count(
                        _bounded(
                            select(func.count())
                            .select_from(complaint)
                            .where(complaint.c.subscriber_id.is_not(None)),
                            complaint.c.received_at,
                        ),
                    ),
                    total=total,
                ),
            )
            rows.append(
                CoverageRow(
                    question="complaint -> region",
                    resolved=count(
                        _bounded(
                            select(func.count())
                            .select_from(complaint)
                            .where(complaint.c.region.is_not(None)),
                            complaint.c.received_at,
                        ),
                    ),
                    total=total,
                ),
            )

            call_total = count(
                _bounded(select(func.count()).select_from(care_call), care_call.c.started_at),
            )
            if call_total:
                rows.append(
                    CoverageRow(
                        question="care call -> cell",
                        resolved=count(
                            _bounded(
                                select(func.count())
                                .select_from(care_call)
                                .where(care_call.c.cell_id.is_not(None)),
                                care_call.c.started_at,
                            ),
                        ),
                        total=call_total,
                    ),
                )

            # How many complaints *could* be resolved, per the seeded ground truth. The
            # gap between this and "complaint -> cell" is the work the pipeline has left.
            resolvable = count(
                select(func.count(func.distinct(ground_truth.c.entity_id)))
                .select_from(ground_truth)
                .where(ground_truth.c.entity_type == "complaint")
                .where(ground_truth.c.label == "cell"),
            )
            if resolvable:
                rows.append(
                    CoverageRow(
                        question="complaint -> cell (ceiling)",
                        resolved=resolvable,
                        total=total,
                        detail="resolvable in principle, per seeded ground truth",
                    ),
                )

            # Time alignment: a complaint is only usable if KPI data covers its moment.
            kpi_bounds = connection.execute(
                select(func.min(kpi.c.bucket_start), func.max(kpi.c.bucket_start)),
            ).one()
            if kpi_bounds[0] is None:
                warnings.append("No KPI rows: time alignment cannot be measured.")
            else:
                kpi_min = _as_utc(kpi_bounds[0])
                kpi_max = _as_utc(kpi_bounds[1])
                in_range = count(
                    _bounded(
                        select(func.count())
                        .select_from(complaint)
                        .where(complaint.c.received_at >= kpi_min)
                        .where(complaint.c.received_at <= kpi_max),
                        complaint.c.received_at,
                    ),
                )
                alignment.append(
                    CoverageRow(
                        question="complaint inside KPI window",
                        resolved=in_range,
                        total=total,
                        detail=f"KPI data spans {kpi_min.isoformat()} .. {kpi_max.isoformat()}",
                    ),
                )

            lag_seconds = _seconds_between(
                self._engine.dialect.name,
                complaint.c.received_at,
                complaint.c.source_ts,
            )
            late = count(
                _bounded(
                    select(func.count())
                    .select_from(complaint)
                    .where(complaint.c.source_ts.is_not(None))
                    .where(lag_seconds > 3600),
                    complaint.c.received_at,
                ),
            )
            alignment.append(
                CoverageRow(
                    question="late arrivals (>1h)",
                    resolved=late,
                    total=total,
                    detail="source timestamp more than an hour before ingestion",
                ),
            )

            skewed = count(
                _bounded(
                    select(func.count())
                    .select_from(complaint)
                    .where(complaint.c.source_ts.is_not(None))
                    .where(lag_seconds < -300),
                    complaint.c.received_at,
                ),
            )
            alignment.append(
                CoverageRow(
                    question="clock skew (source ahead)",
                    resolved=skewed,
                    total=total,
                    detail="source timestamp later than ingestion by more than 5 minutes",
                ),
            )
            if skewed:
                warnings.append(
                    f"{skewed} complaint(s) have a source timestamp ahead of ingestion — "
                    "treat their ordering as unreliable.",
                )

        coverage = rows[0].pct if rows else 0.0
        # Below this, correlation quality is limited by the join rather than by the
        # clustering logic, which is worth saying out loud on every catalog run.
        if coverage < COVERAGE_WARN_PCT:
            warnings.append(
                f"Only {coverage:.1f}% of complaints resolve to a cell. Site identification "
                "is the bottleneck for correlation quality.",
            )

        return JoinCoverage(
            measured_at=datetime.now(tz=UTC),
            window_start=window_start,
            window_end=window_end,
            rows=tuple(rows),
            time_alignment=tuple(alignment),
            warnings=tuple(warnings),
        )


class SqlQueryRunner:
    """Executes a guarded SELECT with a timeout and a hard row cap."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()

    def run(
        self,
        sql: str,
        *,
        timeout_s: int,
        max_rows: int,
    ) -> tuple[tuple[str, ...], list[tuple[Any, ...]], bool]:
        started = time.perf_counter()
        with self._engine.connect() as connection:
            apply_statement_timeout(connection, timeout_s)
            # The statement is the guard's output, executed verbatim; no interpolation.
            result = connection.execute(text(sql))
            # `keys()` on a SQLAlchemy result is the column list, not a mapping.
            columns = tuple(str(key) for key in result.keys())  # noqa: SIM118
            rows: list[tuple[Any, ...]] = []
            truncated = False
            for row in result:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(tuple(row))
                # SQLite cannot enforce a server-side timeout, so the budget is also
                # enforced here while streaming (PRD section 15: huge result sets).
                if time.perf_counter() - started > timeout_s:
                    truncated = True
                    _log.warning("explorer_timeout", extra={"timeout_s": timeout_s})
                    break
        return columns, rows, truncated


class SqlQueryAuditSink:
    """Append-only query audit. Nothing updates or deletes these rows."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()

    def record(self, entry: QueryAuditRecord) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(QueryAudit).values(
                    executed_at=entry.executed_at,
                    actor=entry.actor,
                    actor_role=entry.actor_role,
                    sql_hash=entry.sql_hash,
                    sql_text=entry.sql_text,
                    accepted=entry.accepted,
                    reject_reason=entry.reject_reason,
                    row_count=entry.row_count,
                    truncated=entry.truncated,
                    duration_ms=entry.duration_ms,
                    correlation_id=entry.correlation_id,
                ),
            )

    def recent(
        self,
        *,
        actor: str | None = None,
        limit: int = 50,
    ) -> tuple[QueryAuditRecord, ...]:
        table = QueryAudit.__table__
        stmt = select(table).order_by(table.c.executed_at.desc()).limit(max(1, min(limit, 500)))
        if actor is not None:
            stmt = stmt.where(table.c.actor == actor)
        with self._engine.connect() as connection:
            found: Sequence[Any] = connection.execute(stmt).mappings().all()
        return tuple(
            QueryAuditRecord(
                executed_at=_as_utc(row["executed_at"]),
                actor=row["actor"],
                actor_role=row["actor_role"],
                sql_text=row["sql_text"],
                sql_hash=row["sql_hash"],
                accepted=bool(row["accepted"]),
                reject_reason=row["reject_reason"],
                row_count=row["row_count"],
                truncated=bool(row["truncated"]),
                duration_ms=row["duration_ms"],
                correlation_id=row["correlation_id"],
            )
            for row in found
        )


class SqlSavedQueryStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()

    def save(self, record: SavedQueryRecord) -> SavedQueryRecord:
        table = SavedQuery.__table__
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(table.c.id).where(table.c.id == record.id),
            ).scalar_one_or_none()
            values = {
                "name": record.name,
                "sql_text": record.sql_text,
                "description": record.description,
                "created_by": record.created_by,
                "created_at": record.created_at,
                "is_shared": record.is_shared,
            }
            if existing:
                connection.execute(
                    update(SavedQuery).where(table.c.id == record.id).values(**values),
                )
            else:
                connection.execute(insert(SavedQuery).values(id=record.id, **values))
        return record

    def list_for(self, actor: str) -> tuple[SavedQueryRecord, ...]:
        table = SavedQuery.__table__
        stmt = (
            select(table)
            .where((table.c.created_by == actor) | (table.c.is_shared.is_(True)))
            .order_by(table.c.created_at.desc())
            .limit(MAX_LISTED_QUERIES)
        )
        with self._engine.connect() as connection:
            return tuple(_saved(row) for row in connection.execute(stmt).mappings())

    def get(self, query_id: str, *, actor: str) -> SavedQueryRecord | None:
        table = SavedQuery.__table__
        # Object-level authorisation happens in SQL: another analyst's private query is
        # simply not returned, so an id cannot be probed for existence.
        stmt = (
            select(table)
            .where(table.c.id == query_id)
            .where((table.c.created_by == actor) | (table.c.is_shared.is_(True)))
        )
        with self._engine.connect() as connection:
            row = connection.execute(stmt).mappings().one_or_none()
        return _saved(row) if row else None

    def delete(self, query_id: str, *, actor: str) -> bool:
        table = SavedQuery.__table__
        with self._engine.begin() as connection:
            result = connection.execute(
                delete(SavedQuery).where(table.c.id == query_id).where(table.c.created_by == actor),
            )
        return bool(result.rowcount)


class SqlSeedProvenanceReader:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()

    def describe_latest(self) -> str:
        table = SeedRun.__table__
        stmt = select(table).order_by(table.c.created_at.desc()).limit(1)
        try:
            with self._engine.connect() as connection:
                row = connection.execute(stmt).mappings().one_or_none()
        except SQLAlchemyError:
            return ""
        if row is None:
            return "No seed run recorded — the data was not produced by `make seed`."
        return (
            f"Seeded by generator `{row['generator_version']}` with seed `{row['seed']}` "
            f"({row['days']} days of history, {row['fine_days']} of 15-minute detail), "
            f"anchored at {_as_utc(row['anchor_at']).isoformat()}. "
            f"Content hash `{row['content_hash'][:16]}…` — re-running `make seed` with the "
            "same arguments reproduces exactly this dataset."
        )


MAX_LISTED_QUERIES = 200


def _saved(row: Any) -> SavedQueryRecord:
    return SavedQueryRecord(
        id=row["id"],
        name=row["name"],
        sql_text=row["sql_text"],
        created_by=row["created_by"],
        created_at=_as_utc(row["created_at"]),
        description=row["description"],
        is_shared=bool(row["is_shared"]),
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fmt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    text_value = str(value)
    if len(text_value) > MAX_SAMPLE_LEN:
        return text_value[: MAX_SAMPLE_LEN - 1] + "…"
    return text_value


def _seconds_between(dialect: str, later: Any, earlier: Any) -> Any:
    """`later - earlier` in seconds, as a SQL expression.

    Datetime arithmetic is one of the few places the two backends genuinely differ:
    SQLite stores timestamps as text and needs `julianday`, while Oracle timestamp
    subtraction yields an INTERVAL whose fields must be extracted. Binding a Python
    `timedelta` instead would be rejected by the `UtcDateTime` codec, which is correct —
    a timedelta is not a timestamp.
    """
    if dialect == "oracle":
        difference = later - earlier
        return (
            func.extract("day", difference) * 86400
            + func.extract("hour", difference) * 3600
            + func.extract("minute", difference) * 60
            + func.extract("second", difference)
        )
    return (func.julianday(later) - func.julianday(earlier)) * 86400.0


def _is_textual(column_type: Any) -> bool:
    name = type(column_type).__name__.lower()
    return "text" in name or "clob" in name or "json" in name
