"""Data Discovery: catalog, profiler, join coverage and the safe SQL explorer.

This module exists because the PRD makes data discovery first-class: you cannot correlate
complaints with network KPIs until you know what the data actually contains and how well
it joins. The service is deliberately boring — guard, run, cap, mask, audit — and every
refusal is explained to the analyst.

Order of operations for a query (never reordered):
    1. authorise the caller (capability, decided server-side)
    2. guard the SQL (parse, allowlist, rewrite with a row cap, mask PII)
    3. audit the attempt — including refusals
    4. execute with a timeout
    5. audit the outcome
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from datetime import datetime

from tazama.core.clock import Clock
from tazama.core.errors import Forbidden, NotFound, QueryRejected
from tazama.core.ids import deterministic_id, get_correlation_id
from tazama.core.logging import get_logger
from tazama.core.security import Principal
from tazama.modules.discovery.mappings import (
    DEFAULT_PATH,
    MappingReport,
    Mode,
    load_mappings,
    validate_mappings,
)
from tazama.modules.discovery.models import (
    LARGE_TABLES,
    CatalogSnapshot,
    JoinCoverage,
    ProfileRequest,
    QueryAuditRecord,
    QueryColumn,
    QueryResult,
    SavedQueryRecord,
    TableProfile,
)
from tazama.modules.discovery.ports import (
    CatalogReader,
    CoverageReader,
    QueryAuditSink,
    QueryRunner,
    SavedQueryStore,
    SeedProvenanceReader,
)
from tazama.modules.discovery.sql_guard import GuardConfig, GuardedQuery, guard

_log = get_logger(__name__)

MAX_SAVED_QUERIES_PER_USER = 100
MAX_SAVED_NAME_LEN = 120


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    """Everything the service needs to know about the deployment it is running in."""

    app_mode: str
    db_backend: str
    guard_config: GuardConfig
    mappings_path: str = str(DEFAULT_PATH)


class DiscoveryService:
    def __init__(
        self,
        *,
        catalog: CatalogReader,
        coverage: CoverageReader,
        runner: QueryRunner,
        audit: QueryAuditSink,
        saved_queries: SavedQueryStore,
        clock: Clock,
        context: DiscoveryContext,
        seed_provenance: SeedProvenanceReader | None = None,
    ) -> None:
        self._catalog = catalog
        self._coverage = coverage
        self._runner = runner
        self._audit = audit
        self._saved = saved_queries
        self._clock = clock
        self._ctx = context
        self._seed = seed_provenance

    # ------------------------------------------------------------- catalog

    def list_tables(self) -> tuple[str, ...]:
        return self._catalog.list_tables()

    def profile_table(self, table: str, *, deep: bool = True) -> TableProfile:
        if table not in self._catalog.list_tables():
            raise NotFound(f"Unknown table {table!r}.")
        return self._catalog.profile_table(table, deep=deep)

    def profile_all(self, request: ProfileRequest | None = None) -> CatalogSnapshot:
        """Profile the whole schema and return a snapshot that can render itself."""
        request = request or ProfileRequest()
        available = self._catalog.list_tables()
        selected = request.tables or available
        unknown = [t for t in selected if t not in available]
        if unknown:
            raise NotFound(f"Unknown table(s): {', '.join(sorted(unknown))}.")

        mappings = load_mappings(self._ctx.mappings_path, mode=self._mapping_mode())
        profiles: list[TableProfile] = []
        for table in selected:
            if table in request.exclude:
                continue
            profile = self._catalog.profile_table(
                table,
                deep=request.depth == "full",
                sample_rows=request.sample_rows,
            )
            mapping = mappings.get(table)
            profiles.append(
                replace(
                    profile,
                    is_large=table in LARGE_TABLES,
                    app_owned=mapping is None,
                    mapped_source=mapping.source if mapping else None,
                ),
            )

        coverage: JoinCoverage | None = None
        try:
            coverage = self._coverage.join_coverage()
        except Exception as exc:  # coverage is informative; never fail the catalog on it
            _log.warning("coverage_unavailable", extra={"error": str(exc)})

        return CatalogSnapshot(
            generated_at=self._clock.now(),
            app_mode=self._ctx.app_mode,
            db_backend=self._ctx.db_backend,
            tables=tuple(profiles),
            mapping_summary=self.validate_mappings().render(),
            coverage=coverage,
            seed_note=self._seed.describe_latest() if self._seed else "",
        )

    def join_coverage(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> JoinCoverage:
        return self._coverage.join_coverage(window_start=window_start, window_end=window_end)

    # ------------------------------------------------------------ mappings

    def validate_mappings(self) -> MappingReport:
        """Validate `config/source_mappings.yaml` against the live schema."""
        return validate_mappings(
            self._catalog.canonical_columns(),
            path=self._ctx.mappings_path,
            mode=self._mapping_mode(),
        )

    def _mapping_mode(self) -> Mode:
        return "live" if self._ctx.app_mode == "live" else "demo"

    # ------------------------------------------------------------ explorer

    def explain(self, sql: str, *, principal: Principal) -> GuardedQuery:
        """Guard a query without running it — what the UI calls as the analyst types."""
        principal.require("explorer:query")
        return guard(sql, self._ctx.guard_config)

    def run_query(
        self,
        sql: str,
        *,
        principal: Principal,
        max_rows: int | None = None,
    ) -> QueryResult:
        """Guard, audit, execute, audit. A refusal is recorded as carefully as a success."""
        principal.require("explorer:query")
        sql_hash = hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()

        try:
            guarded = guard(sql, self._ctx.guard_config)
        except QueryRejected as refusal:
            self._audit.record(
                QueryAuditRecord(
                    executed_at=self._clock.now(),
                    actor=principal.subject,
                    actor_role=principal.role,
                    sql_text=sql[:4000],
                    sql_hash=sql_hash,
                    accepted=False,
                    reject_reason=refusal.detail[:200],
                    correlation_id=get_correlation_id() or None,
                ),
            )
            _log.info(
                "explorer_query_refused",
                extra={"actor": principal.subject, "reason": refusal.code, "sql_hash": sql_hash},
            )
            raise

        effective_rows = min(max_rows or guarded.row_limit, guarded.row_limit)
        started = time.perf_counter()
        try:
            columns, rows, truncated = self._runner.run(
                guarded.sql,
                timeout_s=self._ctx.guard_config.timeout_s,
                max_rows=effective_rows,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._audit.record(
                QueryAuditRecord(
                    executed_at=self._clock.now(),
                    actor=principal.subject,
                    actor_role=principal.role,
                    sql_text=guarded.sql[:4000],
                    sql_hash=sql_hash,
                    accepted=True,
                    reject_reason=f"execution failed: {type(exc).__name__}",
                    duration_ms=duration_ms,
                    correlation_id=get_correlation_id() or None,
                ),
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self._audit.record(
            QueryAuditRecord(
                executed_at=self._clock.now(),
                actor=principal.subject,
                actor_role=principal.role,
                sql_text=guarded.sql[:4000],
                sql_hash=sql_hash,
                accepted=True,
                row_count=len(rows),
                truncated=truncated,
                duration_ms=duration_ms,
                correlation_id=get_correlation_id() or None,
            ),
        )

        warnings = list(guarded.warnings)
        if truncated:
            warnings.append(
                f"Result truncated at {effective_rows} rows — narrow the query or aggregate.",
            )
        if guarded.masked_columns:
            warnings.append(
                "Masked column(s): " + ", ".join(guarded.masked_columns),
            )

        masked = set(guarded.masked_columns)
        return QueryResult(
            columns=tuple(QueryColumn(name=c, masked=c in masked) for c in columns),
            rows=tuple(tuple(r) for r in rows),
            row_count=len(rows),
            truncated=truncated,
            duration_ms=duration_ms,
            executed_sql=guarded.sql,
            warnings=tuple(warnings),
        )

    def export_csv(self, sql: str, *, principal: Principal) -> tuple[str, str]:
        """Masked CSV export. Returns `(filename, csv_text)`.

        The filename is generated, never taken from input, so there is nothing to
        traverse with.
        """
        principal.require("explorer:export")
        result = self.run_query(sql, principal=principal)
        stamp = self._clock.now().strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256(result.executed_sql.encode("utf-8")).hexdigest()[:8]
        return f"tazama-export-{stamp}-{digest}.csv", result.to_csv()

    def query_history(
        self,
        *,
        principal: Principal,
        limit: int = 50,
    ) -> tuple[QueryAuditRecord, ...]:
        """An analyst sees their own history; audit readers see everyone's."""
        principal.require("explorer:query")
        if principal.can("audit:read"):
            return self._audit.recent(limit=limit)
        return self._audit.recent(actor=principal.subject, limit=limit)

    # -------------------------------------------------------- saved queries

    def save_query(
        self,
        *,
        principal: Principal,
        name: str,
        sql: str,
        description: str | None = None,
        shared: bool = False,
    ) -> SavedQueryRecord:
        principal.require("explorer:query")
        cleaned = name.strip()
        if not cleaned or len(cleaned) > MAX_SAVED_NAME_LEN:
            raise QueryRejected(f"A saved query needs a name of 1-{MAX_SAVED_NAME_LEN} characters.")
        # Refuse to store something that would be refused on execution.
        guard(sql, self._ctx.guard_config)
        if len(self._saved.list_for(principal.subject)) >= MAX_SAVED_QUERIES_PER_USER:
            raise QueryRejected(
                f"You already have {MAX_SAVED_QUERIES_PER_USER} saved queries; delete one first.",
            )
        record = SavedQueryRecord(
            id=deterministic_id("SQY", principal.subject, cleaned),
            name=cleaned,
            sql_text=sql,
            created_by=principal.subject,
            created_at=self._clock.now(),
            description=description,
            is_shared=shared,
        )
        return self._saved.save(record)

    def list_saved(self, *, principal: Principal) -> tuple[SavedQueryRecord, ...]:
        principal.require("explorer:query")
        return self._saved.list_for(principal.subject)

    def get_saved(self, query_id: str, *, principal: Principal) -> SavedQueryRecord:
        principal.require("explorer:query")
        record = self._saved.get(query_id, actor=principal.subject)
        if record is None:
            # Object-level authorisation: another analyst's private query is "not found",
            # which is also what prevents enumeration by id (IDOR).
            raise NotFound("Saved query not found.")
        if record.created_by != principal.subject and not record.is_shared:
            raise Forbidden("That saved query belongs to someone else.")
        return record

    def delete_saved(self, query_id: str, *, principal: Principal) -> None:
        principal.require("explorer:query")
        if not self._saved.delete(query_id, actor=principal.subject):
            raise NotFound("Saved query not found.")
