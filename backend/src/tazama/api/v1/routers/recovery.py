"""Recovery recommendations: propose, simulate, approve, reject, roll back.

Nothing here executes anything. There is no endpoint that touches the network, and the
service exposes no method that could — a test asserts that. What this router offers is a
recommendation with its evidence, a *modelled* outcome, and an approval trail.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from tazama.api.deps import (
    AuditDep,
    PrincipalDep,
    RecoveryDep,
    SessionDep,
    client_ip,
    requires,
)
from tazama.core.errors import NotFound
from tazama.infrastructure.db.models import Incident, IncidentLink, Runbook
from tazama.modules.recovery.models import IncidentBrief

router = APIRouter(prefix="/recovery", tags=["recovery"])


class RecommendIn(BaseModel):
    incident_id: Annotated[str, Field(min_length=1, max_length=40)]
    #: An optional hint from a diagnosis. It narrows the runbook choice; it never bypasses
    #: the cause the rules derived.
    diagnosis_hint: Annotated[str | None, Field(default=None, max_length=40)] = None


class RejectIn(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=200)]


class ProposalOut(BaseModel):
    id: str
    incident_id: str
    runbook_id: str
    status: str
    risk_tier: str
    rationale: str
    requires_approval: bool
    required_approvals: int
    approvals: list[str]
    approved_at: str | None
    rejected_reason: str | None
    rollback_of: str | None
    affected_elements: list[str]
    simulated_outcome: dict[str, Any] | None
    #: Stated on every response so a reader cannot mistake this for something that ran.
    executed: bool = False


class SimulationOut(BaseModel):
    runbook_id: str
    expected_effect: str
    estimated_minutes: int
    confidence: float
    affected_elements: list[str]
    expected_kpis: list[dict[str, Any]]
    rollback_plan: list[dict[str, Any]]
    caveats: list[str]
    simulation: bool = True
    executed: bool = False


class RunbookOut(BaseModel):
    id: str
    title: str
    cause: str
    risk_tier: str
    requires_approval: bool
    owner: str | None
    steps: list[dict[str, Any]]
    rollback: list[dict[str, Any]]
    expected_effect: str | None


def _proposal_out(proposal: Any) -> ProposalOut:
    return ProposalOut(
        id=proposal.id,
        incident_id=proposal.incident_id,
        runbook_id=proposal.runbook_id,
        status=str(proposal.status),
        risk_tier=str(proposal.risk_tier),
        rationale=proposal.rationale,
        requires_approval=proposal.requires_approval,
        required_approvals=proposal.required_approvals,
        approvals=list(proposal.approvals),
        approved_at=proposal.approved_at.isoformat() if proposal.approved_at else None,
        rejected_reason=proposal.rejected_reason,
        rollback_of=proposal.rollback_of,
        affected_elements=list(proposal.affected_elements),
        simulated_outcome=dict(proposal.simulated_outcome or {}) or None,
    )


def _brief(session: SessionDep, incident_id: str) -> IncidentBrief:
    row = session.get(Incident, incident_id)
    if row is None:
        raise NotFound("Incident not found.")
    elements = (
        session.execute(
            select(IncidentLink.entity_id)
            .where(IncidentLink.incident_id == incident_id)
            .where(IncidentLink.entity_type.in_(("cell", "site", "node")))
            .limit(500),
        )
        .scalars()
        .all()
    )
    return IncidentBrief(
        id=row.id,
        region=row.region,
        status=row.status,
        cause=row.cause,
        affected_elements=tuple(elements),
        affected_subs=row.affected_subs,
        priority=row.priority,
    )


@router.get(
    "/runbooks",
    response_model=list[RunbookOut],
    dependencies=[Depends(requires("incident:read"))],
    summary="The runbook catalogue",
)
async def list_runbooks(session: SessionDep) -> list[RunbookOut]:
    rows = session.execute(select(Runbook).order_by(Runbook.id)).scalars().all()
    return [
        RunbookOut(
            id=row.id,
            title=row.title,
            cause=row.cause,
            risk_tier=row.risk_tier,
            requires_approval=row.requires_approval,
            owner=row.owner,
            steps=list(row.steps or []),
            rollback=list(row.rollback or []),
            expected_effect=row.expected_effect,
        )
        for row in rows
    ]


@router.post(
    "",
    response_model=ProposalOut,
    status_code=201,
    dependencies=[Depends(requires("recovery:recommend"))],
    summary="Recommend a runbook for an incident (proposal only)",
)
async def recommend(
    body: RecommendIn,
    service: RecoveryDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> ProposalOut:
    proposal = service.recommend(
        _brief(session, body.incident_id),
        body.diagnosis_hint,
        principal=principal,
    )
    return _proposal_out(proposal)


@router.get(
    "/{action_id}/simulation",
    response_model=SimulationOut,
    dependencies=[Depends(requires("incident:read"))],
    summary="Modelled outcome of a proposed recovery (never executed)",
)
async def simulate(action_id: str, service: RecoveryDep) -> SimulationOut:
    outcome = service.simulate(action_id)
    return SimulationOut(
        runbook_id=outcome.runbook_id,
        expected_effect=outcome.expected_effect,
        estimated_minutes=outcome.estimated_minutes,
        confidence=outcome.confidence,
        affected_elements=list(outcome.affected_elements),
        expected_kpis=[
            {
                "kpi": getattr(k, "kpi", None),
                "direction": getattr(k, "direction", None),
                "expected": getattr(k, "expected", None),
            }
            for k in outcome.expected_kpis
        ],
        rollback_plan=[
            {"order": getattr(s, "order", None), "action": getattr(s, "action", None)}
            for s in outcome.rollback_plan
        ],
        caveats=list(outcome.caveats),
        simulation=outcome.simulation,
        executed=outcome.executed,
    )


@router.post(
    "/{action_id}/approve",
    response_model=ProposalOut,
    dependencies=[Depends(requires("recovery:approve"))],
    summary="Approve a recovery proposal",
)
async def approve(
    action_id: str,
    service: RecoveryDep,
    principal: PrincipalDep,
    audit: AuditDep,
    request_ip: Annotated[str, Depends(client_ip)],
) -> ProposalOut:
    proposal = service.approve(action_id, principal)
    audit.record_human_action(
        principal=principal,
        action="approve_recovery",
        entity_type="recovery_action",
        entity_id=action_id,
        outcome=str(proposal.status),
        inputs={
            "runbook_id": proposal.runbook_id,
            "risk_tier": str(proposal.risk_tier),
            "approvals": list(proposal.approvals),
            "required_approvals": proposal.required_approvals,
        },
        client_ip=request_ip,
    )
    return _proposal_out(proposal)


@router.post(
    "/{action_id}/reject",
    response_model=ProposalOut,
    dependencies=[Depends(requires("recovery:approve"))],
    summary="Reject a recovery proposal",
)
async def reject(
    action_id: str,
    body: RejectIn,
    service: RecoveryDep,
    principal: PrincipalDep,
    audit: AuditDep,
    request_ip: Annotated[str, Depends(client_ip)],
) -> ProposalOut:
    proposal = service.reject(action_id, principal, body.reason)
    audit.record_human_action(
        principal=principal,
        action="reject_recovery",
        entity_type="recovery_action",
        entity_id=action_id,
        outcome="rejected",
        inputs={"reason": body.reason},
        client_ip=request_ip,
    )
    return _proposal_out(proposal)


@router.post(
    "/{action_id}/rollback",
    response_model=ProposalOut,
    status_code=201,
    dependencies=[Depends(requires("recovery:recommend"))],
    summary="Propose the rollback of a recovery action",
)
async def propose_rollback(
    action_id: str,
    service: RecoveryDep,
    principal: PrincipalDep,
) -> ProposalOut:
    # The service takes the proposal, not its id: a rollback is defined by what it undoes.
    proposal = service.propose_rollback(service.get(action_id), principal=principal)
    return _proposal_out(proposal)


class RecoveryRowOut(BaseModel):
    id: str
    incident_id: str
    runbook_id: str
    status: str
    risk_tier: str
    proposed_by: str
    proposed_at: str
    approved_by: str | None
    rollback_of: str | None


@router.get(
    "",
    response_model=list[RecoveryRowOut],
    dependencies=[Depends(requires("incident:read"))],
    summary="Recovery proposals, optionally for one incident",
)
async def list_actions(
    session: SessionDep,
    incident_id: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[RecoveryRowOut]:
    from tazama.infrastructure.db.models import RecoveryAction

    stmt = select(RecoveryAction).order_by(RecoveryAction.proposed_at.desc()).limit(limit)
    if incident_id:
        stmt = stmt.where(RecoveryAction.incident_id == incident_id)
    rows = session.execute(stmt).scalars().all()
    return [
        RecoveryRowOut(
            id=row.id,
            incident_id=row.incident_id,
            runbook_id=row.runbook_id,
            status=row.status,
            risk_tier=row.risk_tier,
            proposed_by=row.proposed_by,
            proposed_at=row.proposed_at.isoformat(),
            approved_by=row.approved_by,
            rollback_of=row.rollback_of,
        )
        for row in rows
    ]
