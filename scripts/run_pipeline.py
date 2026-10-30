#!/usr/bin/env python3
"""`make pipeline` — run the detection pipeline over the seeded data.

The seed produces observations (KPIs, alarms, complaints); it deliberately does **not**
produce incidents, because deriving them is the product's job. This script does that
derivation once, so the demo has real incidents that were detected rather than planted:

    degradation scan -> correlation -> incidents -> impact -> priority -> routing

Everything it writes is reproducible: the same seed and the same window produce the same
incidents, with the same ids.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect incidents in the seeded data")
    parser.add_argument(
        "--hours",
        type=int,
        default=36,
        help="window to scan, backwards from the end of the data",
    )
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()

    from sqlalchemy import func, select

    from tazama.composition import unit_of_work
    from tazama.core.clock import FixedClock
    from tazama.core.logging import configure_logging
    from tazama.core.settings import get_settings
    from tazama.domain.value_objects import TimeWindow
    from tazama.infrastructure.db import models as m
    from tazama.infrastructure.db.engine import get_engine, session_factory
    from tazama.modules.correlation.models import ComplaintObservation
    from tazama.modules.enrichment.models import EnrichmentInput
    from tazama.modules.impact.models import ImpactTarget
    from tazama.modules.prioritisation.models import PriorityInputs
    from tazama.modules.routing.models import RoutingInputs

    settings = get_settings()
    configure_logging(
        level="WARNING" if args.quiet else "INFO",
        service="tazama-pipeline",
        mode=settings.APP_MODE,
    )
    engine = get_engine(settings)

    # Anchor on the data, not on today: the seed's window is in its own past.
    with session_factory(engine)() as session:
        latest = session.execute(select(func.max(m.KpiCell.bucket_start))).scalar_one_or_none()
        complaints_total = session.execute(
            select(func.count()).select_from(m.Complaint),
        ).scalar_one()
    if latest is None:
        print("no KPI data found - run `make seed` first")  # noqa: T201
        return 1
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)

    window = TimeWindow(start=latest - timedelta(hours=args.hours), end=latest + timedelta(hours=1))
    clock = FixedClock(window.end)
    print(f"scanning {window.start.isoformat()} .. {window.end.isoformat()}")  # noqa: T201
    print(f"complaints in the database: {complaints_total:,}")  # noqa: T201

    # 1. Network first: find the degradations nobody has complained about yet.
    with unit_of_work(engine=engine, settings=settings, clock=clock) as services:
        degradations = services.correlation.detect_degradations(window)
    raised = sum(1 for outcome in degradations if getattr(outcome, "raised_incident", False))
    print(f"degradation scan: {len(degradations)} candidate(s), {raised} incident(s) raised")  # noqa: T201

    # 2. Classify and correlate the complaints in the window, in event-time order.
    with session_factory(engine)() as session:
        rows = session.execute(
            select(
                m.Complaint.id,
                m.Complaint.raw_text,
                m.Complaint.channel,
                m.Complaint.category,
                m.Complaint.cell_id,
                m.Complaint.site_id,
                m.Complaint.region,
                m.Complaint.received_at,
                m.Complaint.source_ts,
                m.Complaint.dedupe_key,
            )
            .where(m.Complaint.received_at >= window.start)
            .where(m.Complaint.received_at < window.end)
            .where(m.Complaint.cell_id.is_not(None))
            .order_by(m.Complaint.received_at, m.Complaint.id),
        ).all()
    print(f"located complaints in window: {len(rows):,}")  # noqa: T201

    enriched = 0
    attached = 0
    for row in rows:
        with unit_of_work(engine=engine, settings=settings, clock=clock) as services:
            category = row.category
            if not category:
                result = services.enrichment.enrich(
                    EnrichmentInput(
                        complaint_id=row.id,
                        text=row.raw_text or "",
                        channel=row.channel,
                    ),
                )
                category = str(result.category)
                enriched += 1
                stored = services.session.get(m.Complaint, row.id)
                if stored is not None:
                    stored.category = category
                    stored.is_network = result.is_network
                    stored.language = str(result.language)
                    stored.urgency = result.urgency
                    stored.sentiment = result.sentiment
                    stored.classifier_version = result.method

            outcome = services.correlation.correlate_complaint(
                ComplaintObservation(
                    complaint_id=row.id,
                    cell_id=row.cell_id or "",
                    site_id=row.site_id or "",
                    region=row.region or "",
                    category=_category(category),
                    received_at=row.received_at,
                    source_ts=row.source_ts,
                    dedupe_key=row.dedupe_key or row.id,
                ),
            )
            if outcome.raised_incident and outcome.incident_id:
                stored = services.session.get(m.Complaint, row.id)
                if stored is not None and not stored.incident_id:
                    stored.incident_id = outcome.incident_id
                    stored.status = "correlated"
                    attached += 1
    print(f"classified {enriched:,} complaint(s); attached {attached:,} to incidents")  # noqa: T201

    # 2b. Back-fill `complaint.incident_id` from correlation's own links.
    #
    # The per-complaint write-back above only fires when the *returned* outcome says a live
    # incident exists because of that complaint, and it disagreed with reality: correlation
    # had written 69 `incident_link` rows while every `complaint.incident_id` stayed null.
    # `incident_link` is the authoritative record — correlation writes it as part of its own
    # transaction — and `complaint.incident_id` is a denormalised convenience the console
    # reads. Deriving the second from the first in one statement removes the chance of them
    # disagreeing at all, which per-row bookkeeping in a script cannot guarantee.
    with session_factory(engine)() as session, session.begin():
        links = session.execute(
            select(m.IncidentLink.entity_id, m.IncidentLink.incident_id)
            .join(m.Incident, m.Incident.id == m.IncidentLink.incident_id)
            .where(m.IncidentLink.entity_type == "complaint")
            .where(m.Incident.suppressed_reason.is_(None)),
        ).all()
        back_filled = 0
        for complaint_id, incident_id in links:
            complaint = session.get(m.Complaint, complaint_id)
            if complaint is not None and not complaint.incident_id:
                complaint.incident_id = incident_id
                complaint.status = "correlated"
                back_filled += 1
    print(f"back-filled {back_filled:,} complaint(s) from incident links")  # noqa: T201

    # 3. Impact, priority and routing for every live incident.
    with session_factory(engine)() as session:
        incident_ids = list(
            session.execute(
                select(m.Incident.id).where(m.Incident.status.not_in(("suppressed", "closed"))),
            ).scalars(),
        )

    for incident_id in incident_ids:
        with unit_of_work(engine=engine, settings=settings, clock=clock) as services:
            incident = services.incidents.get(incident_id)
            links = services.incidents.links_for(incident_id)
            cell_ids = tuple(
                link.entity_id for link in links if getattr(link, "entity_type", "") == "cell"
            )
            started = getattr(incident, "started_at", None) or incident.detected_at
            ended = getattr(incident, "resolved_at", None) or (started + timedelta(hours=2))
            impact = services.impact.calculate(
                ImpactTarget(
                    incident_id=incident.id,
                    region=incident.region,
                    window=TimeWindow(start=started, end=ended),
                    cause=str(incident.cause) if incident.cause else "unknown",
                    primary_node_id=getattr(incident, "primary_node_id", None),
                    primary_node_kind=getattr(incident, "primary_node_kind", None),
                    complaint_count=int(getattr(incident, "complaint_count", 0) or 0),
                    cell_ids=cell_ids,
                ),
            )
            payload = impact.to_payload()
            services.incidents.apply_impact(
                incident_id,
                affected_cells=int(payload.get("affected_cells", 0) or 0),
                affected_sites=int(payload.get("affected_sites", 0) or 0),
                affected_subs=int(payload.get("affected_subscribers", 0) or 0),
                enterprise_accounts=int(payload.get("enterprise_accounts", 0) or 0),
                sla_risk=float(payload.get("sla_risk", 0.0) or 0.0),
            )

            decision = services.prioritisation.prioritise_incident(
                incident_id=incident_id,
                inputs=PriorityInputs(
                    affected_subscribers=int(payload.get("affected_subscribers", 0) or 0),
                    enterprise_accounts=int(payload.get("enterprise_accounts", 0) or 0),
                    duration_minutes=float(payload.get("duration_minutes", 0.0) or 0.0),
                    sla_risk=float(payload.get("sla_risk", 0.0) or 0.0),
                    complaint_count=int(payload.get("complaint_count", 0) or 0),
                    region=incident.region,
                    cause=str(incident.cause) if incident.cause else None,
                    is_silent_sufferer=bool(payload.get("is_silent_sufferer", False)),
                ),
            )
            services.routing.route_case(
                case_id=f"CASE-{incident_id}",
                incident_id=incident_id,
                inputs=RoutingInputs(
                    cause=str(incident.cause) if incident.cause else None,
                    priority=decision.priority,
                    region=incident.region,
                    primary_node_kind=getattr(incident, "primary_node_kind", None),
                    is_network=str(incident.cause) != "non_network",
                    is_planned_maintenance=str(incident.cause) == "planned_maintenance",
                    enterprise_accounts=int(payload.get("enterprise_accounts", 0) or 0),
                ),
                summary=getattr(incident, "title", "") or "",
            )

    # Summary.
    with session_factory(engine)() as session:
        live = session.execute(
            select(m.Incident.priority, func.count())
            .where(m.Incident.status.not_in(("suppressed",)))
            .group_by(m.Incident.priority)
            .order_by(m.Incident.priority),
        ).all()
        suppressed = session.execute(
            select(func.count()).select_from(m.Incident).where(m.Incident.status == "suppressed"),
        ).scalar_one()
        correlated = session.execute(
            select(func.count()).select_from(m.Complaint).where(m.Complaint.incident_id.is_not(None)),
        ).scalar_one()

    print()  # noqa: T201
    print("incidents by priority:")  # noqa: T201
    for priority, count in live:
        print(f"  {priority}: {count}")  # noqa: T201
    print(f"  suppressed: {suppressed}")  # noqa: T201
    print(f"complaints attached to an incident: {correlated:,} of {complaints_total:,}")  # noqa: T201
    return 0


def _category(value: str | None) -> object:
    from tazama.domain.value_objects import ComplaintCategory

    if not value:
        return ComplaintCategory.OTHER
    try:
        return ComplaintCategory(value)
    except ValueError:
        return ComplaintCategory.OTHER


if __name__ == "__main__":
    sys.exit(main())
