"""Incidents: the Command Center's data, and the incident card's evidence.

The incident card is the product's centrepiece, so this router is shaped around what a NOC
engineer needs to decide in one screen: what is broken, how big it is, what the evidence is,
what has been decided already, what has happened before, and what to do next.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from tazama.api.deps import AuditDep, SessionDep, requires
from tazama.core.errors import NotFound
from tazama.infrastructure.db.models import (
    Alarm,
    Cell,
    Complaint,
    Incident,
    IncidentLink,
    KpiCell,
    MaintenanceWindow,
    Notification,
    RecoveryAction,
    Site,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])

MAX_PAGE = 200
#: How much KPI history the incident card charts around the incident window.
CHART_PADDING = timedelta(hours=6)
KPI_COLUMNS = (
    "availability_pct",
    "rrc_setup_success_pct",
    "erab_setup_success_pct",
    "drop_rate_pct",
    "prb_utilisation_pct",
    "dl_throughput_mbps",
    "ul_throughput_mbps",
    "latency_ms",
    "active_users",
    "sinr_db",
)


class IncidentSummary(BaseModel):
    id: str
    title: str
    status: str
    priority: str
    cause: str | None
    region: str
    primary_node_id: str | None
    primary_node_kind: str | None
    detected_at: str
    started_at: str | None
    resolved_at: str | None
    affected_cells: int
    affected_sites: int
    affected_subs: int
    enterprise_accounts: int
    complaint_count: int
    sla_risk: float | None
    suppressed_reason: str | None
    duration_minutes: float | None


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    total: int
    limit: int
    offset: int


class MapPoint(BaseModel):
    cell_id: str
    site_id: str
    name: str
    lat: float
    lon: float
    tech: str
    region: str
    subscribers_est: int


class EvidenceItem(BaseModel):
    kind: str
    ref: str
    note: str = ""
    weight: float = 1.0


class DecisionItem(BaseModel):
    occurred_at: str
    actor: str
    action: str
    outcome: str
    confidence: float | None
    rule_version: str | None
    evidence: list[EvidenceItem] = Field(default_factory=list)


class AlarmItem(BaseModel):
    id: str
    node_id: str
    alarm_type: str
    severity: str
    raised_at: str
    cleared_at: str | None
    is_flapping: bool


class ComplaintItem(BaseModel):
    id: str
    received_at: str
    channel: str
    category: str | None
    language: str | None
    urgency: int | None
    cell_id: str | None
    text_preview: str


class KpiSeries(BaseModel):
    kpi: str
    cell_id: str
    points: list[dict[str, Any]]


class SimilarIncident(BaseModel):
    id: str
    title: str
    cause: str | None
    region: str
    detected_at: str
    status: str
    affected_subs: int


class IncidentDetail(BaseModel):
    incident: IncidentSummary
    cells: list[MapPoint]
    alarms: list[AlarmItem]
    complaints: list[ComplaintItem]
    kpi_series: list[KpiSeries]
    decisions: list[DecisionItem]
    similar: list[SimilarIncident]
    maintenance: list[dict[str, Any]]
    notifications: list[dict[str, Any]]
    recovery: list[dict[str, Any]]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _duration(row: Incident) -> float | None:
    start = row.started_at or row.detected_at
    end = row.resolved_at
    if start is None:
        return None
    if end is None:
        return None
    return round((end - start).total_seconds() / 60, 1)


def _summary(row: Incident) -> IncidentSummary:
    return IncidentSummary(
        id=row.id,
        title=row.title,
        status=row.status,
        priority=row.priority,
        cause=row.cause,
        region=row.region,
        primary_node_id=row.primary_node_id,
        primary_node_kind=row.primary_node_kind,
        detected_at=_iso(row.detected_at) or "",
        started_at=_iso(row.started_at),
        resolved_at=_iso(row.resolved_at),
        affected_cells=row.affected_cells,
        affected_sites=row.affected_sites,
        affected_subs=row.affected_subs,
        enterprise_accounts=row.enterprise_accounts,
        complaint_count=row.complaint_count,
        sla_risk=row.sla_risk,
        suppressed_reason=row.suppressed_reason,
        duration_minutes=_duration(row),
    )


@router.get(
    "",
    response_model=IncidentPage,
    dependencies=[Depends(requires("incident:read"))],
    summary="Live incidents for the Command Center",
)
async def list_incidents(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    status: Annotated[str | None, Query(max_length=32)] = None,
    priority: Annotated[str | None, Query(max_length=4)] = None,
    region: Annotated[str | None, Query(max_length=32)] = None,
    include_suppressed: bool = False,
) -> IncidentPage:
    filters: list[Any] = []
    if status:
        filters.append(Incident.status == status)
    if priority:
        filters.append(Incident.priority == priority)
    if region:
        filters.append(Incident.region == region)
    if not include_suppressed:
        # Suppressed incidents are recorded, not shown: the Command Center is for things
        # that need attention.
        filters.append(Incident.status != "suppressed")

    total = int(
        session.execute(select(func.count()).select_from(Incident).where(*filters)).scalar_one(),
    )
    rows = (
        session.execute(
            select(Incident)
            .where(*filters)
            .order_by(Incident.priority, Incident.detected_at.desc())
            .limit(limit)
            .offset(offset),
        )
        .scalars()
        .all()
    )
    return IncidentPage(
        items=[_summary(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


class RegionStat(BaseModel):
    region: str
    incidents: int
    affected_subs: int


class Overview(BaseModel):
    live_incidents: int
    by_priority: dict[str, int]
    by_cause: dict[str, int]
    by_region: list[RegionStat]
    suppressed: int
    affected_subs: int
    complaints_24h: int
    complaints_correlated: int
    open_notifications: int
    data_window_end: str | None


@router.get(
    "/overview",
    response_model=Overview,
    dependencies=[Depends(requires("incident:read"))],
    summary="Headline numbers for the Command Center",
)
async def overview(session: SessionDep) -> Overview:
    live_filter = Incident.status.not_in(("suppressed", "closed"))

    by_priority = {
        str(priority): int(count)
        for priority, count in session.execute(
            select(Incident.priority, func.count()).where(live_filter).group_by(Incident.priority),
        ).all()
    }
    by_cause = {
        str(cause or "unknown"): int(count)
        for cause, count in session.execute(
            select(Incident.cause, func.count()).where(live_filter).group_by(Incident.cause),
        ).all()
    }
    regions = [
        RegionStat(region=str(region), incidents=int(count), affected_subs=int(subs or 0))
        for region, count, subs in session.execute(
            select(Incident.region, func.count(), func.sum(Incident.affected_subs))
            .where(live_filter)
            .group_by(Incident.region)
            .order_by(func.count().desc()),
        ).all()
    ]
    latest = session.execute(select(func.max(KpiCell.bucket_start))).scalar_one_or_none()
    since = (latest or datetime.now(tz=UTC)) - timedelta(hours=24)

    return Overview(
        live_incidents=sum(by_priority.values()),
        by_priority=by_priority,
        by_cause=by_cause,
        by_region=regions,
        suppressed=int(
            session.execute(
                select(func.count()).select_from(Incident).where(Incident.status == "suppressed"),
            ).scalar_one(),
        ),
        affected_subs=int(
            session.execute(
                select(func.coalesce(func.sum(Incident.affected_subs), 0)).where(live_filter),
            ).scalar_one(),
        ),
        complaints_24h=int(
            session.execute(
                select(func.count())
                .select_from(Complaint)
                .where(Complaint.received_at >= since),
            ).scalar_one(),
        ),
        complaints_correlated=int(
            session.execute(
                select(func.count())
                .select_from(Complaint)
                .where(Complaint.incident_id.is_not(None)),
            ).scalar_one(),
        ),
        open_notifications=int(
            session.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.status == "proposed"),
            ).scalar_one(),
        ),
        data_window_end=_iso(latest),
    )


@router.get(
    "/map",
    response_model=list[MapPoint],
    dependencies=[Depends(requires("incident:read"))],
    summary="Affected cells with coordinates, for the map",
)
async def incident_map(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[MapPoint]:
    rows = session.execute(
        select(Cell)
        .join(IncidentLink, IncidentLink.entity_id == Cell.id)
        .join(Incident, Incident.id == IncidentLink.incident_id)
        .where(IncidentLink.entity_type == "cell")
        .where(Incident.status.not_in(("suppressed", "closed")))
        .limit(limit),
    ).scalars()
    return [_map_point(cell) for cell in rows]


def _map_point(cell: Cell) -> MapPoint:
    return MapPoint(
        cell_id=cell.id,
        site_id=cell.site_id,
        name=cell.name,
        lat=cell.lat,
        lon=cell.lon,
        tech=cell.tech,
        region=cell.region,
        subscribers_est=cell.subscribers_est,
    )


@router.get(
    "/{incident_id}",
    response_model=IncidentDetail,
    dependencies=[Depends(requires("incident:read"))],
    summary="One incident, with its evidence, decisions and history",
)
async def incident_detail(
    incident_id: str,
    session: SessionDep,
    audit: AuditDep,
) -> IncidentDetail:
    row = session.get(Incident, incident_id)
    if row is None:
        raise NotFound("Incident not found.")

    links = (
        session.execute(select(IncidentLink).where(IncidentLink.incident_id == incident_id))
        .scalars()
        .all()
    )
    cell_ids = [link.entity_id for link in links if link.entity_type == "cell"]
    alarm_ids = [link.entity_id for link in links if link.entity_type == "alarm"]
    complaint_ids = [link.entity_id for link in links if link.entity_type == "complaint"]

    cells = (
        session.execute(select(Cell).where(Cell.id.in_(cell_ids))).scalars().all()
        if cell_ids
        else []
    )
    alarms = (
        session.execute(
            select(Alarm).where(Alarm.id.in_(alarm_ids)).order_by(Alarm.raised_at),
        )
        .scalars()
        .all()
        if alarm_ids
        else []
    )
    complaints = (
        session.execute(
            select(Complaint)
            .where(Complaint.id.in_(complaint_ids))
            .order_by(Complaint.received_at)
            .limit(50),
        )
        .scalars()
        .all()
        if complaint_ids
        else []
    )

    window_start = (row.started_at or row.detected_at) - CHART_PADDING
    window_end = (row.resolved_at or row.detected_at) + CHART_PADDING
    series: list[KpiSeries] = []
    for cell_id in cell_ids[:4]:  # four cells is as much as a card can show usefully
        points = session.execute(
            select(KpiCell)
            .where(KpiCell.cell_id == cell_id)
            .where(KpiCell.granularity == "1h")
            .where(KpiCell.bucket_start >= window_start)
            .where(KpiCell.bucket_start < window_end)
            .order_by(KpiCell.bucket_start),
        ).scalars().all()
        if not points:
            continue
        series.append(
            KpiSeries(
                kpi="all",
                cell_id=cell_id,
                points=[
                    {
                        "at": _iso(point.bucket_start),
                        **{name: getattr(point, name) for name in KPI_COLUMNS},
                    }
                    for point in points
                ],
            ),
        )

    decisions = [
        DecisionItem(
            occurred_at=_iso(decision.occurred_at) or "",
            actor=decision.actor,
            action=decision.action,
            outcome=decision.outcome,
            confidence=decision.confidence,
            rule_version=decision.rule_version,
            evidence=[
                EvidenceItem(kind=item.kind, ref=item.ref, note=item.note, weight=item.weight)
                for item in decision.evidence
            ],
        )
        for decision in audit.history("incident", incident_id, limit=50)
    ]

    similar = (
        session.execute(
            select(Incident)
            .where(Incident.id != incident_id)
            .where(Incident.cause == row.cause)
            .order_by(Incident.detected_at.desc())
            .limit(5),
        )
        .scalars()
        .all()
    )

    maintenance = (
        session.execute(
            select(MaintenanceWindow)
            .where(MaintenanceWindow.ends_at >= window_start)
            .where(MaintenanceWindow.starts_at < window_end)
            .limit(10),
        )
        .scalars()
        .all()
    )

    notifications = (
        session.execute(
            select(Notification)
            .where(Notification.incident_id == incident_id)
            .order_by(Notification.proposed_at.desc())
            .limit(20),
        )
        .scalars()
        .all()
    )
    recovery = (
        session.execute(
            select(RecoveryAction)
            .where(RecoveryAction.incident_id == incident_id)
            .order_by(RecoveryAction.proposed_at.desc())
            .limit(10),
        )
        .scalars()
        .all()
    )

    return IncidentDetail(
        incident=_summary(row),
        cells=[_map_point(cell) for cell in cells],
        alarms=[
            AlarmItem(
                id=alarm.id,
                node_id=alarm.node_id,
                alarm_type=alarm.alarm_type,
                severity=alarm.severity,
                raised_at=_iso(alarm.raised_at) or "",
                cleared_at=_iso(alarm.cleared_at),
                is_flapping=alarm.is_flapping,
            )
            for alarm in alarms
        ],
        complaints=[
            ComplaintItem(
                id=complaint.id,
                received_at=_iso(complaint.received_at) or "",
                channel=complaint.channel,
                category=complaint.category,
                language=complaint.language,
                urgency=complaint.urgency,
                cell_id=complaint.cell_id,
                text_preview=" ".join((complaint.raw_text or "").split())[:100],
            )
            for complaint in complaints
        ],
        kpi_series=series,
        decisions=decisions,
        similar=[
            SimilarIncident(
                id=other.id,
                title=other.title,
                cause=other.cause,
                region=other.region,
                detected_at=_iso(other.detected_at) or "",
                status=other.status,
                affected_subs=other.affected_subs,
            )
            for other in similar
        ],
        maintenance=[
            {
                "id": window.id,
                "node_id": window.node_id,
                "starts_at": _iso(window.starts_at),
                "ends_at": _iso(window.ends_at),
                "description": window.description,
            }
            for window in maintenance
        ],
        notifications=[
            {
                "id": notification.id,
                "status": notification.status,
                "template_id": notification.template_id,
                "language": notification.language,
                "proposed_at": _iso(notification.proposed_at),
                "text_preview": (notification.rendered_text or "")[:120],
            }
            for notification in notifications
        ],
        recovery=[
            {
                "id": action.id,
                "runbook_id": action.runbook_id,
                "status": action.status,
                "risk_tier": action.risk_tier,
                "proposed_at": _iso(action.proposed_at),
                "approved_by": action.approved_by,
                "executed": False,
            }
            for action in recovery
        ],
    )


@router.get(
    "/{incident_id}/site-names",
    response_model=dict[str, str],
    dependencies=[Depends(requires("incident:read"))],
    summary="Site names for the incident's cells",
)
async def site_names(incident_id: str, session: SessionDep) -> dict[str, str]:
    rows = session.execute(
        select(Site.id, Site.name)
        .join(Cell, Cell.site_id == Site.id)
        .join(IncidentLink, IncidentLink.entity_id == Cell.id)
        .where(IncidentLink.incident_id == incident_id)
        .where(IncidentLink.entity_type == "cell"),
    ).all()
    return {str(site_id): str(name) for site_id, name in rows}
