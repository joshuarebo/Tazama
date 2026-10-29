"""Reports: the Teams digest, previewed rather than sent.

`TEAMS_DRY_RUN` is true by default and this router has no send endpoint at all — the card
is built, validated and returned, and the console renders it. That is deliberate: a digest
that can be posted from a GET is a digest that gets posted by accident, and the scheduled
sender (Phase 6) is the only thing that should ever hold a webhook.

The card returned here is byte-for-byte the one the sender would post, so previewing it is
a real check and not a mock-up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from tazama.api.deps import ClockDep, SessionDep, SettingsDep, requires
from tazama.infrastructure.db.models import Complaint, Incident, Notification
from tazama.modules.reporting.digest import build_card, card_size_bytes
from tazama.modules.reporting.models import Digest, DigestIncident, DigestSection

router = APIRouter(prefix="/reports", tags=["reports"])

OPEN_STATUSES = ("detected", "diagnosing", "diagnosed", "acting", "verifying", "open")

KINDS = {
    "daily": (timedelta(days=1), "TAZAMA daily network-care digest"),
    "weekly": (timedelta(days=7), "TAZAMA weekly network-care summary"),
}


class DigestIncidentOut(BaseModel):
    incident_id: str
    title: str
    priority: str
    status: str
    cause: str | None
    region: str
    affected_subs: int
    affected_cells: int
    complaint_count: int
    detected_at: str
    duration_minutes: float | None
    suppressed_reason: str | None


class DigestOut(BaseModel):
    report_id: str
    kind: str
    title: str
    generated_at: str
    window_start: str
    window_end: str
    display_tz: str
    headline: list[list[str]]
    incidents: list[DigestIncidentOut]
    sections: list[dict[str, Any]]
    caveats: list[str]
    is_demo: bool
    #: The Adaptive Card exactly as the sender would post it.
    card: dict[str, Any]
    card_bytes: int
    #: Transport state, so the console can say "nothing was sent" with authority.
    dry_run: bool
    webhook_configured: bool
    delivery_note: str = Field(
        default="Preview only. Nothing has been posted to Teams from this request.",
    )


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


@router.get(
    "/digest",
    response_model=DigestOut,
    dependencies=[Depends(requires("report:read"))],
    summary="Build the Teams digest card (preview only, never sent)",
)
async def digest(
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    kind: Annotated[str, Query(pattern="^(daily|weekly)$")] = "daily",
) -> DigestOut:
    span, title = KINDS[kind]
    now = clock.now()

    # The window ends at the latest data, not at `now`: a seeded demo database would
    # otherwise produce an empty digest the day after it was built, which looks like a
    # broken report rather than an idle network.
    latest = session.execute(select(func.max(Incident.detected_at))).scalar_one_or_none()
    window_end = now
    if latest is not None:
        latest = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
        window_end = max(latest + timedelta(minutes=1), latest)
    window_start = window_end - span

    rows = (
        session.execute(
            select(Incident)
            .where(Incident.detected_at >= window_start)
            .where(Incident.detected_at < window_end)
            .order_by(Incident.priority, Incident.affected_subs.desc())
            .limit(100),
        )
        .scalars()
        .all()
    )

    incidents = tuple(
        DigestIncident(
            incident_id=row.id,
            title=row.title,
            priority=row.priority,
            status=row.status,
            cause=row.cause,
            region=row.region,
            affected_subs=row.affected_subs or 0,
            affected_cells=row.affected_cells or 0,
            complaint_count=row.complaint_count or 0,
            detected_at=row.detected_at
            if row.detected_at.tzinfo
            else row.detected_at.replace(tzinfo=UTC),
            duration_minutes=(
                None
                if row.resolved_at is None
                else round((row.resolved_at - row.detected_at).total_seconds() / 60, 1)
            ),
            suppressed_reason=row.suppressed_reason,
        )
        for row in rows
    )

    live = [item for item in incidents if item.suppressed_reason is None]
    suppressed = len(incidents) - len(live)
    complaints = int(
        session.execute(
            select(func.count())
            .select_from(Complaint)
            .where(Complaint.received_at >= window_start)
            .where(Complaint.received_at < window_end),
        ).scalar_one(),
    )
    correlated = int(
        session.execute(
            select(func.count())
            .select_from(Complaint)
            .where(Complaint.received_at >= window_start)
            .where(Complaint.received_at < window_end)
            .where(Complaint.incident_id.is_not(None)),
        ).scalar_one(),
    )
    awaiting = int(
        session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.status == "proposed"),
        ).scalar_one(),
    )

    by_priority: dict[str, int] = {}
    for item in live:
        by_priority[item.priority] = by_priority.get(item.priority, 0) + 1
    worst_regions = sorted(
        {item.region for item in live},
        key=lambda region: -sum(i.affected_subs for i in live if i.region == region),
    )[:3]

    headline: tuple[tuple[str, str], ...] = (
        ("Incidents", f"{len(live)} live, {suppressed} suppressed"),
        (
            "By priority",
            ", ".join(f"{key} {by_priority[key]}" for key in sorted(by_priority)) or "none",
        ),
        ("Subscribers affected", f"~{sum(item.affected_subs for item in live):,}"),
        ("Complaints", f"{complaints:,} received, {correlated:,} correlated"),
        ("Awaiting approval", f"{awaiting} proposed notification(s)"),
    )

    sections: list[DigestSection] = []
    if worst_regions:
        sections.append(
            DigestSection(
                heading="Most affected regions",
                lines=tuple(
                    f"{region}: ~{sum(i.affected_subs for i in live if i.region == region):,} "
                    f"subscribers across "
                    f"{sum(1 for i in live if i.region == region)} incident(s)"
                    for region in worst_regions
                ),
            ),
        )
    if awaiting:
        sections.append(
            DigestSection(
                heading="Needs a human",
                lines=(
                    f"{awaiting} proactive notification(s) are proposed and waiting for "
                    "approval. Nothing is sent to a customer without one.",
                ),
            ),
        )

    caveats = [
        "Subscriber counts are estimates from cell exposure, not measured sessions.",
    ]
    if settings.APP_MODE == "demo":
        caveats.append("All figures come from the synthetic demo seed.")

    report = Digest(
        # Deterministic: the same window and kind always produce the same id, so a retry
        # is recognised as the same report rather than posted twice.
        report_id=f"RPT-{kind.upper()}-{window_start:%Y%m%dT%H%M}",
        kind=kind,
        title=title,
        generated_at=now,
        window_start=window_start,
        window_end=window_end,
        display_tz=settings.DISPLAY_TZ,
        headline=headline,
        incidents=incidents,
        sections=tuple(sections),
        caveats=tuple(caveats),
        is_demo=settings.APP_MODE == "demo",
    )

    card = build_card(report)

    return DigestOut(
        report_id=report.report_id,
        kind=report.kind,
        title=report.title,
        generated_at=_iso(report.generated_at),
        window_start=_iso(report.window_start),
        window_end=_iso(report.window_end),
        display_tz=report.display_tz,
        headline=[[label, value] for label, value in report.headline],
        incidents=[
            DigestIncidentOut(
                incident_id=item.incident_id,
                title=item.title,
                priority=item.priority,
                status=item.status,
                cause=item.cause,
                region=item.region,
                affected_subs=item.affected_subs,
                affected_cells=item.affected_cells,
                complaint_count=item.complaint_count,
                detected_at=_iso(item.detected_at),
                duration_minutes=item.duration_minutes,
                suppressed_reason=item.suppressed_reason,
            )
            for item in report.incidents
        ],
        sections=[
            {"heading": section.heading, "lines": list(section.lines)}
            for section in report.sections
        ],
        caveats=list(report.caveats),
        is_demo=report.is_demo,
        card=card,
        card_bytes=card_size_bytes(card),
        dry_run=settings.TEAMS_DRY_RUN,
        # Whether a webhook exists at all, never the value.
        webhook_configured=bool(settings.TEAMS_WORKFLOW_WEBHOOK_URL.get_secret_value()),
    )
