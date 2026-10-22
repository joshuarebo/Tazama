"""The audit trail, read-only.

Append-only means there is no write endpoint here at all, and no delete: rows arrive
through the modules that make decisions. Reading is restricted to `audit:read`
(admin and manager), because a decision log names people and their actions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from tazama.api.deps import AuditDep, requires

router = APIRouter(prefix="/audit", tags=["audit"])


class EvidenceOut(BaseModel):
    kind: str
    ref: str
    note: str = ""
    weight: float = 1.0


class DecisionOut(BaseModel):
    occurred_at: str
    actor: str
    actor_role: str | None
    action: str
    entity_type: str
    entity_id: str
    outcome: str
    confidence: float | None
    rule_version: str | None
    model_version: str | None
    prompt_version: str | None
    correlation_id: str | None
    inputs: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceOut] = Field(default_factory=list)


def _out(decision: Any) -> DecisionOut:
    return DecisionOut(
        occurred_at=decision.occurred_at.isoformat(),
        actor=decision.actor,
        actor_role=decision.actor_role,
        action=decision.action,
        entity_type=decision.entity_type,
        entity_id=decision.entity_id,
        outcome=decision.outcome,
        confidence=decision.confidence,
        rule_version=decision.rule_version,
        model_version=decision.model_version,
        prompt_version=decision.prompt_version,
        correlation_id=decision.correlation_id,
        inputs=dict(decision.inputs or {}),
        evidence=[
            EvidenceOut(kind=e.kind, ref=e.ref, note=e.note, weight=e.weight)
            for e in decision.evidence
        ],
    )


@router.get(
    "",
    response_model=list[DecisionOut],
    dependencies=[Depends(requires("audit:read"))],
    summary="Recent decisions and actions",
)
async def recent(
    audit: AuditDep,
    since: datetime | None = None,
    action: Annotated[str | None, Query(max_length=48)] = None,
    actor: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DecisionOut]:
    return [
        _out(decision)
        for decision in audit.recent(since=since, action=action, actor=actor, limit=limit)
    ]


@router.get(
    "/{entity_type}/{entity_id}",
    response_model=list[DecisionOut],
    dependencies=[Depends(requires("audit:read"))],
    summary="Everything that happened to one entity, newest first",
)
async def for_entity(
    entity_type: Annotated[str, Field(max_length=32)],
    entity_id: Annotated[str, Field(max_length=40)],
    audit: AuditDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DecisionOut]:
    return [_out(d) for d in audit.history(entity_type, entity_id, limit=limit)]
