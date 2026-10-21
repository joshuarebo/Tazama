"""Append-only audit storage.

There is no update or delete path here, by design: the only way an audit row changes is a
new row. `AuditLog` has no version column for the same reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import AuditLog
from tazama.modules.audit.models import Decision, Evidence

MAX_LIMIT = 500


class SqlAuditSink:
    """Writes to `audit_log`.

    A session can be injected so an audit row commits in the same transaction as the state
    change it describes; without one, each append is its own transaction (the right choice
    for API-level actions, which must be recorded even if the following work fails).
    """

    __slots__ = ("_engine", "_session")

    def __init__(self, engine: Engine | None = None, *, session: Session | None = None) -> None:
        self._engine = engine or get_engine()
        self._session = session

    def append(self, decision: Decision) -> None:
        row = decision.to_row()
        statement = insert(AuditLog).values(**row)
        if self._session is not None:
            self._session.execute(statement)
            return
        with session_factory(self._engine)() as session, session.begin():
            session.execute(statement)

    def for_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[Decision, ...]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(_bounded(limit))
        )
        return self._fetch(stmt)

    def recent(
        self,
        *,
        since: datetime | None = None,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
    ) -> tuple[Decision, ...]:
        stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        if since is not None:
            stmt = stmt.where(AuditLog.occurred_at >= since)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if actor is not None:
            stmt = stmt.where(AuditLog.actor == actor)
        return self._fetch(stmt.limit(_bounded(limit)))

    def _fetch(self, stmt: Any) -> tuple[Decision, ...]:
        if self._session is not None:
            rows = self._session.execute(stmt).scalars().all()
        else:
            with session_factory(self._engine)() as session:
                rows = session.execute(stmt).scalars().all()
        return tuple(_to_decision(row) for row in rows)


def _bounded(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _to_decision(row: AuditLog) -> Decision:
    return Decision(
        actor=row.actor,
        actor_role=row.actor_role,
        action=row.action,
        entity_type=row.entity_type or "",
        entity_id=row.entity_id or "",
        outcome=row.outcome,
        occurred_at=row.occurred_at
        if row.occurred_at.tzinfo
        else row.occurred_at.replace(tzinfo=UTC),
        inputs=dict(row.inputs or {}),
        evidence=tuple(
            Evidence(
                kind=str(item.get("kind", "")),
                ref=str(item.get("ref", "")),
                note=str(item.get("note", "")),
                weight=float(item.get("weight", 1.0)),
            )
            for item in (row.evidence or [])
        ),
        confidence=row.confidence,
        rule_version=row.rule_version,
        model_version=row.model_version,
        prompt_version=row.prompt_version,
        correlation_id=row.correlation_id,
        client_ip=row.client_ip,
    )
