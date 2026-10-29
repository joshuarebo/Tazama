"""SQLAlchemy adapters for the ML ports: feature reads, registry, eval runs, predictions.

Three rules, the same ones `kpi_repo.py` follows:

* **Aggregate in SQL.** The feature builder wants one row per (cell, hour). Complaints and
  care calls are counted in the database with a truncated-hour group-by, not pulled into
  Python 20,000 rows at a time, and `kpi_cell` is already hourly so it is read as-is with
  named columns only.
* **Bound parameters, named columns, time-bounded.** Every window bound and cell id is a
  bound parameter; every statement filters `granularity` and the time range first, which
  is what keeps it partition-friendly on Oracle.
* **A session when there is one, an engine otherwise**, so a training run can share a
  transaction with the rows it writes.

Hour bucketing is done in Python for the two event tables rather than with a dialect date
function: `date_trunc` (Postgres), `trunc` (Oracle) and `strftime` (SQLite) are three
different spellings, and a repository that branches on dialect is how backends drift
apart. The counts are small (20k complaints, 6k care calls over 30 days), so the cost of
bucketing a timestamp per row in Python is irrelevant next to the cost of the query.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.ai.ml.features import KPI_COLUMNS, CellMeta, CountObservation, KpiObservation
from tazama.ai.ml.registry import EvalRunRecord, ModelEntry, PredictionRecord
from tazama.core.clock import Clock, SystemClock, ensure_utc, floor_to
from tazama.core.ids import new_id
from tazama.core.logging import get_logger
from tazama.domain.value_objects import TimeWindow
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import (
    CareCall,
    Cell,
    Complaint,
    EvalRun,
    KpiCell,
    ModelRegistry,
    Prediction,
)

_log = get_logger(__name__)

#: `kpi_cell` column objects for the ten KPI families, in the feature builder's order.
#: Identifiers come from our own metadata, never from input.
KPI_COLUMN_MAP: dict[str, Any] = {
    "availability_pct": KpiCell.availability_pct,
    "rrc_setup_success_pct": KpiCell.rrc_setup_success_pct,
    "erab_setup_success_pct": KpiCell.erab_setup_success_pct,
    "drop_rate_pct": KpiCell.drop_rate_pct,
    "prb_utilisation_pct": KpiCell.prb_utilisation_pct,
    "dl_throughput_mbps": KpiCell.dl_throughput_mbps,
    "ul_throughput_mbps": KpiCell.ul_throughput_mbps,
    "latency_ms": KpiCell.latency_ms,
    "active_users": KpiCell.active_users,
    "sinr_db": KpiCell.sinr_db,
}

#: Oracle caps an IN list at 1,000 expressions, so every id filter is chunked.
IN_CHUNK = 500

#: Predictions are written in batches; a training run stores one row per test cell-hour.
INSERT_CHUNK = 1_000


def _chunks(values: Sequence[str], size: int = IN_CHUNK) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class _SqlAdapter:
    """Shared plumbing: run a statement on the caller's session, or on a connection."""

    __slots__ = ("_engine", "_session")

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        self._session = session
        self._engine = engine

    def _rows(self, statement: Select[Any]) -> Sequence[Any]:
        if self._session is not None:
            return self._session.execute(statement).all()
        with (self._engine or get_engine()).connect() as connection:
            return connection.execute(statement).all()


class _SqlWriter(_SqlAdapter):
    """Adds a write path: the caller's session, or a short-lived committed one."""

    __slots__ = ()

    def _write(self, work: Any) -> Any:
        if self._session is not None:
            result = work(self._session)
            self._session.flush()
            return result
        session = session_factory(self._engine or get_engine())()
        try:
            result = work(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        else:
            return result
        finally:
            session.close()


# =============================================================== feature reads


class SqlFeatureSource(_SqlAdapter):
    """Satisfies `ai.ml.features.FeatureSource` over the canonical schema."""

    __slots__ = ()

    def cells(self, *, region: str | None = None, limit: int | None = None) -> tuple[CellMeta, ...]:
        statement = select(
            Cell.id,
            Cell.site_id,
            Cell.region,
            Cell.tech,
            Cell.subscribers_est,
        ).order_by(Cell.id)
        if region:
            statement = statement.where(Cell.region == region)
        if limit:
            statement = statement.limit(limit)
        return tuple(
            CellMeta(
                cell_id=row[0],
                site_id=row[1],
                region=row[2],
                tech=row[3],
                subscribers_est=int(row[4] or 0),
            )
            for row in self._rows(statement)
        )

    def kpi_observations(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
        granularity: str = "1h",
    ) -> tuple[KpiObservation, ...]:
        names = list(KPI_COLUMNS)
        observations: list[KpiObservation] = []
        for chunk in _chunks(list(cell_ids)):
            statement = (
                select(KpiCell.cell_id, KpiCell.bucket_start, *[KPI_COLUMN_MAP[n] for n in names])
                .where(
                    KpiCell.granularity == granularity,
                    KpiCell.bucket_start >= window.start,
                    KpiCell.bucket_start < window.end,
                    KpiCell.cell_id.in_(list(chunk)),
                )
                .order_by(KpiCell.cell_id, KpiCell.bucket_start)
            )
            observations.extend(
                KpiObservation(
                    cell_id=row[0],
                    bucket_start=ensure_utc(row[1]),
                    values=dict(zip(names, row[2:], strict=True)),
                )
                for row in self._rows(statement)
            )
        return tuple(observations)

    def complaint_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
    ) -> tuple[CountObservation, ...]:
        return self._hourly_counts(
            window,
            cell_ids=cell_ids,
            id_column=Complaint.cell_id,
            time_column=Complaint.received_at,
        )

    def care_call_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
    ) -> tuple[CountObservation, ...]:
        return self._hourly_counts(
            window,
            cell_ids=cell_ids,
            id_column=CareCall.cell_id,
            time_column=CareCall.started_at,
        )

    def _hourly_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
        id_column: Any,
        time_column: Any,
    ) -> tuple[CountObservation, ...]:
        """Count events per (cell, hour), bucketing the timestamp in Python.

        See the module docstring: one portable statement beats three dialect spellings of
        `date_trunc`, and the event tables are small enough that it costs nothing.
        """
        tally: dict[tuple[str, datetime], int] = {}
        for chunk in _chunks(list(cell_ids)):
            statement = select(id_column, time_column).where(
                time_column >= window.start,
                time_column < window.end,
                id_column.in_(list(chunk)),
            )
            for cell_id, moment in self._rows(statement):
                if cell_id is None or moment is None:
                    continue
                key = (cell_id, floor_to(ensure_utc(moment), 60))
                tally[key] = tally.get(key, 0) + 1
        return tuple(
            CountObservation(cell_id=cell_id, bucket_start=bucket, count=count)
            for (cell_id, bucket), count in sorted(tally.items())
        )


# ==================================================================== registry


def _entry(row: Any) -> ModelEntry:
    return ModelEntry(
        name=row.name,
        version=row.version,
        algorithm=row.algorithm,
        trained_at=ensure_utc(row.trained_at),
        metrics=dict(row.metrics or {}),
        params=dict(row.params or {}),
        feature_list=tuple(row.feature_list or ()),
        data_hash=row.data_hash,
        artifact_path=row.artifact_path,
        card_path=row.card_path,
        beats_baseline=row.beats_baseline,
        is_active=bool(row.is_active),
    )


class SqlModelRegistryStore(_SqlWriter):
    """Satisfies `ai.ml.registry.ModelRegistryStore` over `model_registry`."""

    __slots__ = ()

    def upsert(self, entry: ModelEntry) -> ModelEntry:
        values = {
            "id": f"{entry.name}:{entry.version}",
            "name": entry.name,
            "version": entry.version,
            "algorithm": entry.algorithm,
            "trained_at": ensure_utc(entry.trained_at),
            "metrics": dict(entry.metrics),
            "params": dict(entry.params),
            "feature_list": list(entry.feature_list),
            "data_hash": entry.data_hash,
            "artifact_path": entry.artifact_path,
            "card_path": entry.card_path,
            "beats_baseline": entry.beats_baseline,
            "is_active": entry.is_active,
        }

        def work(session: Session) -> None:
            # Select-then-write rather than a dialect upsert, so the same statement runs on
            # SQLite (tests) and Oracle (deployment).
            existing = session.get(ModelRegistry, values["id"])
            if existing is None:
                session.execute(insert(ModelRegistry).values(**values))
            else:
                session.execute(
                    update(ModelRegistry)
                    .where(ModelRegistry.id == values["id"])
                    .values(**{k: v for k, v in values.items() if k != "id"}),
                )

        self._write(work)
        stored = self.get(entry.name, entry.version)
        if stored is None:  # pragma: no cover - only reachable if the write vanished
            raise RuntimeError(f"registry row {entry.name}:{entry.version} did not persist")
        return stored

    def get(self, name: str, version: str) -> ModelEntry | None:
        statement = select(ModelRegistry).where(
            ModelRegistry.name == name,
            ModelRegistry.version == version,
        )
        rows = self._rows(statement)
        return _entry(rows[0][0]) if rows else None

    def active(self, name: str) -> ModelEntry | None:
        statement = select(ModelRegistry).where(
            ModelRegistry.name == name,
            ModelRegistry.is_active.is_(True),
        )
        rows = self._rows(statement)
        if not rows:
            return None
        if len(rows) > 1:  # pragma: no cover - the invariant below prevents this
            raise RuntimeError(
                f"{len(rows)} active versions of {name!r}: the one-active invariant is broken",
            )
        return _entry(rows[0][0])

    def set_active(self, name: str, version: str) -> ModelEntry:
        """Activate one version and deactivate the rest, in a single transaction.

        Deactivate-then-activate inside one transaction is what makes "only one active
        version per name" an invariant rather than a race.
        """

        def work(session: Session) -> None:
            session.execute(
                update(ModelRegistry)
                .where(ModelRegistry.name == name, ModelRegistry.version != version)
                .values(is_active=False),
            )
            updated: Any = session.execute(
                update(ModelRegistry)
                .where(ModelRegistry.name == name, ModelRegistry.version == version)
                .values(is_active=True),
            )
            if updated.rowcount == 0:
                raise LookupError(f"no registry row for {name}:{version}")

        self._write(work)
        activated = self.get(name, version)
        if activated is None:  # pragma: no cover
            raise RuntimeError(f"registry row {name}:{version} vanished during activation")
        return activated

    def list_all(self, *, name: str | None = None) -> tuple[ModelEntry, ...]:
        statement = select(ModelRegistry).order_by(
            ModelRegistry.name,
            ModelRegistry.trained_at.desc(),
            ModelRegistry.version,
        )
        if name:
            statement = statement.where(ModelRegistry.name == name)
        return tuple(_entry(row[0]) for row in self._rows(statement))

    def clear(self, *, name: str) -> int:
        """Remove every version of one model. Test and re-seed support only."""

        def work(session: Session) -> int:
            result: Any = session.execute(
                delete(ModelRegistry).where(ModelRegistry.name == name),
            )
            return int(result.rowcount or 0)

        return int(self._write(work))


# ================================================================== eval runs


class SqlEvalRunSink(_SqlWriter):
    """Satisfies `ai.ml.registry.EvalRunSink` over `eval_run` (append-only)."""

    __slots__ = ("_clock",)

    def __init__(
        self,
        session: Session | None = None,
        *,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(session, engine=engine)
        self._clock = clock or SystemClock()

    def record(self, run: EvalRunRecord) -> str:
        run_id = new_id("EVL", self._clock)
        values = {
            "id": run_id,
            "suite": run.suite,
            "llm_mode": run.llm_mode,
            "started_at": ensure_utc(run.started_at),
            "finished_at": None if run.finished_at is None else ensure_utc(run.finished_at),
            "metrics": dict(run.metrics),
            "thresholds": dict(run.thresholds),
            "passed": run.passed,
            "prompt_versions": None,
            "notes": run.notes,
        }

        def work(session: Session) -> None:
            session.execute(insert(EvalRun).values(**values))

        self._write(work)
        _log.info("eval run recorded", extra={"eval_run": run_id, "suite": run.suite})
        return run_id

    def recent(self, *, suite: str | None = None, limit: int = 20) -> tuple[EvalRunRecord, ...]:
        statement = select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit)
        if suite:
            statement = statement.where(EvalRun.suite == suite)
        return tuple(
            EvalRunRecord(
                suite=row[0].suite,
                started_at=ensure_utc(row[0].started_at),
                finished_at=None if row[0].finished_at is None else ensure_utc(row[0].finished_at),
                metrics=dict(row[0].metrics or {}),
                thresholds=dict(row[0].thresholds or {}),
                passed=row[0].passed,
                llm_mode=row[0].llm_mode,
                notes=row[0].notes,
            )
            for row in self._rows(statement)
        )


# ================================================================= predictions


class SqlPredictionSink(_SqlWriter):
    """Satisfies `ai.ml.registry.PredictionSink` over `prediction`."""

    __slots__ = ()

    def write(self, predictions: Sequence[PredictionRecord]) -> int:
        if not predictions:
            return 0
        rows = [
            {
                "model_name": p.model_name,
                "model_version": p.model_version,
                "entity_type": p.entity_type,
                "entity_id": p.entity_id,
                "target": p.target,
                "horizon_minutes": p.horizon_minutes,
                "predicted_for": None if p.predicted_for is None else ensure_utc(p.predicted_for),
                "value": float(p.value),
                "lower": None if p.lower is None else float(p.lower),
                "upper": None if p.upper is None else float(p.upper),
                "features": None if p.features is None else dict(p.features),
                "created_at": ensure_utc(p.created_at),
            }
            for p in predictions
        ]

        def work(session: Session) -> None:
            for start in range(0, len(rows), INSERT_CHUNK):
                session.execute(insert(Prediction), rows[start : start + INSERT_CHUNK])

        self._write(work)
        return len(rows)

    def delete_for(self, *, model_name: str, model_version: str) -> int:
        """Remove a model version's stored predictions (re-run support)."""

        def work(session: Session) -> int:
            result: Any = session.execute(
                delete(Prediction).where(
                    Prediction.model_name == model_name,
                    Prediction.model_version == model_version,
                ),
            )
            return int(result.rowcount or 0)

        return int(self._write(work))


def latest_kpi_hour(session: Session | None = None, *, engine: Engine | None = None) -> datetime:
    """The most recent hourly KPI bucket, exclusive-end aligned.

    Training reads the window backwards from the data, not from the wall clock: the seed is
    a fixed historical month, and `datetime.now()` would silently train on nothing.
    """
    statement = (
        select(KpiCell.bucket_start)
        .where(KpiCell.granularity == "1h")
        .order_by(KpiCell.bucket_start.desc())
        .limit(1)
    )
    if session is not None:
        rows = session.execute(statement).all()
    else:
        with (engine or get_engine()).connect() as connection:
            rows = connection.execute(statement).all()
    if not rows:
        raise LookupError("no hourly kpi_cell rows: run `make seed` first")
    return ensure_utc(rows[0][0]) + timedelta(hours=1)
