"""Proactive customer notifications: propose, approve, reject, send.

Four rules are visible in the shape of this router, not just in the service:
  * a message can only come from an approved template, so the request carries a template
    id and slot values, never text;
  * proposing and approving are different capabilities, held by different roles;
  * sending is a separate call that re-checks policy, because an approval can be hours old;
  * suppressions are returned, not hidden — "we deliberately did not message these people"
    is information a manager needs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from tazama.api.deps import (
    AuditDep,
    NotificationDep,
    PrincipalDep,
    SessionDep,
    client_ip,
    requires,
)
from tazama.core.errors import NotFound
from tazama.infrastructure.db.models import Incident, Notification
from tazama.modules.notification.models import IncidentBrief

router = APIRouter(prefix="/notifications", tags=["notifications"])

MAX_SLOT_LEN = 60
MAX_SLOTS = 8


class ProposeIn(BaseModel):
    """Slot *values* only. There is no field here for message text, by design."""

    incident_id: Annotated[str, Field(min_length=1, max_length=40)]
    template_id: Annotated[str, Field(min_length=1, max_length=32)]
    slots: Annotated[dict[str, str], Field(max_length=MAX_SLOTS)]
    limit: Annotated[int, Field(default=500, ge=1, le=5000)] = 500


class RejectIn(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=200)]


class ProposalOut(BaseModel):
    id: str
    incident_id: str
    subscriber_id: str
    template_id: str
    template_version: str
    language: str
    channel: str
    status: str
    rendered_text: str
    parts: int
    proposed_at: str
    proposed_by: str
    approved_by: str | None = None
    approved_at: str | None = None
    rejected_reason: str | None = None
    sent_at: str | None = None
    suppressed_reason: str | None = None


class SuppressionOut(BaseModel):
    subscriber_id: str | None
    reason: str
    detail: str = ""


class BatchOut(BaseModel):
    proposals: list[ProposalOut]
    suppressions: list[SuppressionOut]
    storm_guard_tripped: bool
    proposed: int
    suppressed: int


def _proposal_out(proposal: object) -> ProposalOut:
    get = proposal.__getattribute__
    return ProposalOut(
        id=get("id"),
        incident_id=get("incident_id"),
        subscriber_id=get("subscriber_id"),
        template_id=get("template_id"),
        template_version=get("template_version"),
        language=get("language"),
        channel=get("channel"),
        status=str(get("status")),
        rendered_text=get("rendered_text"),
        parts=get("parts"),
        proposed_at=get("proposed_at").isoformat(),
        proposed_by=get("proposed_by"),
        approved_by=get("approved_by"),
        approved_at=_iso(get("approved_at")),
        rejected_reason=get("rejected_reason"),
        sent_at=_iso(get("sent_at")),
        suppressed_reason=_opt_str(get("suppressed_reason")),
    )


def _iso(value: object) -> str | None:
    return None if value is None else str(value.isoformat())  # type: ignore[attr-defined]


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _incident_brief(session: SessionDep, incident_id: str) -> IncidentBrief:
    """Build the brief from the incident row.

    Notification never reaches into another module for this: the caller passes the facts,
    and here the caller is the API, which reads its own database.
    """
    row = session.get(Incident, incident_id)
    if row is None:
        raise NotFound("Incident not found.")
    from tazama.infrastructure.db.models import IncidentLink

    elements = (
        session.execute(
            select(IncidentLink.entity_id)
            .where(IncidentLink.incident_id == incident_id)
            .where(IncidentLink.entity_type == "cell")
            .limit(200),
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
    )


@router.post(
    "",
    response_model=BatchOut,
    status_code=201,
    dependencies=[Depends(requires("notification:propose"))],
    summary="Propose notifications for an incident (nothing is sent)",
)
async def propose(
    body: ProposeIn,
    service: NotificationDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> BatchOut:
    brief = _incident_brief(session, body.incident_id)
    batch = service.propose(
        brief,
        template_id=body.template_id,
        slots=body.slots,
        principal=principal,
    )
    return BatchOut(
        proposals=[_proposal_out(p) for p in batch.proposals],
        suppressions=[
            SuppressionOut(
                subscriber_id=getattr(s, "subscriber_id", None),
                reason=str(getattr(s, "reason", "")),
                detail=str(getattr(s, "detail", "")),
            )
            for s in batch.suppressions
        ],
        storm_guard_tripped=batch.storm_guard_tripped,
        proposed=len(batch.proposals),
        suppressed=len(batch.suppressions),
    )


@router.post(
    "/{notification_id}/approve",
    response_model=ProposalOut,
    dependencies=[Depends(requires("notification:approve"))],
    summary="Approve a proposed notification",
)
async def approve(
    notification_id: str,
    service: NotificationDep,
    principal: PrincipalDep,
    audit: AuditDep,
    request_ip: Annotated[str, Depends(client_ip)],
) -> ProposalOut:
    proposal = service.approve(notification_id, principal)
    audit.record_human_action(
        principal=principal,
        action="approve_notification",
        entity_type="notification",
        entity_id=notification_id,
        outcome="approved",
        inputs={"template_id": proposal.template_id, "incident_id": proposal.incident_id},
        client_ip=request_ip,
    )
    return _proposal_out(proposal)


@router.post(
    "/{notification_id}/reject",
    response_model=ProposalOut,
    dependencies=[Depends(requires("notification:approve"))],
    summary="Reject a proposed notification",
)
async def reject(
    notification_id: str,
    body: RejectIn,
    service: NotificationDep,
    principal: PrincipalDep,
    audit: AuditDep,
    request_ip: Annotated[str, Depends(client_ip)],
) -> ProposalOut:
    proposal = service.reject(notification_id, principal, body.reason)
    audit.record_human_action(
        principal=principal,
        action="reject_notification",
        entity_type="notification",
        entity_id=notification_id,
        outcome="rejected",
        inputs={"reason": body.reason},
        client_ip=request_ip,
    )
    return _proposal_out(proposal)


@router.post(
    "/{notification_id}/send",
    response_model=ProposalOut,
    dependencies=[Depends(requires("notification:approve"))],
    summary="Send an approved notification through the SMS gateway (mock in v1)",
)
async def send(
    notification_id: str,
    service: NotificationDep,
    principal: PrincipalDep,
    audit: AuditDep,
    request_ip: Annotated[str, Depends(client_ip)],
) -> ProposalOut:
    proposal = service.send_approved(notification_id)
    audit.record_human_action(
        principal=principal,
        action="send_notification",
        entity_type="notification",
        entity_id=notification_id,
        outcome=str(proposal.status),
        inputs={"gateway_mode": proposal.gateway_mode},
        client_ip=request_ip,
    )
    return _proposal_out(proposal)


class NotificationRowOut(BaseModel):
    id: str
    incident_id: str | None
    subscriber_id: str | None
    template_id: str
    status: str
    language: str
    channel: str
    proposed_at: str
    approved_by: str | None
    sent_at: str | None
    suppressed_reason: str | None
    text_preview: str


@router.get(
    "",
    response_model=list[NotificationRowOut],
    dependencies=[Depends(requires("incident:read"))],
    summary="Notifications, optionally for one incident",
)
async def list_notifications(
    session: SessionDep,
    incident_id: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[NotificationRowOut]:
    stmt = select(Notification).order_by(Notification.proposed_at.desc()).limit(limit)
    if incident_id:
        stmt = stmt.where(Notification.incident_id == incident_id)
    if status:
        stmt = stmt.where(Notification.status == status)
    rows = session.execute(stmt).scalars().all()
    return [
        NotificationRowOut(
            id=row.id,
            incident_id=row.incident_id,
            subscriber_id=row.subscriber_id,
            template_id=row.template_id,
            status=row.status,
            language=row.language,
            channel=row.channel,
            proposed_at=row.proposed_at.isoformat(),
            approved_by=row.approved_by,
            sent_at=row.sent_at.isoformat() if row.sent_at else None,
            suppressed_reason=row.suppressed_reason,
            text_preview=(row.rendered_text or "")[:120],
        )
        for row in rows
    ]
