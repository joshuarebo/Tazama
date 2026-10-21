"""Security baseline layer 5: the SELECT-only SQL guard (PRD section 7).

Every rule in `modules/discovery/sql_guard.py` has at least one case here, and every
rejection asserts that the analyst-facing message names the rule that fired — a refusal
that does not say which control refused it is not a usable control.
"""

from __future__ import annotations

import pytest
import sqlglot
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlglot import exp

from tazama.core.errors import QueryRejected
from tazama.modules.discovery.sql_guard import (
    EXPLORER_TABLES,
    MAX_JOINS,
    MAX_QUERY_DEPTH,
    MAX_SQL_CHARS,
    PII_COLUMNS,
    GuardConfig,
    GuardedQuery,
    config_from_settings,
    guard,
    rejection_reason,
)

pytestmark = pytest.mark.security

MAX_ROWS = 1000

SQLITE = GuardConfig(
    allowed_tables=EXPLORER_TABLES, max_rows=MAX_ROWS, timeout_s=15, dialect="sqlite"
)
ORACLE = GuardConfig(
    allowed_tables=EXPLORER_TABLES, max_rows=MAX_ROWS, timeout_s=15, dialect="oracle"
)
PII_ALLOWED = GuardConfig(
    allowed_tables=EXPLORER_TABLES,
    max_rows=MAX_ROWS,
    timeout_s=15,
    dialect="sqlite",
    allow_pii_columns=True,
)


def rule_of(sql: str, config: GuardConfig = SQLITE) -> str:
    """The rule name from a refusal. Fails the test when the query is accepted."""
    reason = rejection_reason(sql, config)
    assert reason is not None, f"expected a rejection, got an accepted query: {sql!r}"
    return reason.split(":", 1)[0]


# ----------------------------------------------------------------- accepted


ACCEPTED: list[tuple[str, str]] = [
    ("simple-select", "select id, name from site"),
    (
        "where-and-time-bound",
        "select id from kpi_cell where cell_id = 'C1' and bucket_start >= '2026-01-01'",
    ),
    ("join-two-allowlisted", "select c.id, s.name from cell c join site s on c.site_id = s.id"),
    (
        "cte-read-only",
        "with recent as (select id from incident where opened_at > '2026-01-01')"
        " select id from recent",
    ),
    ("union-of-allowed", "select id from site union select id from cell"),
    ("union-all-of-allowed", "select id from site union all select id from cell"),
    (
        "aggregate",
        "select region, count(*) as n from site group by region"
        " having count(*) > 2 order by n desc",
    ),
    ("small-limit", "select id from site limit 10"),
    ("intersect-of-allowed", "select id from site intersect select site_id from cell"),
    ("subquery-in-where", "select id from cell where site_id in (select id from site)"),
    (
        "named-pii-column-with-time-bound",
        "select raw_text from complaint where received_at > '2026-01-01'",
    ),
    ("star-on-table-without-pii", "select * from site"),
    ("trailing-semicolon", "select id from site;"),
]


@pytest.mark.parametrize(("label", "sql"), ACCEPTED, ids=[label for label, _ in ACCEPTED])
def test_accepted_queries(label: str, sql: str) -> None:
    result = guard(sql, SQLITE)
    assert isinstance(result, GuardedQuery)
    assert result.original_sql == sql
    assert rejection_reason(sql, SQLITE) is None, label


def test_accepted_oracle_fetch_first_is_preserved() -> None:
    result = guard("select id from site fetch first 5 rows only", ORACLE)
    assert result.row_limit == 5
    assert "FETCH FIRST 5 ROWS ONLY" in result.sql


def test_accepted_query_reports_its_tables() -> None:
    result = guard("select c.id, s.name from cell c join site s on c.site_id = s.id", SQLITE)
    assert set(result.tables) == {"cell", "site"}


def test_cte_alias_is_not_treated_as_a_table() -> None:
    result = guard("with x as (select id from site) select id from x", SQLITE)
    assert result.tables == ("site",)


def test_accepted_query_columns_are_reported() -> None:
    result = guard("select id, name from site", SQLITE)
    assert result.columns == ("id", "name")


def test_star_projection_is_reported_as_star() -> None:
    assert guard("select * from site", SQLITE).columns == ("*",)


# ------------------------------------------------- rejected: DML / DDL / commands


DML_DDL: list[tuple[str, str]] = [
    ("insert", "insert into cell (id) values ('C1')"),
    ("update", "update cell set name = 'x'"),
    ("delete", "delete from cell"),
    (
        "merge",
        "merge into cell using site on (cell.site_id = site.id)"
        " when matched then update set name = 'x'",
    ),
    ("drop", "drop table cell"),
    ("alter", "alter table cell add column x int"),
    ("create", "create table t as select id from site"),
    ("truncate", "truncate table cell"),
    ("grant", "grant select on cell to bob"),
    ("commit", "commit"),
    ("rollback", "rollback"),
    ("set", "set x = 1"),
    ("explain", "explain select id from site"),
    ("pragma", "pragma table_info(cell)"),
    ("attach", "attach database 'evil.db' as evil"),
    ("vacuum", "vacuum"),
    ("call", "call some_procedure()"),
]


@pytest.mark.parametrize(("label", "sql"), DML_DDL, ids=[label for label, _ in DML_DDL])
def test_non_select_statements_are_rejected(label: str, sql: str) -> None:
    config = ORACLE if label in {"merge", "grant", "call"} else SQLITE
    assert rule_of(sql, config) in {"select-only", "dml-or-ddl", "banned-construct"}, label


def test_dml_inside_a_cte_is_rejected() -> None:
    sql = "with x as (delete from cell returning 1) select * from x"
    assert rule_of(sql) == "dml-or-ddl"


def test_dml_inside_a_subquery_is_rejected() -> None:
    sql = (
        "select id from site where id in"
        " (with d as (delete from cell returning id) select id from d)"
    )
    assert rule_of(sql) == "dml-or-ddl"


def test_select_into_is_rejected() -> None:
    assert rule_of("select id into v_holder from cell", ORACLE) == "select-into"


def test_locking_clause_is_rejected() -> None:
    assert rule_of("select id from cell for update") == "locking-clause"


# ------------------------------------------------------ rejected: statement shape


def test_stacked_statements_are_rejected() -> None:
    assert rule_of("select 1; drop table cell") == "single-statement"


def test_trailing_semicolon_content_is_rejected() -> None:
    assert rule_of("select id from site;;") == "single-statement"


def test_plsql_begin_end_block_is_rejected() -> None:
    assert rule_of("begin execute immediate 'drop table cell'; end;", ORACLE) == "plsql-block"


def test_plsql_declare_is_rejected() -> None:
    assert rule_of("declare v number; begin null; end;", ORACLE) == "plsql-block"


def test_unparseable_junk_is_rejected() -> None:
    assert rule_of("not sql at all !!! ???") == "unparseable"


def test_empty_string_is_rejected() -> None:
    assert rule_of("") == "empty-statement"


def test_whitespace_only_is_rejected() -> None:
    assert rule_of("   \n\t  ") == "empty-statement"


def test_nul_byte_is_rejected() -> None:
    assert rule_of("select id from site where name = 'a\x00b'") == "control-character"


def test_control_character_is_rejected() -> None:
    assert rule_of("select id\x07 from site") == "control-character"


def test_overlong_query_is_rejected() -> None:
    sql = "select id from site where name = '" + "a" * 25_000 + "'"
    assert len(sql) > MAX_SQL_CHARS
    assert rule_of(sql) == "too-long"


def test_too_many_joins_is_rejected() -> None:
    joins = " ".join(f"join cell c{i} on c{i}.site_id = site.id" for i in range(12))
    assert rule_of(f"select site.id from site {joins}") == "too-many-joins"


def test_join_count_at_the_limit_is_accepted() -> None:
    joins = " ".join(f"join cell c{i} on c{i}.site_id = site.id" for i in range(MAX_JOINS))
    assert rejection_reason(f"select site.id from site {joins}", SQLITE) is None


def test_deep_nesting_is_rejected() -> None:
    sql = "select id from " + "(select id from " * 25 + "site" + ")" * 25
    assert rule_of(sql) == "too-deep"


def test_shallow_nesting_is_accepted() -> None:
    sql = "select id from (select id from (select id from site) a) b"
    assert rejection_reason(sql, SQLITE) is None
    assert MAX_QUERY_DEPTH >= 20


# ------------------------------------------------------------ rejected: comments


def test_line_comment_is_rejected() -> None:
    assert rule_of("select id from site -- and now something else") == "comment-not-allowed"


def test_block_comment_is_rejected() -> None:
    assert rule_of("select /* sneaky */ id from site") == "comment-not-allowed"


def test_oracle_hint_shaped_comment_is_rejected() -> None:
    assert rule_of("select /*+ full(site) */ id from site", ORACLE) == "comment-not-allowed"


def test_tautology_smuggled_in_a_comment_is_rejected() -> None:
    sql = "select id from site where id = 'x' /* ' or 1=1 -- */"
    assert rule_of(sql) == "comment-not-allowed"


# ----------------------------------------------------------- rejected: allowlist


def test_table_outside_the_allowlist_is_rejected() -> None:
    assert rule_of("select id from audit_log") == "table-not-allowed"


def test_union_branch_from_a_forbidden_table_is_rejected() -> None:
    sql = "select id from site union select name from sqlite_master"
    assert rule_of(sql) == "table-not-allowed"


def test_sqlite_master_is_rejected() -> None:
    assert rule_of("select name from sqlite_master") == "table-not-allowed"


def test_dual_is_rejected() -> None:
    assert rule_of("select 1 from dual", ORACLE) == "table-not-allowed"


def test_user_tables_is_rejected() -> None:
    assert rule_of("select table_name from user_tables", ORACLE) == "table-not-allowed"


def test_all_tables_is_rejected() -> None:
    assert rule_of("select table_name from all_tables", ORACLE) == "table-not-allowed"


def test_dba_users_is_rejected() -> None:
    assert rule_of("select username from dba_users", ORACLE) == "table-not-allowed"


def test_v_dollar_session_is_rejected() -> None:
    assert rule_of('select sid from "V$SESSION"', ORACLE) == "table-not-allowed"


def test_information_schema_is_rejected() -> None:
    assert rule_of("select table_name from information_schema.tables") == "schema-qualified"


def test_schema_qualified_table_is_rejected() -> None:
    assert rule_of("select id from other_schema.site") == "schema-qualified"


def test_sys_user_dollar_is_rejected() -> None:
    assert rule_of("select name from sys.user$", ORACLE) == "schema-qualified"


def test_subquery_table_is_also_allowlisted() -> None:
    sql = "select id from site where id in (select name from sqlite_master)"
    assert rule_of(sql) == "table-not-allowed"


def test_a_narrower_allowlist_is_honoured() -> None:
    narrow = GuardConfig(allowed_tables=frozenset({"site"}), max_rows=10, timeout_s=5)
    assert rejection_reason("select id from site", narrow) is None
    assert rule_of("select id from cell", narrow) == "table-not-allowed"


# -------------------------------------------------- rejected: dangerous functions


DANGEROUS: list[tuple[str, str]] = [
    ("sleep", "select sleep(5)"),
    ("pg_sleep", "select pg_sleep(5)"),
    ("benchmark", "select benchmark(1000000, md5('a'))"),
    ("load_file", "select load_file('/etc/passwd')"),
    ("readfile", "select readfile('/etc/passwd')"),
    ("writefile", "select writefile('/tmp/x', 'y')"),
    ("randomblob", "select randomblob(1000000000)"),
    ("utl_http", "select utl_http.request('http://evil') from site"),
    ("utl_file", "select utl_file.fopen('D', 'f', 'r') from site"),
    ("utl_smtp", "select utl_smtp.open_connection('h', 25) from site"),
    ("dbms_scheduler", "select dbms_scheduler.create_job('j') from site"),
    ("dbms_lock_sleep", "select dbms_lock.sleep(5) from site"),
]


@pytest.mark.parametrize(("label", "sql"), DANGEROUS, ids=[label for label, _ in DANGEROUS])
def test_dangerous_functions_are_rejected(label: str, sql: str) -> None:
    assert rule_of(sql, ORACLE) == "banned-function", label


def test_dangerous_function_inside_a_subquery_is_rejected() -> None:
    sql = "select id from site where id in (select sleep(5))"
    assert rule_of(sql) == "banned-function"


CONSTRUCTS: list[tuple[str, str]] = [
    ("into-outfile", "select id into outfile '/tmp/dump' from site"),
    ("into-dumpfile", "select id into dumpfile '/tmp/dump' from site"),
    ("execute-immediate", "select id from site where 1 = 1 and execute immediate 'x'"),
    ("xp_cmdshell", "exec xp_cmdshell 'dir'"),
    ("waitfor", "select id from site; waitfor delay '0:0:5'"),
    ("load-extension", "select load_extension('evil.so')"),
    ("dot-shell", ".shell rm -rf /"),
]


@pytest.mark.parametrize(("label", "sql"), CONSTRUCTS, ids=[label for label, _ in CONSTRUCTS])
def test_dangerous_constructs_are_rejected(label: str, sql: str) -> None:
    assert rule_of(sql) in {"banned-construct", "banned-function"}, label


# ------------------------------------------------------------- rewriting: row cap


def test_missing_limit_gains_the_cap() -> None:
    result = guard("select id from site", SQLITE)
    assert result.sql.endswith(f"LIMIT {MAX_ROWS}")
    assert result.row_limit == MAX_ROWS
    assert any("row limit" in warning.lower() for warning in result.warnings)


def test_oversized_limit_is_lowered_to_the_cap() -> None:
    result = guard("select id from site limit 10000", SQLITE)
    assert result.row_limit == MAX_ROWS
    assert "LIMIT 10000" not in result.sql
    assert f"LIMIT {MAX_ROWS}" in result.sql


def test_smaller_limit_is_kept() -> None:
    result = guard("select id from site limit 7", SQLITE)
    assert result.row_limit == 7
    assert "LIMIT 7" in result.sql


def test_oracle_dialect_emits_fetch_first() -> None:
    result = guard("select id from site", ORACLE)
    assert f"FETCH FIRST {MAX_ROWS} ROWS ONLY" in result.sql
    assert "LIMIT" not in result.sql


def test_union_is_capped_as_a_whole() -> None:
    result = guard("select id from site union all select id from cell", SQLITE)
    assert result.row_limit == MAX_ROWS
    assert f"LIMIT {MAX_ROWS}" in result.sql


def test_oracle_fetch_percent_is_recapped() -> None:
    # `FETCH FIRST 50 PERCENT` is not a row count, so the cap must replace it.
    result = guard("select id from site fetch first 50 percent rows only", ORACLE)
    assert result.row_limit == MAX_ROWS
    assert "PERCENT" not in result.sql


@pytest.mark.parametrize(
    "sql",
    [
        "select id from site",
        "select id from site limit 10000",
        "select id from site union all select id from cell",
        "select raw_text from complaint where received_at > '2026-01-01'",
        "with x as (select id from site) select id from x",
    ],
)
def test_rewritten_sql_still_parses_as_one_read_statement(sql: str) -> None:
    result = guard(sql, SQLITE)
    statements = sqlglot.parse(result.sql, read="sqlite")
    assert len(statements) == 1
    assert isinstance(statements[0], exp.Select | exp.SetOperation)


def test_rewritten_sql_references_only_the_original_tables() -> None:
    result = guard("select raw_text from complaint where received_at > '2026-01-01'", SQLITE)
    parsed = sqlglot.parse_one(result.sql, read="sqlite")
    rewritten_tables = {table.name.lower() for table in parsed.find_all(exp.Table)}
    assert rewritten_tables == set(result.tables) == {"complaint"}


# ----------------------------------------------------------------- PII masking


def test_raw_text_is_masked() -> None:
    result = guard("select raw_text from complaint where received_at > '2026-01-01'", SQLITE)
    assert result.masked_columns == ("raw_text",)
    assert "SUBSTRING(raw_text, 1, 2) || '***' AS raw_text" in result.sql


def test_subscriber_id_is_masked() -> None:
    result = guard("select subscriber_id from care_call where started_at > '2026-01-01'", SQLITE)
    assert result.masked_columns == ("subscriber_id",)
    assert "AS subscriber_id" in result.sql


def test_masking_keeps_an_explicit_alias() -> None:
    sql = "select raw_text as text_preview from complaint where received_at > '2026-01-01'"
    result = guard(sql, SQLITE)
    assert result.masked_columns == ("raw_text",)
    assert "AS text_preview" in result.sql


def test_masking_is_dialect_correct_on_oracle() -> None:
    result = guard("select raw_text from complaint where received_at > '2026-01-01'", ORACLE)
    assert "SUBSTR(raw_text, 1, 2) || '***' AS raw_text" in result.sql


def test_non_pii_columns_are_untouched() -> None:
    result = guard("select id from complaint where received_at > '2026-01-01'", SQLITE)
    assert result.masked_columns == ()
    assert "***" not in result.sql


def test_allow_pii_columns_disables_masking() -> None:
    result = guard("select raw_text, subscriber_id from complaint", PII_ALLOWED)
    assert result.masked_columns == ()
    assert "***" not in result.sql
    assert "raw_text" in result.sql


def test_star_on_a_pii_table_is_rejected() -> None:
    reason = rejection_reason("select * from complaint", SQLITE)
    assert reason is not None
    assert reason.startswith("star-with-pii")
    # The message must tell the analyst what to do instead of silently returning PII.
    assert "Name the columns" in reason


def test_star_on_subscriber_is_rejected() -> None:
    assert rule_of("select * from subscriber") == "star-with-pii"


def test_qualified_star_on_a_pii_table_is_rejected() -> None:
    assert rule_of("select c.* from complaint c") == "star-with-pii"


def test_count_star_on_a_pii_table_is_allowed() -> None:
    # `count(*)` returns no personal data, so it must not trip the star rule.
    assert rejection_reason("select count(*) from complaint", SQLITE) is None


def test_every_declared_pii_column_is_masked_when_selected() -> None:
    for column in sorted(PII_COLUMNS & {"raw_text", "reason_text", "stated_location"}):
        result = guard(f"select {column} from complaint", PII_ALLOWED)
        assert result.masked_columns == ()
        masked = guard(f"select {column} from complaint", SQLITE)
        assert masked.masked_columns == (column,)


# -------------------------------------------------------------- full-scan flag


def test_unbounded_large_table_read_warns() -> None:
    result = guard("select id from kpi_cell", SQLITE)
    assert result.estimated_full_scan is True
    assert any("time bound" in warning for warning in result.warnings)


def test_time_bounded_large_table_read_does_not_warn() -> None:
    result = guard("select id from kpi_cell where bucket_start > '2026-01-01'", SQLITE)
    assert result.estimated_full_scan is False
    assert not any("time bound" in warning for warning in result.warnings)


def test_non_time_predicate_on_a_large_table_still_warns() -> None:
    result = guard("select id from kpi_cell where cell_id = 'C1'", SQLITE)
    assert result.estimated_full_scan is True


def test_small_table_without_a_where_clause_does_not_warn() -> None:
    assert guard("select id from site", SQLITE).estimated_full_scan is False


# ------------------------------------------------------- config_from_settings


def test_config_from_settings_uses_the_env_caps(settings: object) -> None:
    config = config_from_settings(settings)  # type: ignore[arg-type]
    assert config.max_rows == settings.SQL_EXPLORER_MAX_ROWS  # type: ignore[attr-defined]
    assert config.timeout_s == settings.SQL_EXPLORER_TIMEOUT_S  # type: ignore[attr-defined]
    assert config.dialect == "sqlite"
    assert config.allow_pii_columns is False
    assert config.require_read_only is False
    assert config.allowed_tables == EXPLORER_TABLES


def test_config_from_settings_picks_the_oracle_dialect(settings: object) -> None:
    oracle_settings = settings.model_copy(update={"DB_BACKEND": "oracle"})  # type: ignore[attr-defined]
    assert config_from_settings(oracle_settings).dialect == "oracle"


def test_live_mode_requires_a_read_only_connection(settings: object) -> None:
    live = settings.model_copy(update={"APP_MODE": "live"})  # type: ignore[attr-defined]
    config = config_from_settings(live)
    assert config.require_read_only is True
    result = guard("select id from site", config)
    assert any("read-only" in warning for warning in result.warnings)


def test_live_mode_still_refuses_tables_outside_the_allowlist(settings: object) -> None:
    live = settings.model_copy(update={"APP_MODE": "live"})  # type: ignore[attr-defined]
    assert rule_of("select name from sqlite_master", config_from_settings(live)) == (
        "table-not-allowed"
    )


def test_config_from_settings_accepts_an_explicit_allowlist(settings: object) -> None:
    config = config_from_settings(
        settings,  # type: ignore[arg-type]
        allow_pii=True,
        allowed_tables=frozenset({"site"}),
    )
    assert config.allowed_tables == frozenset({"site"})
    assert config.allow_pii_columns is True


# --------------------------------------------------------------- guard surface


def test_guard_raises_query_rejected_not_a_generic_error() -> None:
    with pytest.raises(QueryRejected) as excinfo:
        guard("drop table cell", SQLITE)
    assert excinfo.value.status == 400
    assert excinfo.value.extra["rule"] == "select-only"


def test_rejection_reason_returns_none_for_an_accepted_query() -> None:
    assert rejection_reason("select id from site", SQLITE) is None


def test_refusal_never_echoes_the_submitted_sql() -> None:
    # The message is shown to an analyst and copied into the audit row; it must name the
    # rule without replaying the payload.
    payload = "select secret_marker_value from sqlite_master"
    reason = rejection_reason(payload, SQLITE)
    assert reason is not None
    assert "secret_marker_value" not in reason


def test_guard_does_not_mutate_the_callers_statement() -> None:
    sql = "select id from site"
    first = guard(sql, SQLITE)
    second = guard(sql, SQLITE)
    assert first.sql == second.sql
    assert first.original_sql == sql


# ---------------------------------------------------------------- property test


@given(
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        max_size=40,
    )
)
@hypothesis_settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_guard_either_accepts_a_read_or_raises_query_rejected(text: str) -> None:
    """The guard is total: it never leaks another exception and never returns a write."""
    try:
        result = guard(text, SQLITE)
    except QueryRejected:
        return
    statements = sqlglot.parse(result.sql, read="sqlite")
    assert len(statements) == 1
    root = statements[0]
    assert isinstance(root, exp.Select | exp.SetOperation)
    assert not any(
        isinstance(node, exp.Insert | exp.Update | exp.Delete | exp.Merge | exp.Drop)
        for node in root.walk()
    )
