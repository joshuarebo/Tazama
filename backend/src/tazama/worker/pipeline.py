"""The pipeline, wired as event consumers.

This is the file that makes the chain in PRD section 8 actually run by itself:

    ComplaintReceived  -> locate the complaint              -> SiteIdentified
    SiteIdentified     -> correlate it                      -> IncidentDetected/Updated
    DegradationDetected-> correlate it (network-first)      -> IncidentDetected/Updated
    IncidentDetected   -> measure impact                    -> ImpactCalculated
    ImpactCalculated   -> score priority, then route        -> CasePrioritized, CaseRouted
    VerificationCompleted -> resolve, then close the case    -> CaseClosed

Each step runs in **its own transaction** with its own service graph, so a failure in one
step cannot half-commit another, and the outbox redelivers only the step that failed.

Every handler is idempotent, because the outbox delivers at least once and replay delivers
again on purpose: the services use deterministic ids and the event writer deduplicates on
`event_id`, so re-running a step converges instead of duplicating.

Handlers raise on a transient failure (the dispatcher retries, then dead-letters) and
return quietly when there is simply nothing to do — a complaint with no resolvable cell is
an outcome, not an error.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.engine import Engine

from tazama.composition import Services, unit_of_work
from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.core.settings import Settings
from tazama.domain.events import DomainEvent, EventType
from tazama.domain.value_objects import ComplaintCategory, TimeWindow

_log = get_logger(__name__)

Handler = Callable[[DomainEvent], None]

#: How far back a correlation pass looks when it is triggered by one complaint. Wide
#: enough to catch the cluster the complaint belongs to, narrow enough to stay cheap.
CORRELATION_LOOKBACK = timedelta(hours=3)

#: Default assumed duration when an incident has no end yet, for impact measurement.
OPEN_INCIDENT_SPAN = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """What the handlers need to open their own units of work."""

    settings: Settings
    policies: Policies
    clock: Clock
    engine: Engine


def _complaint_row(services: Services, complaint_id: str) -> Any:
    from tazama.infrastructure.db.models import Complaint

    return services.session.get(Complaint, complaint_id)


# --------------------------------------------------------------- handlers


def enrich_complaint(ctx: PipelineContext) -> Handler:
    """`ComplaintReceived` -> what the complaint is about.

    Ingestion deliberately does not classify (its job is to store the fact safely), and
    correlation refuses an unclassified complaint, so this is the step that gives the text
    a meaning. It runs on the deterministic rules classifier; Phase 4 puts a model in front
    of it without changing this handler.
    """

    def handle(event: DomainEvent) -> None:
        from tazama.modules.enrichment.models import EnrichmentInput

        complaint_id = event.aggregate_id
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            row = _complaint_row(services, complaint_id)
            if row is None:
                _log.warning(
                    "enrich_skipped_missing_complaint",
                    extra={"complaint_id": complaint_id},
                )
                return
            if row.category:
                return  # already classified; a redelivery must not relabel it

            result = services.enrichment.enrich(
                EnrichmentInput(
                    complaint_id=row.id,
                    text=row.raw_text or "",
                    channel=row.channel,
                    declared_language=None,
                    stated_location=row.stated_location,
                ),
            )
            # Written here, in the root's transaction: enrichment answers, it does not own
            # the complaint row.
            row.category = str(result.category)
            row.is_network = result.is_network
            row.language = str(result.language)
            row.sentiment = result.sentiment
            row.urgency = result.urgency
            row.classifier_version = result.method
            row.status = "needs_human" if result.needs_human else "enriched"

    return handle


def locate_complaint(ctx: PipelineContext) -> Handler:
    """`ComplaintEnriched` -> resolve a serving cell, or ask for a human."""

    def handle(event: DomainEvent) -> None:
        from tazama.modules.site_identification.models import ComplaintLocation

        complaint_id = event.aggregate_id
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            row = _complaint_row(services, complaint_id)
            if row is None:
                # The complaint is gone: nothing to locate, and retrying will not help.
                _log.warning(
                    "locate_skipped_missing_complaint",
                    extra={"complaint_id": complaint_id},
                )
                return
            if row.cell_id and row.site_confidence is not None:
                return  # already located; a redelivery must not redo the work

            guess = services.site_identification.identify(
                ComplaintLocation(
                    complaint_id=row.id,
                    received_at=row.received_at,
                    subscriber_id=row.subscriber_id,
                    stated_location=row.stated_location,
                    region=row.region,
                    cell_id=row.cell_id,
                ),
            )
            # The guess is written here, in the composition root's transaction: site
            # identification returns an answer and does not own the complaint row.
            row.site_confidence = guess.confidence
            if not guess.needs_human and guess.cell_id:
                row.cell_id = guess.cell_id
                row.site_id = guess.site_id
                row.region = row.region or guess.region
                row.status = "located"
            else:
                row.status = "needs_human"

    return handle


def correlate_complaint(ctx: PipelineContext) -> Handler:
    """`SiteIdentified` -> cluster the complaint into an incident, or suppress it."""

    def handle(event: DomainEvent) -> None:
        from tazama.modules.correlation.models import ComplaintObservation

        complaint_id = event.aggregate_id
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            row = _complaint_row(services, complaint_id)
            if row is None or not row.cell_id or not row.site_id:
                return  # unlocated complaints are site identification's problem, not this one
            if row.incident_id:
                return  # already correlated

            category = _category(row.category)
            outcome = services.correlation.correlate_complaint(
                ComplaintObservation(
                    complaint_id=row.id,
                    cell_id=row.cell_id,
                    site_id=row.site_id,
                    region=row.region or "",
                    category=category,
                    # Enrichment's verdict, not a second guess from the category: a
                    # complaint that names the network without naming a symptom is
                    # `other` and still counts.
                    network=row.is_network,
                    received_at=row.received_at,
                    source_ts=row.source_ts,
                    dedupe_key=row.dedupe_key or row.id,
                ),
            )
            # `raised_incident` is the outcome's own answer to "did this become a live
            # incident": a suppressed candidate has an id too, and must not be attached.
            if outcome.raised_incident and outcome.incident_id:
                row.incident_id = outcome.incident_id
                row.status = "correlated"
            elif outcome.suppressed_reason:
                row.status = "suppressed"

    return handle


def correlate_degradation(ctx: PipelineContext) -> Handler:
    """`DegradationDetected` -> the network-first entry point into the same engine.

    The degradation scan itself publishes these events; this handler exists so that a
    degradation raised by anything else (a future NMS adapter, a replay) reaches the
    engine by the same route.
    """

    def handle(event: DomainEvent) -> None:
        payload = event.payload or {}
        start = payload.get("window_start")
        end = payload.get("window_end")
        if not start or not end:
            return
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            window = TimeWindow(start=_parse(start), end=_parse(end))
            services.correlation.detect_degradations(window)

    return handle


def measure_impact(ctx: PipelineContext) -> Handler:
    """`IncidentDetected` / `IncidentUpdated` -> how much and whom this affects."""

    def handle(event: DomainEvent) -> None:
        from tazama.modules.impact.models import ImpactTarget

        incident_id = event.aggregate_id
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            incident = services.incidents.get(incident_id)
            cells = tuple(services.incidents.links_for(incident_id))
            cell_ids = tuple(
                link.entity_id for link in cells if getattr(link, "entity_type", "") == "cell"
            )
            started = getattr(incident, "started_at", None) or incident.detected_at
            ended = getattr(incident, "resolved_at", None) or (started + OPEN_INCIDENT_SPAN)
            impact = services.impact.calculate(
                ImpactTarget(
                    incident_id=incident.id,
                    region=incident.region,
                    window=TimeWindow(start=started, end=ended),
                    cause=str(incident.cause) if incident.cause else "unknown",
                    primary_node_id=getattr(incident, "primary_node_id", None),
                    primary_node_kind=getattr(incident, "primary_node_kind", None),
                    severity=float(getattr(incident, "cause_confidence", 0.0) or 0.0),
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

    return handle


def prioritise_and_route(ctx: PipelineContext) -> Handler:
    """`ImpactCalculated` -> a priority, a team and an SLA clock.

    One handler for both steps because routing's inputs are the priority decision's
    outputs: splitting them would mean reading the case back to learn what we just decided.
    """

    def handle(event: DomainEvent) -> None:
        from tazama.modules.prioritisation.models import PriorityInputs
        from tazama.modules.routing.models import RoutingInputs

        incident_id = event.aggregate_id
        payload = event.payload or {}
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            incident = services.incidents.get(incident_id)
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

    return handle


def close_verified_incident(ctx: PipelineContext) -> Handler:
    """`VerificationCompleted` -> resolve and close, but only on a pass."""

    def handle(event: DomainEvent) -> None:
        payload = event.payload or {}
        if not payload.get("passed", False):
            return  # a failed verification reopens, which verification itself publishes
        incident_id = str(payload.get("incident_id") or event.aggregate_id)
        with unit_of_work(
            engine=ctx.engine,
            settings=ctx.settings,
            clock=ctx.clock,
            policies=ctx.policies,
        ) as services:
            incident = services.incidents.get(incident_id)
            if str(incident.status) in ("closed", "suppressed"):
                return
            services.incidents.close(incident_id)

    return handle


# ----------------------------------------------------------------- wiring


def build_pipeline_consumers(
    *,
    settings: Settings,
    policies: Policies,
    clock: Clock,
    engine: Engine,
) -> dict[EventType, Sequence[Handler]]:
    """The event-to-handler map the worker registers.

    Read it top to bottom and you have the pipeline.
    """
    ctx = PipelineContext(settings=settings, policies=policies, clock=clock, engine=engine)
    consumers: dict[EventType, Sequence[Handler]] = {
        EventType.COMPLAINT_RECEIVED: (enrich_complaint(ctx),),
        EventType.COMPLAINT_ENRICHED: (locate_complaint(ctx),),
        EventType.SITE_IDENTIFIED: (correlate_complaint(ctx),),
        EventType.DEGRADATION_DETECTED: (correlate_degradation(ctx),),
        EventType.INCIDENT_DETECTED: (measure_impact(ctx),),
        EventType.INCIDENT_UPDATED: (measure_impact(ctx),),
        EventType.IMPACT_CALCULATED: (prioritise_and_route(ctx),),
        EventType.VERIFICATION_COMPLETED: (close_verified_incident(ctx),),
    }
    _log.info("pipeline_wired", extra={"event_types": len(consumers)})
    return consumers


def _category(value: str | None) -> ComplaintCategory:
    """An unclassified complaint is `OTHER`, which never raises a network incident."""
    if not value:
        return ComplaintCategory.OTHER
    try:
        return ComplaintCategory(value)
    except ValueError:
        return ComplaintCategory.OTHER


def _parse(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
