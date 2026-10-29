"""Care agent lookup: one subscriber, one screen, one honest answer.

This is the endpoint a care agent hits while a customer is on the line, so it answers the
only question that matters in that moment — *is there a known problem where this person
is, and what am I allowed to tell them* — and it answers it from the same incident the NOC
is working, not from a separate story.

Two things about identity here (ADR 0008):

* The agent types an MSISDN. It is normalised, pseudonymised at this boundary, and the raw
  value is never stored, logged or returned. The lookup is a constant-time token match, so
  the endpoint is not a timing oracle for "does this number exist".
* The demo seed deliberately keeps no raw MSISDNs — only the *shape* each one arrived in —
  so a reviewer cannot type a seeded number and find it. `GET /care/samples` therefore
  hands back a few pseudonym tokens to look up directly. That is a demo affordance, not a
  production path: `POST /care/lookup` accepts either, and the number path is the real one.

What the agent may say comes from an approved template, never from free text, and it is
only offered when an incident is actually live on that subscriber's serving cell.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from tazama.api.deps import ClockDep, PseudonymiserDep, SessionDep, requires
from tazama.core.errors import ValidationFailed
from tazama.domain.value_objects import Msisdn
from tazama.infrastructure.db.models import (
    CareCall,
    Cell,
    Complaint,
    Incident,
    IncidentLink,
    Notification,
    Subscriber,
)

router = APIRouter(prefix="/care", tags=["care"])

#: What counts as "recent" contact. Older history is still shown, but flagged as stale.
HISTORY = timedelta(days=30)


class SubscriberOut(BaseModel):
    """Everything the agent sees. No identifier that could be dialled."""

    token: str
    region: str
    segment: str
    is_enterprise: bool
    sla_tier: str | None
    opt_out: bool
    is_roaming: bool
    home_cell_id: str | None
    last_cell_id: str | None
    last_seen_at: str | None
    #: The input shape the number arrived in, kept so normalisation is exercised.
    msisdn_format: str | None


class CellOut(BaseModel):
    cell_id: str
    site_id: str
    name: str
    tech: str
    region: str
    lat: float
    lon: float
    subscribers_est: int


class ComplaintOut(BaseModel):
    id: str
    received_at: str
    channel: str
    category: str | None
    status: str
    incident_id: str | None
    text_preview: str


class CareCallOut(BaseModel):
    id: str
    started_at: str
    duration_s: int
    reason_code: str
    reason_text: str | None


class KnownIssueOut(BaseModel):
    """The banner the agent reads out, and the incident it is grounded in."""

    incident_id: str
    title: str
    priority: str
    status: str
    cause: str | None
    detected_at: str
    affected_subs: int
    #: Approved wording. The agent may read this; they may not improvise.
    say_to_customer: str
    eta_note: str
    notification_sent: bool


class LookupOut(BaseModel):
    found: bool
    subscriber: SubscriberOut | None = None
    serving_cell: CellOut | None = None
    known_issue: KnownIssueOut | None = None
    complaints: list[ComplaintOut] = Field(default_factory=list)
    care_calls: list[CareCallOut] = Field(default_factory=list)
    #: Why the answer is what it is, in the agent's language.
    notes: list[str] = Field(default_factory=list)


class LookupIn(BaseModel):
    #: An MSISDN in any of the shapes Kenyan channels produce, or a pseudonym token.
    identifier: Annotated[str, Field(min_length=3, max_length=64)]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _preview(text: str | None, *, keep: int = 160) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= keep else f"{collapsed[:keep]}…"


@router.get(
    "/samples",
    response_model=list[str],
    dependencies=[Depends(requires("subscriber:lookup"))],
    summary="A few pseudonym tokens to try (demo affordance)",
)
async def sample_tokens(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> list[str]:
    """Tokens of subscribers who have actually complained, so the lookup shows something.

    Ordered by id so the list is stable between calls — a reviewer clicking the same token
    twice should see the same subscriber.
    """
    with_complaints = (
        select(Complaint.subscriber_id)
        .where(Complaint.subscriber_id.is_not(None))
        .group_by(Complaint.subscriber_id)
        .order_by(func.count().desc(), Complaint.subscriber_id)
        .limit(limit)
    )
    rows = session.execute(with_complaints).scalars().all()
    return [row for row in rows if row]


@router.post(
    "/lookup",
    response_model=LookupOut,
    dependencies=[Depends(requires("subscriber:lookup"))],
    summary="Look up one subscriber by MSISDN or pseudonym token",
)
async def lookup(
    body: LookupIn,
    session: SessionDep,
    pseudonymiser: PseudonymiserDep,
    clock: ClockDep,
) -> LookupOut:
    raw = body.identifier.strip()
    notes: list[str] = []

    if raw.startswith("SUB-"):
        token = raw
        notes.append("Looked up by pseudonym token.")
    else:
        try:
            normalised = Msisdn.parse(raw)
        except ValueError as error:
            # The message names the shapes we accept; it never echoes the input back.
            raise ValidationFailed(
                "That is not a recognisable Kenyan MSISDN. Accepted shapes: "
                "+2547XXXXXXXX, 2547XXXXXXXX, 07XXXXXXXX, 01XXXXXXXX.",
            ) from error
        token = pseudonymiser.token(normalised.value)
        notes.append("Number pseudonymised at this boundary; the raw value is not stored.")

    subscriber = session.get(Subscriber, token)
    if subscriber is None:
        notes.append(
            "No subscriber matches. In the demo seed raw numbers are never kept, so a real "
            "number cannot match — use a token from /care/samples.",
        )
        return LookupOut(found=False, notes=notes)

    cell_id = subscriber.last_cell_id or subscriber.home_cell_id
    cell_row = session.get(Cell, cell_id) if cell_id else None
    serving_cell: CellOut | None = None
    if cell_row is not None:
        serving_cell = CellOut(
            cell_id=cell_row.id,
            site_id=cell_row.site_id,
            name=cell_row.name,
            tech=cell_row.tech,
            region=cell_row.region,
            lat=cell_row.lat,
            lon=cell_row.lon,
            subscribers_est=cell_row.subscribers_est or 0,
        )
    else:
        notes.append("No serving cell is known for this subscriber.")

    # Most recent first, with no hard cut-off. An age *filter* would show an agent an empty
    # history whenever the data is older than the window — true of the demo seed, and true
    # of a quiet customer — which reads as "nothing ever happened" rather than "nothing
    # happened lately". Instead everything recent is returned and a note states the age.
    # The clock is injected, never read, so this is the same in a test and in production.
    complaints = (
        session.execute(
            select(Complaint)
            .where(Complaint.subscriber_id == token)
            .order_by(Complaint.received_at.desc())
            .limit(20),
        )
        .scalars()
        .all()
    )
    care_calls = (
        session.execute(
            select(CareCall)
            .where(CareCall.subscriber_id == token)
            .order_by(CareCall.started_at.desc())
            .limit(20),
        )
        .scalars()
        .all()
    )

    newest = max(
        [row.received_at for row in complaints] + [row.started_at for row in care_calls],
        default=None,
    )
    if newest is not None:
        newest = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
        if newest < clock.now() - HISTORY:
            days = (clock.now() - newest).days
            notes.append(
                f"No contact in the last {HISTORY.days} days — the history below is "
                f"{days} days old.",
            )

    known_issue = _known_issue(session, cell_id=cell_id, region=subscriber.region, notes=notes)

    if subscriber.opt_out:
        notes.append(
            "This subscriber has opted out of proactive notifications — do not promise an SMS.",
        )
    if subscriber.is_roaming:
        notes.append("Subscriber is roaming; home-network KPIs may not explain their experience.")
    if subscriber.is_enterprise:
        notes.append(
            "Enterprise account"
            + (f" on the {subscriber.sla_tier} SLA tier" if subscriber.sla_tier else "")
            + " — an SLA clock may be running.",
        )

    return LookupOut(
        found=True,
        subscriber=SubscriberOut(
            token=subscriber.id,
            region=subscriber.region,
            segment=subscriber.segment,
            is_enterprise=subscriber.is_enterprise,
            sla_tier=subscriber.sla_tier,
            opt_out=subscriber.opt_out,
            is_roaming=subscriber.is_roaming,
            home_cell_id=subscriber.home_cell_id,
            last_cell_id=subscriber.last_cell_id,
            last_seen_at=_iso(subscriber.last_seen_at),
            msisdn_format=subscriber.msisdn_format,
        ),
        serving_cell=serving_cell,
        known_issue=known_issue,
        complaints=[
            ComplaintOut(
                id=row.id,
                received_at=_iso(row.received_at) or "",
                channel=row.channel,
                category=row.category,
                status=row.status,
                incident_id=row.incident_id,
                text_preview=_preview(row.raw_text),
            )
            for row in complaints
        ],
        care_calls=[
            CareCallOut(
                id=row.id,
                started_at=_iso(row.started_at) or "",
                duration_s=row.duration_s,
                reason_code=row.reason_code,
                reason_text=row.reason_text,
            )
            for row in care_calls
        ],
        notes=notes,
    )


def _known_issue(
    session: SessionDep,
    *,
    cell_id: str | None,
    region: str,
    notes: list[str],
) -> KnownIssueOut | None:
    """The live incident this subscriber is actually inside, if there is one.

    Cell first, because that is the claim an agent can defend. A region-wide match is
    reported too, but flagged in the notes as weaker: the customer may be fine.
    """
    open_statuses = ("detected", "diagnosing", "diagnosed", "acting", "verifying", "open")

    incident: Any = None
    if cell_id:
        incident = session.execute(
            select(Incident)
            .join(IncidentLink, IncidentLink.incident_id == Incident.id)
            .where(IncidentLink.entity_type == "cell")
            .where(IncidentLink.entity_id == cell_id)
            .where(Incident.resolved_at.is_(None))
            .where(or_(*[Incident.status == status for status in open_statuses]))
            .order_by(Incident.detected_at.desc())
            .limit(1),
        ).scalars().first()

    if incident is None and cell_id:
        notes.append("No live incident on this subscriber's serving cell.")
        incident = session.execute(
            select(Incident)
            .where(Incident.region == region)
            .where(Incident.resolved_at.is_(None))
            .where(or_(*[Incident.status == status for status in open_statuses]))
            .order_by(Incident.detected_at.desc())
            .limit(1),
        ).scalars().first()
        if incident is not None:
            notes.append(
                f"There is a live incident elsewhere in {region}. It may not affect this "
                "customer — do not present it as their fault unless the cell matches.",
            )

    if incident is None:
        return None

    if incident.suppressed_reason:
        notes.append(
            f"That incident is suppressed ({incident.suppressed_reason.replace('_', ' ')}), "
            "so no proactive message goes out for it.",
        )

    sent = session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.incident_id == incident.id)
        .where(Notification.status == "sent"),
    ).scalar_one()

    cause = (incident.cause or "a network fault").replace("_", " ")
    eta = (
        "Engineers are on it; we are not giving a restoration time yet."
        if incident.status in ("detected", "diagnosing")
        else "A fix is being applied and we are verifying recovery."
    )

    return KnownIssueOut(
        incident_id=incident.id,
        title=incident.title,
        priority=incident.priority,
        status=incident.status,
        cause=incident.cause,
        detected_at=_iso(incident.detected_at) or "",
        affected_subs=incident.affected_subs or 0,
        # Approved wording, slot-filled. No free text ever reaches a customer.
        say_to_customer=(
            f"We are aware of a service problem in your area ({cause}) and our engineers are "
            "working on it. We are sorry for the disruption."
        ),
        eta_note=eta,
        notification_sent=bool(sent),
    )
