"""Complaint intake and read endpoints.

Intake is the untrusted edge of the system: the text arrives from a call centre, an app, an
SMS or a social channel, and nothing here interprets it. The handler validates shape,
hands the payload to ingestion (which pseudonymises and deduplicates), optionally resolves
a serving cell, and returns identifiers — never the raw number it was given.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from tazama.api.deps import (
    AuditDep,
    ClockDep,
    IngestionDep,
    PrincipalDep,
    SessionDep,
    SiteIdDep,
    client_ip,
    requires,
)
from tazama.core.errors import NotFound
from tazama.core.logging import scrub_text
from tazama.domain.value_objects import Channel, Language
from tazama.infrastructure.db.models import Complaint
from tazama.modules.ingestion.models import InboundCareCall, InboundComplaint

router = APIRouter(tags=["complaints"])

#: Long enough for a genuinely detailed complaint, short enough that nobody posts a book.
MAX_TEXT_CHARS = 8_000
MAX_PAGE = 200


class ComplaintIn(BaseModel):
    """An inbound complaint. `msisdn` is accepted, pseudonymised and discarded."""

    msisdn: Annotated[str | None, Field(default=None, max_length=32)] = None
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CHARS)]
    channel: Channel
    received_at: datetime | None = None
    source_ts: datetime | None = None
    stated_location: Annotated[str | None, Field(default=None, max_length=120)] = None
    external_ref: Annotated[str | None, Field(default=None, max_length=40)] = None
    language: Language | None = None
    cell_id: Annotated[str | None, Field(default=None, max_length=40)] = None
    region: Annotated[str | None, Field(default=None, max_length=32)] = None
    #: When true, resolve a serving cell before returning. Off by default so intake stays
    #: fast and the pipeline does the work.
    locate: bool = False


class CareCallIn(BaseModel):
    msisdn: Annotated[str | None, Field(default=None, max_length=32)] = None
    reason_code: Annotated[str, Field(min_length=1, max_length=32)]
    started_at: datetime | None = None
    duration_s: Annotated[int, Field(ge=0, le=86_400)] = 0
    reason_text: Annotated[str | None, Field(default=None, max_length=120)] = None
    channel: Channel = Channel.CALL_CENTRE
    agent_ref: Annotated[str | None, Field(default=None, max_length=32)] = None
    region: Annotated[str | None, Field(default=None, max_length=32)] = None
    cell_id: Annotated[str | None, Field(default=None, max_length=40)] = None
    is_repeat: bool = False


class IngestOut(BaseModel):
    complaint_id: str
    subscriber_id: str | None
    duplicate: bool
    needs_human: bool
    is_late: bool
    is_skewed: bool
    text_truncated: bool
    reasons: list[str] = Field(default_factory=list)
    cell_id: str | None = None
    site_id: str | None = None
    site_confidence: float | None = None
    location_method: str | None = None


class CareCallOut(BaseModel):
    care_call_id: str
    subscriber_id: str | None
    duplicate: bool
    needs_human: bool
    reasons: list[str] = Field(default_factory=list)


class ComplaintOut(BaseModel):
    id: str
    received_at: str
    channel: str
    language: str | None
    region: str | None
    cell_id: str | None
    site_id: str | None
    site_confidence: float | None
    category: str | None
    status: str
    incident_id: str | None
    #: The text is masked by default: it is customer-written and may contain anything.
    text_preview: str
    text_length: int


class ComplaintPage(BaseModel):
    items: list[ComplaintOut]
    total: int
    limit: int
    offset: int


def _preview(text: str | None, *, keep: int = 80) -> str:
    if not text:
        return ""
    # Complaint text is whatever the customer typed, and customers type numbers
    # into it constantly. Scrubbed on the way out, never trusted to be clean.
    collapsed = scrub_text(" ".join(text.split()))
    return collapsed if len(collapsed) <= keep else collapsed[: keep - 1] + "…"


def _row_out(row: Complaint) -> ComplaintOut:
    return ComplaintOut(
        id=row.id,
        received_at=row.received_at.isoformat(),
        channel=row.channel,
        language=row.language,
        region=row.region,
        cell_id=row.cell_id,
        site_id=row.site_id,
        site_confidence=row.site_confidence,
        category=row.category,
        status=row.status,
        incident_id=row.incident_id,
        text_preview=_preview(row.raw_text),
        text_length=len(row.raw_text or ""),
    )


@router.post(
    "/complaints",
    response_model=IngestOut,
    status_code=201,
    dependencies=[Depends(requires("complaint:write"))],
    summary="Record an inbound complaint",
)
async def ingest_complaint(
    body: ComplaintIn,
    ingestion: IngestionDep,
    site_id: SiteIdDep,
    clock: ClockDep,
    principal: PrincipalDep,
    audit: AuditDep,
    request_ip: Annotated[str, Depends(client_ip)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IngestOut:
    # The injected clock, not the wall clock: one instant governs validation, dedupe
    # bucketing and the audit row.
    received_at = body.received_at or clock.now()
    result = ingestion.ingest(
        InboundComplaint(
            raw_msisdn=body.msisdn,
            text=body.text,
            channel=body.channel,
            received_at=received_at,
            source_ts=body.source_ts,
            stated_location=body.stated_location,
            external_ref=body.external_ref,
            language=body.language,
            cell_id=body.cell_id,
            region=body.region,
            idempotency_key=idempotency_key,
        ),
    )

    out = IngestOut(
        complaint_id=result.complaint_id,
        subscriber_id=result.subscriber_id,
        duplicate=result.duplicate,
        needs_human=result.needs_human,
        is_late=result.is_late,
        is_skewed=result.is_skewed,
        text_truncated=result.text_truncated,
        reasons=list(result.reasons),
    )

    if body.locate and not result.duplicate:
        from tazama.modules.site_identification.models import ComplaintLocation

        guess = site_id.identify(
            ComplaintLocation(
                complaint_id=result.complaint_id,
                subscriber_id=result.subscriber_id,
                stated_location=body.stated_location,
                region=body.region,
                cell_id=body.cell_id,
                received_at=received_at,
            ),
        )
        out = out.model_copy(
            update={
                "cell_id": guess.cell_id,
                "site_id": guess.site_id,
                "site_confidence": guess.confidence,
                "location_method": guess.method,
                "needs_human": out.needs_human or guess.needs_human,
            },
        )

    audit.record_human_action(
        principal=principal,
        action="ingest_complaint",
        entity_type="complaint",
        entity_id=result.complaint_id,
        outcome="duplicate" if result.duplicate else "stored",
        inputs={"channel": str(body.channel), "located": body.locate},
        client_ip=request_ip,
    )
    return out


@router.post(
    "/care-calls",
    response_model=CareCallOut,
    status_code=201,
    dependencies=[Depends(requires("complaint:write"))],
    summary="Record an inbound care call",
)
async def ingest_care_call(
    body: CareCallIn,
    ingestion: IngestionDep,
    clock: ClockDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CareCallOut:
    result = ingestion.ingest_care_call(
        InboundCareCall(
            raw_msisdn=body.msisdn,
            reason_code=body.reason_code,
            started_at=body.started_at or clock.now(),
            duration_s=body.duration_s,
            reason_text=body.reason_text,
            channel=body.channel,
            agent_ref=body.agent_ref,
            region=body.region,
            cell_id=body.cell_id,
            is_repeat=body.is_repeat,
            idempotency_key=idempotency_key,
        ),
    )
    return CareCallOut(
        care_call_id=result.care_call_id,
        subscriber_id=result.subscriber_id,
        duplicate=result.duplicate,
        needs_human=result.needs_human,
        reasons=list(result.reasons),
    )


@router.get(
    "/complaints",
    response_model=ComplaintPage,
    dependencies=[Depends(requires("complaint:read"))],
    summary="Browse complaints",
)
async def list_complaints(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    region: Annotated[str | None, Query(max_length=32)] = None,
    cell_id: Annotated[str | None, Query(max_length=40)] = None,
    incident_id: Annotated[str | None, Query(max_length=40)] = None,
    channel: Channel | None = None,
    category: Annotated[str | None, Query(max_length=32)] = None,
    unresolved_only: Annotated[bool, Query(description="Only complaints with no cell")] = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> ComplaintPage:
    from sqlalchemy import func

    filters: list[Any] = []
    if region:
        filters.append(Complaint.region == region)
    if cell_id:
        filters.append(Complaint.cell_id == cell_id)
    if incident_id:
        filters.append(Complaint.incident_id == incident_id)
    if channel:
        filters.append(Complaint.channel == str(channel))
    if category:
        filters.append(Complaint.category == category)
    if unresolved_only:
        filters.append(Complaint.cell_id.is_(None))
    if since:
        filters.append(Complaint.received_at >= since)
    if until:
        filters.append(Complaint.received_at < until)

    total = int(
        session.execute(
            select(func.count()).select_from(Complaint).where(*filters),
        ).scalar_one(),
    )
    rows = (
        session.execute(
            select(Complaint)
            .where(*filters)
            .order_by(Complaint.received_at.desc(), Complaint.id.desc())
            .limit(limit)
            .offset(offset),
        )
        .scalars()
        .all()
    )
    return ComplaintPage(
        items=[_row_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/complaints/{complaint_id}",
    response_model=ComplaintOut,
    dependencies=[Depends(requires("complaint:read"))],
    summary="One complaint",
)
async def get_complaint(complaint_id: str, session: SessionDep) -> ComplaintOut:
    row = session.get(Complaint, complaint_id)
    if row is None:
        raise NotFound("Complaint not found.")
    return _row_out(row)
