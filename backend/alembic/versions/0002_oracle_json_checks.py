"""Oracle-only IS JSON check constraints on the JSON text columns.

Revision ID: 0002
Revises: 0001
Created: 2026-10-21

The portable schema stores JSON as text (CLOB on Oracle) so one set of DDL works on both
backends. On Oracle we additionally constrain those columns with `IS JSON`, which is what
makes the column queryable with JSON path expressions and stops malformed payloads at the
database rather than at read time. SQLite has no equivalent, so this revision is a no-op
there — the application-side codec in `infrastructure/db/types.JsonText` validates on both.

Reversibility: downgrade drops the constraints it created.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column) pairs holding JSON documents. Constraint names are kept short so they
#: stay inside Oracle's 30-character identifier limit.
JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("alarm", "details"),
    ("diagnosis", "evidence"),
    ("case_record", "decision_inputs"),
    ("runbook", "steps"),
    ("runbook", "rollback"),
    ("recovery_action", "simulated_outcome"),
    ("verification", "detail"),
    ("notification", "slots"),
    ("knowledge_doc", "tags"),
    ("knowledge_doc", "embedding"),
    ("prediction", "features"),
    ("model_registry", "metrics"),
    ("model_registry", "params"),
    ("model_registry", "feature_list"),
    ("eval_run", "metrics"),
    ("eval_run", "thresholds"),
    ("eval_run", "prompt_versions"),
    ("audit_log", "inputs"),
    ("audit_log", "evidence"),
    ("outbox_event", "payload"),
    ("report_delivery", "card"),
    ("ground_truth", "detail"),
    ("seed_run", "row_counts"),
)


def _constraint_name(table: str, column: str) -> str:
    """`ck_<abbreviated table>_<column>_json`, truncated to 30 characters."""
    base = f"ck_{table[:8]}_{column[:10]}_json"
    return base[:30]


def upgrade() -> None:
    if op.get_bind().dialect.name != "oracle":
        return
    for table, column in JSON_COLUMNS:
        op.create_check_constraint(
            _constraint_name(table, column),
            table,
            f"{column} is json",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "oracle":
        return
    for table, column in JSON_COLUMNS:
        op.drop_constraint(_constraint_name(table, column), table, type_="check")
