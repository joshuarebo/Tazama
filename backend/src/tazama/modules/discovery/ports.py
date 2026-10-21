"""Ports for the discovery module.

Everything the service needs from the outside world is declared here as a Protocol, and
satisfied structurally by an adapter in `infrastructure/`. The module therefore never
imports infrastructure, and the service can be tested with in-memory fakes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tazama.modules.discovery.models import (
    JoinCoverage,
    QueryAuditRecord,
    SavedQueryRecord,
    TableProfile,
)


@runtime_checkable
class CatalogReader(Protocol):
    """Schema metadata and per-table profiling."""

    def list_tables(self) -> tuple[str, ...]:
        """Canonical tables present in the database, in a stable order."""
        ...

    def canonical_columns(self) -> dict[str, frozenset[str]]:
        """Table -> column names, used to validate the source mappings."""
        ...

    def profile_table(
        self,
        table: str,
        *,
        deep: bool = True,
        sample_rows: int = 3,
    ) -> TableProfile:
        """Row count, column metadata and (when `deep`) per-column statistics."""
        ...


@runtime_checkable
class CoverageReader(Protocol):
    """Join coverage and time-alignment measurements."""

    def join_coverage(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> JoinCoverage: ...


@runtime_checkable
class QueryRunner(Protocol):
    """Executes an already-guarded, already-capped SELECT."""

    def run(
        self,
        sql: str,
        *,
        timeout_s: int,
        max_rows: int,
    ) -> tuple[tuple[str, ...], list[tuple[Any, ...]], bool]:
        """Return `(column_names, rows, truncated)`.

        The runner never builds SQL and never interpolates values: it receives the exact
        statement the guard produced.
        """
        ...


@runtime_checkable
class QueryAuditSink(Protocol):
    """Append-only record of every explorer query, accepted or refused."""

    def record(self, entry: QueryAuditRecord) -> None: ...

    def recent(
        self, *, actor: str | None = None, limit: int = 50
    ) -> tuple[QueryAuditRecord, ...]: ...


@runtime_checkable
class SavedQueryStore(Protocol):
    def save(self, record: SavedQueryRecord) -> SavedQueryRecord: ...

    def list_for(self, actor: str) -> tuple[SavedQueryRecord, ...]: ...

    def get(self, query_id: str, *, actor: str) -> SavedQueryRecord | None: ...

    def delete(self, query_id: str, *, actor: str) -> bool: ...


@runtime_checkable
class SeedProvenanceReader(Protocol):
    """Reads the `seed_run` row so the catalog can state how the data was produced."""

    def describe_latest(self) -> str: ...
