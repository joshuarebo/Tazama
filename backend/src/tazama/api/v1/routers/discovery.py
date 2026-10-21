"""Data Explorer and data catalog endpoints.

Every endpoint declares a capability; authorisation is decided from the token, server
side. The explorer endpoints are the only place in the product that accepts SQL, and the
request never reaches the database without passing the guard.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from tazama.api.deps import DiscoveryDep, PrincipalDep, requires
from tazama.modules.discovery.models import CatalogSnapshot, JoinCoverage, TableProfile

router = APIRouter(prefix="/discovery", tags=["discovery"])

MAX_SQL_CHARS = 20_000


# ------------------------------------------------------------------ schemas


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    indexed: bool
    is_pii: bool
    null_count: int | None = None
    null_pct: float | None = None
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    sample: list[str] = Field(default_factory=list)


class TableOut(BaseModel):
    name: str
    row_count: int
    freshness: str | None = None
    freshness_column: str | None = None
    is_large: bool
    app_owned: bool
    mapped_source: str | None = None
    pii_columns: list[str] = Field(default_factory=list)
    columns: list[ColumnOut] = Field(default_factory=list)


class CoverageRowOut(BaseModel):
    question: str
    resolved: int
    total: int
    pct: float
    detail: str = ""


class CoverageOut(BaseModel):
    measured_at: str
    rows: list[CoverageRowOut]
    time_alignment: list[CoverageRowOut]
    warnings: list[str]
    complaint_to_cell_pct: float


class CatalogOut(BaseModel):
    generated_at: str
    app_mode: str
    db_backend: str
    total_rows: int
    mapping_summary: str
    seed_note: str
    tables: list[TableOut]
    coverage: CoverageOut | None = None


class MappingIssueOut(BaseModel):
    entity: str
    severity: str
    message: str


class MappingReportOut(BaseModel):
    mode: str
    version: int
    ok: bool
    coverage_pct: float
    entities: list[str]
    app_owned: list[str]
    issues: list[MappingIssueOut]


class QueryIn(BaseModel):
    sql: Annotated[str, Field(min_length=1, max_length=MAX_SQL_CHARS)]
    max_rows: Annotated[int | None, Field(default=None, ge=1, le=100_000)] = None


class QueryColumnOut(BaseModel):
    name: str
    masked: bool


class QueryResultOut(BaseModel):
    columns: list[QueryColumnOut]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: float
    executed_sql: str
    warnings: list[str]


class ExplainOut(BaseModel):
    sql: str
    tables: list[str]
    masked_columns: list[str]
    row_limit: int
    estimated_full_scan: bool
    warnings: list[str]


class SaveQueryIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    sql: Annotated[str, Field(min_length=1, max_length=MAX_SQL_CHARS)]
    description: Annotated[str | None, Field(default=None, max_length=400)] = None
    shared: bool = False


class SavedQueryOut(BaseModel):
    id: str
    name: str
    sql_text: str
    description: str | None
    created_by: str
    created_at: str
    is_shared: bool


class QueryHistoryOut(BaseModel):
    executed_at: str
    actor: str
    accepted: bool
    reject_reason: str | None
    row_count: int | None
    truncated: bool
    duration_ms: float | None
    sql_text: str


# ----------------------------------------------------------------- mappers


def _column_out(column: Any, row_count: int) -> ColumnOut:
    return ColumnOut(
        name=column.name,
        type=column.type,
        nullable=column.nullable,
        primary_key=column.primary_key,
        indexed=column.indexed,
        is_pii=column.is_pii,
        null_count=column.null_count,
        null_pct=column.null_pct(row_count),
        distinct_count=column.distinct_count,
        min_value=column.min_value,
        max_value=column.max_value,
        sample=list(column.sample),
    )


def _table_out(profile: TableProfile) -> TableOut:
    return TableOut(
        name=profile.name,
        row_count=profile.row_count,
        freshness=profile.freshness.isoformat() if profile.freshness else None,
        freshness_column=profile.freshness_column,
        is_large=profile.is_large,
        app_owned=profile.app_owned,
        mapped_source=profile.mapped_source,
        pii_columns=list(profile.pii_columns),
        columns=[_column_out(c, profile.row_count) for c in profile.columns],
    )


def _coverage_out(coverage: JoinCoverage) -> CoverageOut:
    return CoverageOut(
        measured_at=coverage.measured_at.isoformat(),
        rows=[
            CoverageRowOut(
                question=r.question,
                resolved=r.resolved,
                total=r.total,
                pct=r.pct,
                detail=r.detail,
            )
            for r in coverage.rows
        ],
        time_alignment=[
            CoverageRowOut(
                question=r.question,
                resolved=r.resolved,
                total=r.total,
                pct=r.pct,
                detail=r.detail,
            )
            for r in coverage.time_alignment
        ],
        warnings=list(coverage.warnings),
        complaint_to_cell_pct=coverage.complaint_to_cell_pct,
    )


def _catalog_out(snapshot: CatalogSnapshot) -> CatalogOut:
    return CatalogOut(
        generated_at=snapshot.generated_at.isoformat(),
        app_mode=snapshot.app_mode,
        db_backend=snapshot.db_backend,
        total_rows=snapshot.total_rows,
        mapping_summary=snapshot.mapping_summary,
        seed_note=snapshot.seed_note,
        tables=[_table_out(t) for t in snapshot.tables],
        coverage=_coverage_out(snapshot.coverage) if snapshot.coverage else None,
    )


# --------------------------------------------------------------- endpoints


@router.get(
    "/tables",
    response_model=list[str],
    dependencies=[Depends(requires("catalog:read"))],
    summary="Tables available to the explorer",
)
async def list_tables(service: DiscoveryDep) -> list[str]:
    return list(service.list_tables())


@router.get(
    "/catalog",
    response_model=CatalogOut,
    dependencies=[Depends(requires("catalog:read"))],
    summary="Profile every table (counts, nulls, cardinality, freshness, PII flags)",
)
async def catalog(
    service: DiscoveryDep,
    deep: Annotated[bool, Query(description="Include per-column statistics")] = True,
) -> CatalogOut:
    from tazama.modules.discovery.models import ProfileRequest

    snapshot = service.profile_all(ProfileRequest(depth="full" if deep else "shallow"))
    return _catalog_out(snapshot)


@router.get(
    "/catalog/{table}",
    response_model=TableOut,
    dependencies=[Depends(requires("catalog:read"))],
    summary="Profile one table",
)
async def profile_table(
    table: Annotated[str, "canonical table name"],
    service: DiscoveryDep,
    deep: bool = True,
) -> TableOut:
    return _table_out(service.profile_table(table, deep=deep))


@router.get(
    "/join-coverage",
    response_model=CoverageOut,
    dependencies=[Depends(requires("catalog:read"))],
    summary="Complaint to cell/site/incident resolvability and time alignment",
)
async def join_coverage(service: DiscoveryDep) -> CoverageOut:
    return _coverage_out(service.join_coverage())


@router.get(
    "/mappings",
    response_model=MappingReportOut,
    dependencies=[Depends(requires("catalog:read"))],
    summary="Validate config/source_mappings.yaml against the live schema",
)
async def mappings(service: DiscoveryDep) -> MappingReportOut:
    report = service.validate_mappings()
    return MappingReportOut(
        mode=report.mode,
        version=report.version,
        ok=report.ok,
        coverage_pct=report.coverage_pct,
        entities=list(report.entities),
        app_owned=list(report.app_owned),
        issues=[
            MappingIssueOut(entity=i.entity, severity=i.severity, message=i.message)
            for i in report.issues
        ],
    )


@router.post(
    "/query/explain",
    response_model=ExplainOut,
    summary="Validate SQL without running it (what the editor calls as you type)",
)
async def explain(body: QueryIn, service: DiscoveryDep, principal: PrincipalDep) -> ExplainOut:
    guarded = service.explain(body.sql, principal=principal)
    return ExplainOut(
        sql=guarded.sql,
        tables=list(guarded.tables),
        masked_columns=list(guarded.masked_columns),
        row_limit=guarded.row_limit,
        estimated_full_scan=guarded.estimated_full_scan,
        warnings=list(guarded.warnings),
    )


@router.post(
    "/query",
    response_model=QueryResultOut,
    summary="Run a guarded, capped, masked SELECT",
)
async def run_query(
    body: QueryIn,
    service: DiscoveryDep,
    principal: PrincipalDep,
) -> QueryResultOut:
    result = service.run_query(body.sql, principal=principal, max_rows=body.max_rows)
    return QueryResultOut(
        columns=[QueryColumnOut(name=c.name, masked=c.masked) for c in result.columns],
        rows=[list(row) for row in result.rows],
        row_count=result.row_count,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        executed_sql=result.executed_sql,
        warnings=list(result.warnings),
    )


@router.post(
    "/query/export",
    response_class=PlainTextResponse,
    summary="Export a guarded query as masked CSV",
)
async def export_csv(
    body: QueryIn,
    service: DiscoveryDep,
    principal: PrincipalDep,
) -> PlainTextResponse:
    filename, csv_text = service.export_csv(body.sql, principal=principal)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            # The filename is generated server-side, so there is nothing to traverse.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/query/history",
    response_model=list[QueryHistoryOut],
    summary="Recent explorer queries (your own, unless you can read the audit log)",
)
async def query_history(
    service: DiscoveryDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[QueryHistoryOut]:
    return [
        QueryHistoryOut(
            executed_at=entry.executed_at.isoformat(),
            actor=entry.actor,
            accepted=entry.accepted,
            reject_reason=entry.reject_reason,
            row_count=entry.row_count,
            truncated=entry.truncated,
            duration_ms=entry.duration_ms,
            sql_text=entry.sql_text,
        )
        for entry in service.query_history(principal=principal, limit=limit)
    ]


@router.get(
    "/saved-queries",
    response_model=list[SavedQueryOut],
    summary="Your saved queries, plus anything shared with the team",
)
async def list_saved(service: DiscoveryDep, principal: PrincipalDep) -> list[SavedQueryOut]:
    return [_saved_out(q) for q in service.list_saved(principal=principal)]


@router.post(
    "/saved-queries",
    response_model=SavedQueryOut,
    status_code=201,
    summary="Save a query (validated by the guard before it is stored)",
)
async def save_query(
    body: SaveQueryIn,
    service: DiscoveryDep,
    principal: PrincipalDep,
) -> SavedQueryOut:
    record = service.save_query(
        principal=principal,
        name=body.name,
        sql=body.sql,
        description=body.description,
        shared=body.shared,
    )
    return _saved_out(record)


@router.get(
    "/saved-queries/{query_id}",
    response_model=SavedQueryOut,
    summary="Fetch one saved query",
)
async def get_saved(
    query_id: str,
    service: DiscoveryDep,
    principal: PrincipalDep,
) -> SavedQueryOut:
    return _saved_out(service.get_saved(query_id, principal=principal))


@router.delete(
    "/saved-queries/{query_id}",
    status_code=204,
    summary="Delete one of your saved queries",
)
async def delete_saved(query_id: str, service: DiscoveryDep, principal: PrincipalDep) -> None:
    service.delete_saved(query_id, principal=principal)


def _saved_out(record: Any) -> SavedQueryOut:
    return SavedQueryOut(
        id=record.id,
        name=record.name,
        sql_text=record.sql_text,
        description=record.description,
        created_by=record.created_by,
        created_at=record.created_at.isoformat(),
        is_shared=record.is_shared,
    )
