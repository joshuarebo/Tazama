"""Contracts the schema must keep so that one backend cannot diverge from the other.

These are cheap assertions about metadata rather than behaviour, and they catch the class
of mistake that only shows up on Oracle, in production, months later.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy import Boolean, Float, Integer, String, Text

from tazama.infrastructure.db.base import MAX_IDENTIFIER_LEN, Base
from tazama.infrastructure.db.models import (
    EXPLORER_ALLOWLIST,
    PII_COLUMNS,
)
from tazama.infrastructure.db.types import Embedding, JsonText, NonEmptyStr, UtcDateTime

METADATA = Base.metadata


def _migration_module(repo_root: Path, filename: str):
    path = repo_root / "backend" / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"tazama_mig_{filename[:4]}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------- Oracle-safe identifiers


def test_table_names_fit_oracle() -> None:
    too_long = [name for name in METADATA.tables if len(name) > MAX_IDENTIFIER_LEN]
    assert too_long == []


def test_column_names_fit_oracle() -> None:
    too_long = [
        f"{table.name}.{column.name}"
        for table in METADATA.tables.values()
        for column in table.columns
        if len(column.name) > MAX_IDENTIFIER_LEN
    ]
    assert too_long == []


def test_generated_constraint_and_index_names_fit_oracle() -> None:
    """Index and constraint names are generated, so this is where limits get blown."""
    offenders: list[str] = []
    for table in METADATA.tables.values():
        for index in table.indexes:
            if index.name and len(index.name) > MAX_IDENTIFIER_LEN:
                offenders.append(f"index {index.name} ({len(index.name)})")
        for constraint in table.constraints:
            if constraint.name and len(str(constraint.name)) > MAX_IDENTIFIER_LEN:
                offenders.append(f"constraint {constraint.name} ({len(str(constraint.name))})")
    assert offenders == []


def test_identifiers_are_lowercase_snake_case() -> None:
    pattern = re.compile(r"^[a-z][a-z0-9_]*$")
    bad = [
        f"{table.name}.{column.name}"
        for table in METADATA.tables.values()
        for column in table.columns
        if not pattern.match(column.name)
    ] + [name for name in METADATA.tables if not pattern.match(name)]
    assert bad == []


# ----------------------------------------------------------- column types


def test_every_table_has_a_primary_key() -> None:
    missing = [name for name, table in METADATA.tables.items() if not table.primary_key.columns]
    assert missing == []


def test_every_timestamp_column_is_utc_aware() -> None:
    """A plain DateTime column would silently drop the offset on SQLite."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in METADATA.tables.values()
        for column in table.columns
        if type(column.type).__name__ in {"DateTime", "DATETIME", "TIMESTAMP"}
        and not isinstance(column.type, UtcDateTime)
    ]
    assert offenders == []


def test_only_portable_column_types_are_used() -> None:
    allowed = (String, Text, Integer, Float, Boolean, UtcDateTime, JsonText, NonEmptyStr, Embedding)
    offenders = [
        f"{table.name}.{column.name}: {type(column.type).__name__}"
        for table in METADATA.tables.values()
        for column in table.columns
        if not isinstance(column.type, allowed)
    ]
    assert offenders == []


def test_string_columns_declare_a_length() -> None:
    """Oracle needs an explicit length; an unbounded VARCHAR is not portable."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, String)
        and not isinstance(column.type, Text)
        and getattr(column.type, "length", None) is None
    ]
    assert offenders == []


def test_no_raw_msisdn_column_exists_anywhere() -> None:
    """ADR 0008: identity downstream of ingestion is a pseudonym, never a number."""
    forbidden = re.compile(r"(msisdn|phone|mobile_number|^number$)", re.IGNORECASE)
    offenders = [
        f"{table.name}.{column.name}"
        for table in METADATA.tables.values()
        for column in table.columns
        if forbidden.search(column.name) and column.name != "msisdn_format"
    ]
    assert offenders == []


# ------------------------------------------------------------- migrations


def test_json_columns_are_all_constrained_on_oracle(repo_root: Path) -> None:
    """Every JsonText column must appear in the IS JSON migration, or Oracle will accept
    malformed payloads into a CLOB."""
    module = _migration_module(repo_root, "0002_oracle_json_checks.py")
    declared = {(table, column) for table, column in module.JSON_COLUMNS}
    actual = {
        (table.name, column.name)
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, JsonText)
    }
    assert actual - declared == set(), "JSON columns missing an IS JSON constraint"
    assert declared - actual == set(), "migration constrains columns that are not JSON"


def test_json_constraint_names_fit_oracle(repo_root: Path) -> None:
    module = _migration_module(repo_root, "0002_oracle_json_checks.py")
    names = [module._constraint_name(t, c) for t, c in module.JSON_COLUMNS]
    assert all(len(name) <= MAX_IDENTIFIER_LEN for name in names)
    assert len(set(names)) == len(names), "constraint names collide after truncation"


def test_every_migration_implements_downgrade(repo_root: Path) -> None:
    versions = sorted((repo_root / "backend" / "alembic" / "versions").glob("*.py"))
    assert versions, "no migrations found"
    for path in versions:
        source = path.read_text(encoding="utf-8")
        assert "def downgrade()" in source, path.name
        body = source.split("def downgrade()", 1)[1]
        assert body.strip() not in ("-> None:\n    pass", ""), f"{path.name} downgrade is empty"


def test_migrations_cover_every_table(repo_root: Path) -> None:
    source = (
        repo_root / "backend" / "alembic" / "versions" / "0001_canonical_schema.py"
    ).read_text(
        encoding="utf-8",
    )
    missing = [name for name in METADATA.tables if f"'{name}'" not in source]
    assert missing == []


# ------------------------------------------- explorer policy stays in sync


def test_sql_guard_table_allowlist_matches_the_schema_policy() -> None:
    """The guard keeps its own copy (it must not import infrastructure); they must agree."""
    from tazama.modules.discovery import sql_guard

    assert sql_guard.EXPLORER_TABLES == EXPLORER_ALLOWLIST


def test_sql_guard_pii_columns_match_the_schema_policy() -> None:
    from tazama.modules.discovery import sql_guard

    assert sql_guard.PII_COLUMNS == PII_COLUMNS


def test_sql_guard_pii_tables_are_derived_correctly() -> None:
    """The guard must know about every queryable table that carries PII.

    A superset is acceptable (it may also list tables the explorer cannot reach at all,
    such as `audit_log`); a *subset* would let `SELECT *` leak PII, so that is what this
    asserts against.
    """
    from tazama.modules.discovery import sql_guard

    queryable_with_pii = {
        table.name
        for table in METADATA.tables.values()
        if table.name in EXPLORER_ALLOWLIST
        and any(column.name in PII_COLUMNS for column in table.columns)
    }
    assert queryable_with_pii <= sql_guard.PII_TABLES
    assert set(METADATA.tables) >= sql_guard.PII_TABLES


def test_explorer_allowlist_only_names_real_tables() -> None:
    assert EXPLORER_ALLOWLIST - set(METADATA.tables) == set()


def test_explorer_allowlist_excludes_the_sensitive_tables() -> None:
    """Audit, query history, ground truth and seed provenance are never queryable."""
    for name in ("audit_log", "query_audit", "ground_truth", "seed_run", "saved_query"):
        assert name not in EXPLORER_ALLOWLIST, name


def test_pii_columns_all_exist_somewhere() -> None:
    all_columns = {column.name for table in METADATA.tables.values() for column in table.columns}
    assert PII_COLUMNS - all_columns == set()


@pytest.mark.parametrize(
    "column",
    ["raw_text", "subscriber_id", "reason_text", "stated_location", "enterprise_ref"],
)
def test_the_obvious_identifiers_are_marked_pii(column: str) -> None:
    assert column in PII_COLUMNS
