"""SQLAlchemy adapters for the recovery ports.

`SqlRunbookRepository` reads the seeded `runbook` table; `FileRunbookRepository` (in the
module) reads `config/runbooks.yaml`. Both satisfy the same port, and which one the
composition root wires is a deployment decision: the file is the reviewed source of truth,
the table is what the seed and the UI already query.

Neither this module nor the port it satisfies can execute a runbook. There is no write path
to the network here — only to `recovery_action`, which records proposals, approvals and
simulations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import RecoveryAction
from tazama.infrastructure.db.models import Runbook as RunbookRow
from tazama.modules.recovery.models import (
    KpiExpectation,
    RecoveryProposal,
    RecoveryStatus,
    RiskTier,
    Runbook,
    RunbookStep,
)


class SqlRunbookRepository:
    """Reads `runbook`. Read-only by design: a runbook is a reviewed document."""

    __slots__ = ("_engine", "_session")

    def __init__(self, engine: Engine | None = None, *, session: Session | None = None) -> None:
        self._engine = engine or get_engine()
        self._session = session

    def get(self, runbook_id: str) -> Runbook | None:
        row = self._first(select(RunbookRow).where(RunbookRow.id == runbook_id))
        return None if row is None else _to_runbook(row)

    def for_cause(self, cause: str) -> tuple[Runbook, ...]:
        rows = self._all(
            select(RunbookRow).where(RunbookRow.cause == cause).order_by(RunbookRow.id),
        )
        return tuple(_to_runbook(row) for row in rows)

    def all(self) -> tuple[Runbook, ...]:
        return tuple(
            _to_runbook(row) for row in self._all(select(RunbookRow).order_by(RunbookRow.id))
        )

    def _first(self, statement: Any) -> RunbookRow | None:
        if self._session is not None:
            return self._session.execute(statement).scalars().first()
        with session_factory(self._engine)() as session:
            return session.execute(statement).scalars().first()

    def _all(self, statement: Any) -> list[RunbookRow]:
        if self._session is not None:
            return list(self._session.execute(statement).scalars().all())
        with session_factory(self._engine)() as session:
            return list(session.execute(statement).scalars().all())


class SqlRecoveryRepository:
    """Reads and writes `recovery_action`."""

    __slots__ = ("_engine", "_session")

    def __init__(self, engine: Engine | None = None, *, session: Session | None = None) -> None:
        self._engine = engine or get_engine()
        self._session = session

    def save(self, proposal: RecoveryProposal) -> RecoveryProposal:
        """Insert or update by primary key, dialect-neutrally (see notification_repo)."""
        row = proposal.to_row()
        exists = (
            self._count(
                select(func.count())
                .select_from(RecoveryAction)
                .where(RecoveryAction.id == proposal.id),
            )
            > 0
        )
        if exists:
            updatable = {key: value for key, value in row.items() if key != "id"}
            self._execute(
                update(RecoveryAction).where(RecoveryAction.id == proposal.id).values(**updatable),
            )
        else:
            self._execute(insert(RecoveryAction).values(**row))
        return proposal

    def get(self, action_id: str) -> RecoveryProposal | None:
        row = self._first(select(RecoveryAction).where(RecoveryAction.id == action_id))
        return None if row is None else _to_proposal(row)

    def for_incident(self, incident_id: str) -> tuple[RecoveryProposal, ...]:
        rows = self._all(
            select(RecoveryAction)
            .where(RecoveryAction.incident_id == incident_id)
            .order_by(RecoveryAction.proposed_at, RecoveryAction.id),
        )
        return tuple(_to_proposal(row) for row in rows)

    def _execute(self, statement: Any) -> None:
        if self._session is not None:
            self._session.execute(statement)
            return
        with session_factory(self._engine)() as session, session.begin():
            session.execute(statement)

    def _first(self, statement: Any) -> RecoveryAction | None:
        if self._session is not None:
            return self._session.execute(statement).scalars().first()
        with session_factory(self._engine)() as session:
            return session.execute(statement).scalars().first()

    def _all(self, statement: Any) -> list[RecoveryAction]:
        if self._session is not None:
            return list(self._session.execute(statement).scalars().all())
        with session_factory(self._engine)() as session:
            return list(session.execute(statement).scalars().all())

    def _count(self, statement: Any) -> int:
        if self._session is not None:
            return int(self._session.execute(statement).scalar_one())
        with session_factory(self._engine)() as session:
            return int(session.execute(statement).scalar_one())


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _steps(raw: Any) -> tuple[RunbookStep, ...]:
    """The seeded column holds either `{n, action, tier}` objects or plain strings."""
    steps: list[RunbookStep] = []
    for position, item in enumerate(raw or (), start=1):
        if isinstance(item, dict):
            steps.append(
                RunbookStep(
                    n=int(item.get("n", position)),
                    action=str(item.get("action", "")),
                    tier=str(item.get("tier", "propose")),
                ),
            )
        else:
            steps.append(RunbookStep(n=position, action=str(item)))
    return tuple(steps)


def _to_runbook(row: RunbookRow) -> Runbook:
    return Runbook(
        id=row.id,
        title=row.title,
        cause=row.cause,
        risk_tier=RiskTier(row.risk_tier),
        steps=_steps(row.steps),
        rollback=_steps(row.rollback),
        expected_effect=row.expected_effect or "",
        requires_approval=bool(row.requires_approval),
        owner=row.owner or "unassigned",
        # The table carries no estimate or KPI expectations; the YAML catalogue does. A
        # simulation built from a table row therefore states the default rather than
        # inventing a number.
        estimated_minutes=30,
        expected_kpis=(),
    )


def _to_proposal(row: RecoveryAction) -> RecoveryProposal:
    outcome = dict(row.simulated_outcome) if row.simulated_outcome else None
    proposed_at = _aware(row.proposed_at)
    assert proposed_at is not None  # NOT NULL in the schema
    status = RecoveryStatus(row.status)
    risk_tier = RiskTier(row.risk_tier)
    # The table keeps one `approved_by` (the last approver), which is what the UI shows.
    # The full approval trail is in the append-only audit log, so a round-trip rebuilds the
    # count from the risk tier rather than pretending to know every name.
    approvals = (row.approved_by,) if row.approved_by else ()
    return RecoveryProposal(
        id=row.id,
        incident_id=row.incident_id,
        runbook_id=row.runbook_id,
        status=status,
        risk_tier=risk_tier,
        proposed_by=row.proposed_by,
        proposed_at=proposed_at,
        requires_approval=True,
        required_approvals=2 if risk_tier is RiskTier.HIGH else 1,
        approvals=approvals,
        approved_at=_aware(row.approved_at),
        rejected_reason=row.rejected_reason,
        rollback_of=row.rollback_of,
        simulated_outcome=outcome,
    )


def expectations_from(raw: Any) -> tuple[KpiExpectation, ...]:
    """Rebuild KPI expectations from a stored simulation, for the incident detail view."""
    return tuple(
        KpiExpectation(
            kpi=str(item.get("kpi", "")),
            direction=str(item.get("direction", "improve")),
            target=None if item.get("target") is None else float(item["target"]),
            note=str(item.get("note", "")),
        )
        for item in (raw or ())
        if isinstance(item, dict)
    )
