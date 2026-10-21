"""SELECT-only SQL guard for the Data Explorer (security baseline layer 5).

This is the *only* place in TAZAMA where user-supplied SQL is parsed, validated and
rewritten before it reaches a database. Everything the explorer executes comes out of
:func:`guard`; nothing else may hand raw text to a cursor.

Two layers, on purpose
----------------------
1. **AST (primary).** The statement is parsed with ``sqlglot`` for the target dialect and
   every rule is decided on the tree: statement count, top-level type, DML/DDL anywhere in
   the tree (including CTE bodies and subqueries), the table allowlist, dangerous function
   names, shape limits, the row cap rewrite and the PII projection rewrite. A parse failure
   is a rejection — we never fall back to "allow because the regex found nothing".
2. **Regex deny-list (secondary, pre-parse).** A parser *normalises away* exactly the
   things an attacker uses to smuggle intent past a scanner: comments (``--``, ``/* */``,
   Oracle hint-shaped ``/*+ ... */``) vanish into node metadata, and control characters are
   swallowed as whitespace. Some hostile constructs also do not parse at all
   (``INTO OUTFILE``, ``EXECUTE IMMEDIATE``, PL/SQL ``BEGIN ... END;``), so an AST-only
   check could never name the rule that fired. The regex layer runs first so the analyst
   gets a specific message instead of "unparseable".

Purity: no database access, no I/O, no logging of the SQL body. The caller audits
(``query_audit``) and applies ``GuardConfig.timeout_s`` to the cursor; the guard only
decides and rewrites. SQL is generated from the AST only — user text is never
interpolated into a statement.

Policy data is *copied* here rather than imported: ``modules/discovery`` must not import
``tazama.infrastructure`` (import-linter contract "modules do not reach into
infrastructure or the api layer"). :data:`EXPLORER_TABLES`, :data:`PII_COLUMNS` and
:data:`PII_TABLES` are kept in sync with
``infrastructure.db.models.EXPLORER_ALLOWLIST`` / ``...PII_COLUMNS``, asserted by a test in
the discovery test suite. Callers may always pass their own allowlist through
:class:`GuardConfig`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import sqlglot
from sqlglot import exp

from tazama.core.errors import QueryRejected

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tazama.core.settings import Settings

__all__ = [
    "EXPLORER_TABLES",
    "LARGE_TABLES",
    "MAX_JOINS",
    "MAX_QUERY_DEPTH",
    "MAX_SQL_CHARS",
    "PII_COLUMNS",
    "PII_TABLES",
    "GuardConfig",
    "GuardedQuery",
    "config_from_settings",
    "guard",
    "rejection_reason",
]

# --------------------------------------------------------------------- policy

#: Kept in sync with ``infrastructure.db.models.EXPLORER_ALLOWLIST``, asserted by a test in
#: the discovery test suite. Anything not listed is refused, even for an admin: the
#: allowlist is the control, not the role.
EXPLORER_TABLES: Final[frozenset[str]] = frozenset(
    {
        "site",
        "cell",
        "topology_node",
        "topology_edge",
        "alarm",
        "kpi_cell",
        "maintenance_window",
        "subscriber",
        "complaint",
        "care_call",
        "incident",
        "incident_link",
        "diagnosis",
        "case_record",
        "runbook",
        "recovery_action",
        "verification",
        "notification",
        "knowledge_doc",
        "prediction",
        "model_registry",
        "eval_run",
        "scheduled_report",
        "report_delivery",
    },
)

#: Kept in sync with ``infrastructure.db.models.PII_COLUMNS``.
PII_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "subscriber_id",
        "enterprise_ref",
        "raw_text",
        "reason_text",
        "rendered_text",
        "stated_location",
        "agent_ref",
        "approved_by",
        "actor",
        "client_ip",
        "recipients",
    },
)

#: Tables that carry at least one column in :data:`PII_COLUMNS`. ``SELECT *`` on one of
#: these is refused rather than silently masked, because a star projection hides from the
#: analyst (and from the audit row) exactly which sensitive columns were read.
PII_TABLES: Final[frozenset[str]] = frozenset(
    {
        "complaint",
        "care_call",
        "subscriber",
        "notification",
        "recovery_action",
        "maintenance_window",
        "scheduled_report",
        "audit_log",
        "query_audit",
    },
)

#: Tables big enough that an unbounded read is worth warning about (PRD section 7:
#: "partition-friendly, time-bounded queries").
LARGE_TABLES: Final[frozenset[str]] = frozenset(
    {"kpi_cell", "complaint", "care_call", "audit_log", "subscriber"},
)

#: Catalog/dictionary objects an attacker reaches for first. They are already outside the
#: allowlist; naming them lets the guard explain *why* without leaking schema details.
_SYSTEM_OBJECTS: Final[frozenset[str]] = frozenset(
    {
        "sqlite_master",
        "sqlite_temp_master",
        "sqlite_schema",
        "sqlite_sequence",
        "dual",
        "all_tables",
        "user_tables",
        "dba_tables",
        "all_tab_columns",
        "user_tab_columns",
        "dba_users",
        "all_users",
        "user_objects",
        "all_objects",
        "v$session",
        "v$version",
        "v$database",
        "v$instance",
        "user$",
        "gv$session",
        "pg_catalog",
        "pg_tables",
        "pg_shadow",
        "information_schema",
        "tables",
        "columns",
    },
)

#: Function / package names refused wherever they appear. Timing oracles, file and network
#: egress, OS execution, scheduler abuse.
_BANNED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "sleep",
        "pg_sleep",
        "benchmark",
        "waitfor",
        "delay",
        "dbms_lock",
        "dbms_scheduler",
        "dbms_job",
        "dbms_pipe",
        "dbms_sql",
        "utl_http",
        "utl_file",
        "utl_smtp",
        "utl_tcp",
        "utl_inaddr",
        "httpuritype",
        "load_file",
        "readfile",
        "writefile",
        "randomblob",
        "load_extension",
        "xp_cmdshell",
        "sp_oacreate",
        "exec",
        "execute",
        "edit",
        "shell",
        "system",
    },
)

MAX_SQL_CHARS: Final[int] = 20_000
#: Nested *query scopes* (SELECT / subquery / CTE / set operation / parenthesis), not raw
#: AST nodes: a hand-written analyst query uses two or three, a parser-stressing payload
#: uses hundreds.
MAX_QUERY_DEPTH: Final[int] = 20
MAX_JOINS: Final[int] = 10

# Control characters are stripped by the tokeniser, so they are caught before parsing.
# Tab / newline / carriage return are ordinary formatting and allowed.
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Comments never survive into the AST in a checkable form; `--` and `/* */` (including
# Oracle hint-shaped `/*+ ... */`) are the classic way to smuggle intent past a scanner.
_COMMENT: Final[re.Pattern[str]] = re.compile(r"(--|/\*|\*/|#\s)")
# Anonymous PL/SQL blocks do not parse at all; name the rule instead of saying "unparseable".
_PLSQL_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"^\s*(declare|begin)\b|\bend\s*;\s*$|\bend\s+loop\b", re.IGNORECASE
)
# Constructs the parser cannot represent, so the AST layer could never name them.
_BANNED_CONSTRUCTS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("INTO OUTFILE", re.compile(r"\binto\s+outfile\b", re.IGNORECASE)),
    ("INTO DUMPFILE", re.compile(r"\binto\s+dumpfile\b", re.IGNORECASE)),
    ("EXECUTE IMMEDIATE", re.compile(r"\bexecute\s+immediate\b", re.IGNORECASE)),
    ("WAITFOR", re.compile(r"\bwaitfor\b", re.IGNORECASE)),
    ("XP_CMDSHELL", re.compile(r"\bxp_cmdshell\b", re.IGNORECASE)),
    ("EXEC", re.compile(r"\bexecu?t?e?\s+\w+\s*('|\")", re.IGNORECASE)),
    ("LOAD EXTENSION", re.compile(r"\bload[_\s]extension\b", re.IGNORECASE)),
    ("dot-command", re.compile(r"(^|\s)\.(shell|load|import|output|system|read|edit)\b")),
)

#: Top-level expression types that are reads. Set operations are SELECTs joined by
#: UNION / INTERSECT / EXCEPT and every branch is validated like any other SELECT.
_READ_TOP_LEVEL: Final[tuple[type[exp.Query], ...]] = (exp.Select, exp.SetOperation)

#: Any of these anywhere in the tree is a write or an administrative command, including
#: inside a CTE body or a subquery (``WITH x AS (DELETE ... RETURNING 1) SELECT * FROM x``).
_WRITE_NODES: Final[tuple[type[exp.Expr], ...]] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Set,
    exp.Use,
    exp.Commit,
    exp.Rollback,
    exp.Transaction,
    exp.Copy,
    exp.Describe,
    exp.Analyze,
    exp.Cache,
    exp.Uncache,
    exp.Export,
    exp.Put,
    exp.Kill,
    exp.Refresh,
)

_DEPTH_NODES: Final[tuple[type[exp.Expr], ...]] = (
    exp.Select,
    exp.Subquery,
    exp.CTE,
    exp.SetOperation,
    exp.Paren,
)

#: A WHERE predicate on one of these (or on a ``*_at`` / ``*_start`` / ``*_end`` /
#: ``*_time`` column) counts as a time bound for the full-scan warning.
_TIME_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ts", "bucket", "day", "hour", "event_time", "period", "window"},
)
_TIME_SUFFIXES: Final[tuple[str, ...]] = ("_at", "_start", "_end", "_time", "_date", "_ts")


# ------------------------------------------------------------------ dataclasses


@dataclass(frozen=True, slots=True)
class GuardConfig:
    """Everything the guard is allowed to know. Built by :func:`config_from_settings`."""

    allowed_tables: frozenset[str]
    max_rows: int
    timeout_s: int
    dialect: str = "sqlite"  # "sqlite" | "oracle"
    allow_pii_columns: bool = False
    #: Live mode: the executor must use the read-only source connection (PRD section 4).
    require_read_only: bool = False


@dataclass(frozen=True, slots=True)
class GuardedQuery:
    """An accepted query plus the rewritten statement that is actually executed."""

    sql: str
    original_sql: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    masked_columns: tuple[str, ...]
    row_limit: int
    estimated_full_scan: bool
    warnings: tuple[str, ...]


def config_from_settings(
    settings: Settings,
    *,
    allow_pii: bool = False,
    allowed_tables: frozenset[str] = EXPLORER_TABLES,
) -> GuardConfig:
    """Build a :class:`GuardConfig` from the environment contract.

    Caps and timeout come from ``SQL_EXPLORER_MAX_ROWS`` / ``SQL_EXPLORER_TIMEOUT_S``, the
    dialect from ``DB_BACKEND``. When ``APP_MODE=live`` the config records that the query
    must run on a read-only connection; the allowlist is unchanged, because it is already
    the only set of readable tables in either mode.
    """
    return GuardConfig(
        allowed_tables=allowed_tables,
        max_rows=int(settings.SQL_EXPLORER_MAX_ROWS),
        timeout_s=int(settings.SQL_EXPLORER_TIMEOUT_S),
        dialect="oracle" if settings.DB_BACKEND == "oracle" else "sqlite",
        allow_pii_columns=allow_pii,
        require_read_only=settings.APP_MODE == "live",
    )


# ----------------------------------------------------------------- rejections


def _reject(rule: str, explanation: str) -> QueryRejected:
    """Build the analyst-facing refusal. It names the rule and never echoes the SQL."""
    return QueryRejected(f"{rule}: {explanation}", rule=rule)


# ------------------------------------------------------- layer 2: pre-parse regex


def _check_text(sql: str) -> None:
    """Checks that must happen before parsing, because parsing destroys the evidence."""
    if not sql or not sql.strip():
        raise _reject("empty-statement", "Enter a SELECT statement.")
    if len(sql) > MAX_SQL_CHARS:
        raise _reject(
            "too-long",
            f"The statement is {len(sql)} characters; the limit is {MAX_SQL_CHARS}.",
        )
    if _CONTROL_CHARS.search(sql):
        raise _reject(
            "control-character",
            "The statement contains a NUL byte or control character. Use plain text.",
        )
    if _COMMENT.search(sql):
        raise _reject(
            "comment-not-allowed",
            "SQL comments (--, /* */) are not allowed in the explorer. Remove them and "
            "express the condition in the query itself.",
        )
    if _PLSQL_BLOCK.search(sql):
        raise _reject(
            "plsql-block",
            "PL/SQL blocks (DECLARE / BEGIN ... END) are not allowed. Only a single "
            "SELECT statement is accepted.",
        )
    for label, pattern in _BANNED_CONSTRUCTS:
        if pattern.search(sql):
            raise _reject(
                "banned-construct",
                f"{label} is not allowed in the explorer.",
            )


# ---------------------------------------------------------- layer 1: AST checks


def _parse_one(sql: str, dialect: str) -> exp.Expression:
    """Parse to exactly one expression or reject. Never returns ``None``."""
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except RecursionError as err:  # a parser-stressing payload, not a query
        raise _reject("too-deep", "The statement is nested too deeply to analyse.") from err
    except Exception as err:  # any parser failure is a rejection
        raise _reject(
            "unparseable",
            "The statement could not be parsed as SQL. Check the syntax and try again.",
        ) from err

    if len(statements) > 1:
        raise _reject(
            "single-statement",
            "Only one statement may be submitted. Remove the extra statement or the "
            "trailing semicolon content.",
        )
    first = statements[0] if statements else None
    # `isinstance` rather than `is not None`: sqlglot yields `None` for an empty statement
    # and this also narrows the parser's loose root type for the checks below.
    if not isinstance(first, exp.Expression):
        raise _reject("empty-statement", "Enter a SELECT statement.")
    return first


def _as_read_query(tree: exp.Expression) -> exp.Query:
    """Narrow the parsed root to a read query, or reject it."""
    if not isinstance(tree, _READ_TOP_LEVEL):
        raise _reject(
            "select-only",
            f"Only SELECT statements are allowed; this is a {_kind(tree)} statement.",
        )
    return tree


def _kind(tree: exp.Expr) -> str:
    """A short, non-leaky name for the statement type, for the refusal message."""
    if isinstance(tree, exp.Command):
        key = str(tree.this or "").strip().upper()
        return key or "non-SELECT"
    # TruncateTable -> "TRUNCATE TABLE": the analyst reads this, not a class name.
    words = re.findall(r"[A-Z][a-z0-9]*", type(tree).__name__) or [type(tree).__name__]
    return " ".join(word.upper() for word in words)


def _check_no_writes(tree: exp.Expr) -> None:
    """DML / DDL / administrative commands anywhere in the tree, CTE bodies included."""
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            raise _reject(
                "dml-or-ddl",
                f"A {_kind(node)} appears inside the statement. The explorer is read-only, "
                "including inside CTEs and subqueries.",
            )
        if isinstance(node, exp.Into):
            raise _reject(
                "select-into",
                "SELECT ... INTO writes its result somewhere. Remove the INTO clause.",
            )
        if isinstance(node, exp.Lock):
            raise _reject(
                "locking-clause",
                "Locking clauses (FOR UPDATE / FOR SHARE) are not allowed on a read.",
            )


def _cte_aliases(tree: exp.Expr) -> frozenset[str]:
    """Names introduced by ``WITH``. They are query aliases, not tables to allowlist."""
    return frozenset(cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE))


def _collect_tables(tree: exp.Expr, allowed: frozenset[str]) -> tuple[str, ...]:
    """Every real table in the tree, rejecting anything outside the allowlist."""
    aliases = _cte_aliases(tree)
    found: list[str] = []
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if table.db or table.catalog:
            raise _reject(
                "schema-qualified",
                "Schema- or catalog-qualified names are not allowed. Reference an "
                "allowlisted table by its plain name.",
            )
        if name in aliases:
            continue
        if name in _SYSTEM_OBJECTS or name not in allowed:
            raise _reject(
                "table-not-allowed",
                f"'{table.name}' is not one of the tables the explorer may read. "
                "Open the data catalog for the readable tables.",
            )
        if name not in found:
            found.append(name)
    return tuple(found)


def _callable_names(node: exp.Expr) -> tuple[str, ...]:
    """Name-like tokens of a node, for the banned-name check."""
    if isinstance(node, exp.Anonymous):
        return (str(node.this).lower(),)
    if isinstance(node, exp.Dot):
        parts = [str(part).lower() for part in node.sql().replace('"', "").split(".")]
        return (".".join(parts), *parts)
    if isinstance(node, exp.Identifier):
        return (str(node.this).lower(),)
    if isinstance(node, exp.Func):
        name = node.sql_name()
        return (str(name).lower(),)
    return ()


def _check_banned_names(tree: exp.Expr) -> None:
    for node in tree.walk():
        for name in _callable_names(node):
            base = name.split("(")[0].strip()
            if base in _BANNED_NAMES or base.split(".")[0] in _BANNED_NAMES:
                raise _reject(
                    "banned-function",
                    f"'{base}' is not available in the explorer.",
                )


def _check_shape(tree: exp.Expr) -> None:
    joins = sum(1 for _ in tree.find_all(exp.Join))
    if joins > MAX_JOINS:
        raise _reject(
            "too-many-joins",
            f"The statement has {joins} joins; the limit is {MAX_JOINS}. Split it up.",
        )
    depth = _max_scope_depth(tree)
    if depth > MAX_QUERY_DEPTH:
        raise _reject(
            "too-deep",
            f"The statement nests {depth} query levels; the limit is {MAX_QUERY_DEPTH}.",
        )


def _max_scope_depth(tree: exp.Expr) -> int:
    """Deepest chain of nested query scopes. Iterative: a payload must not blow the stack."""
    deepest = 0
    for node in tree.walk():
        depth = 0
        current: exp.Expr | None = node
        while current is not None:
            if isinstance(current, _DEPTH_NODES):
                depth += 1
            current = current.parent if isinstance(current, exp.Expression) else None
        deepest = max(deepest, depth)
    return deepest


# --------------------------------------------------------------- projections


def _output_selects(tree: exp.Expr) -> tuple[exp.Select, ...]:
    """The SELECTs whose projection reaches the user: the statement, or each union branch."""
    if isinstance(tree, exp.Select):
        return (tree,)
    if isinstance(tree, exp.SetOperation):
        return (*_output_selects(tree.left), *_output_selects(tree.right))
    if isinstance(tree, exp.Subquery) and isinstance(tree.this, exp.Expression):
        return _output_selects(tree.this)
    return ()


def _is_star(projection: exp.Expr) -> bool:
    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
    )


def _check_star_pii(tree: exp.Expr, tables: tuple[str, ...]) -> None:
    """A star projection must never be the reason PII reaches a screen or a CSV."""
    sensitive = sorted(set(tables) & PII_TABLES)
    if not sensitive:
        return
    for select in tree.find_all(exp.Select):
        if any(_is_star(projection) for projection in select.expressions):
            raise _reject(
                "star-with-pii",
                f"SELECT * is not allowed on {', '.join(sensitive)} because the table holds "
                "personal data. Name the columns you need explicitly.",
            )


def _mask_expression(column_name: str) -> exp.Expression:
    """``substr(col, 1, 2) || '***'``, generated from the AST, never from text.

    The dialect decides the rendering (``SUBSTRING`` on SQLite, ``SUBSTR`` on Oracle);
    the AST itself is dialect-neutral.
    """
    return exp.DPipe(
        this=exp.Substring(
            this=exp.column(column_name),
            start=exp.Literal.number(1),
            length=exp.Literal.number(2),
        ),
        expression=exp.Literal.string("***"),
    )


def _rewrite_projections(
    tree: exp.Query, config: GuardConfig
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Mask PII columns in place and report (selected columns, masked columns)."""
    columns: list[str] = []
    masked: list[str] = []
    for select in _output_selects(tree):
        rewritten: list[exp.Expr] = []
        for projection in select.expressions:
            if _is_star(projection):
                columns.append("*")
                rewritten.append(projection)
                continue
            name = str(projection.alias_or_name or "")
            # An aggregate has no column name of its own; report its shape instead so the
            # audit row and the UI header still say what was selected.
            label = name if name and name != "*" else projection.sql(dialect=config.dialect)
            if label not in columns:
                columns.append(label)
            target = projection.this if isinstance(projection, exp.Alias) else projection
            is_pii = isinstance(target, exp.Column) and target.name.lower() in PII_COLUMNS
            if is_pii and not config.allow_pii_columns:
                source = target.name
                if source not in masked:
                    masked.append(source)
                rewritten.append(exp.alias_(_mask_expression(source), name or source))
            else:
                rewritten.append(projection)
        select.set("expressions", rewritten)
    return tuple(columns), tuple(masked)


# ------------------------------------------------------------------ row cap


def _declared_limit(tree: exp.Query) -> int | None:
    """The statement's own row limit, if it declared a plain numeric one."""
    node = tree.args.get("limit")
    if node is None:
        return None
    count: exp.Expr | None
    if isinstance(node, exp.Fetch):
        options = node.args.get("limit_options")
        # FETCH FIRST n PERCENT / WITH TIES is not a row count: re-cap it.
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            return None
        count = node.args.get("count")
    else:
        count = node.expression
    if isinstance(count, exp.Literal) and not count.is_string:
        try:
            return int(str(count.this))
        except ValueError:  # pragma: no cover - sqlglot guarantees a numeric literal here
            return None
    return None


def _apply_row_cap(tree: exp.Query, config: GuardConfig) -> tuple[exp.Query, int, str | None]:
    """Enforce ``max_rows``: add a limit, or lower one that is too generous."""
    declared = _declared_limit(tree)
    if declared is not None and 0 < declared <= config.max_rows:
        return tree, declared, None
    capped = tree.limit(config.max_rows)
    note = (
        f"Row limit lowered from {declared} to the explorer cap of {config.max_rows}."
        if declared is not None
        else f"A row limit of {config.max_rows} was added."
    )
    return capped, config.max_rows, note


# ------------------------------------------------------------ full-scan hint


def _is_time_column(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TIME_COLUMNS or lowered.endswith(_TIME_SUFFIXES)


def _full_scan_tables(tree: exp.Expr) -> tuple[str, ...]:
    """Large tables read without a WHERE clause or without any time predicate."""
    flagged: list[str] = []
    for select in tree.find_all(exp.Select):
        local = sorted(
            {
                table.name.lower()
                for table in select.find_all(exp.Table)
                if table.name.lower() in LARGE_TABLES
            },
        )
        if not local:
            continue
        where = select.args.get("where")
        bounded = isinstance(where, exp.Expression) and any(
            _is_time_column(column.name) for column in where.find_all(exp.Column)
        )
        if not bounded:
            flagged.extend(name for name in local if name not in flagged)
    return tuple(flagged)


# -------------------------------------------------------------- public surface


def guard(sql: str, config: GuardConfig) -> GuardedQuery:
    """Validate and rewrite ``sql``, or raise :class:`QueryRejected` naming the rule.

    The returned :attr:`GuardedQuery.sql` is generated from the validated AST and is the
    only statement the caller may execute.
    """
    original = sql
    _check_text(sql)
    parsed = _parse_one(sql, config.dialect)
    query = _as_read_query(parsed)
    _check_no_writes(query)
    _check_shape(query)
    _check_banned_names(query)
    tables = _collect_tables(query, config.allowed_tables)
    _check_star_pii(query, tables)

    # Copy before rewriting: the guard must not mutate anything the caller still holds.
    tree = query.copy()
    columns, masked = _rewrite_projections(tree, config)
    tree, row_limit, cap_note = _apply_row_cap(tree, config)

    warnings: list[str] = []
    if cap_note:
        warnings.append(cap_note)
    scans = _full_scan_tables(tree)
    if scans:
        warnings.append(
            "No time bound on " + ", ".join(scans) + ": this may scan the whole table. "
            "Add a predicate on a time column.",
        )
    if masked:
        warnings.append("Masked personal-data columns: " + ", ".join(masked) + ".")
    if config.require_read_only:
        warnings.append("Live mode: this query must run on the read-only connection.")

    try:
        rendered = tree.sql(dialect=config.dialect)
    except Exception as err:  # a statement we cannot render is refused, not executed
        raise _reject("unparseable", "The statement could not be rewritten safely.") from err
    if not rendered.strip():  # pragma: no cover - sqlglot always renders a parsed query
        raise _reject("unparseable", "The statement could not be rewritten safely.")

    return GuardedQuery(
        sql=rendered,
        original_sql=original,
        tables=tables,
        columns=columns,
        masked_columns=masked,
        row_limit=row_limit,
        estimated_full_scan=bool(scans),
        warnings=tuple(warnings),
    )


def rejection_reason(sql: str, config: GuardConfig) -> str | None:
    """``None`` when the query is accepted, otherwise the analyst-facing refusal."""
    try:
        guard(sql, config)
    except QueryRejected as rejected:
        return rejected.detail
    return None
