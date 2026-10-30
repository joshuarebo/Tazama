"""Service construction, shared by the API and the worker.

Both processes are composition roots, and both need the same wiring: a session, the
adapters bound to it, and the module services built from those adapters. Writing that
twice is how the two drift apart, so it lives here once and each process asks for what it
needs.

Everything is built **per unit of work**, around one session, because that is what makes
"the state change, its events and its audit row commit together" true: the repositories,
the outbox writer and the audit sink all share the transaction.

This module may import `modules` and `infrastructure` (it is a composition root, like
`api` and `worker`); nothing below it may import this.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotations only
    # The concrete readers are imported inside `build_services` so this module stays
    # import-light and the layering contract holds. For type checking they are needed by
    # name, and `from __future__ import annotations` keeps the runtime cost at zero.
    from tazama.infrastructure.db.kpi_repo import (
        SqlAlarmReader,
        SqlComplaintReader,
        SqlKpiReader,
    )

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.core.clock import Clock, SystemClock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies, get_policies
from tazama.core.security import Pseudonymiser
from tazama.core.settings import Settings, get_settings
from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.infrastructure.db.audit_repo import SqlAuditSink
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.eventbus import SessionOutboxWriter
from tazama.modules.audit.models import Evidence
from tazama.modules.audit.service import AuditService
from tazama.modules.correlation.service import CorrelationService
from tazama.modules.enrichment.service import EnrichmentService
from tazama.modules.impact.service import ImpactService
from tazama.modules.incidents.service import IncidentService
from tazama.modules.ingestion.service import IngestionService
from tazama.modules.notification.service import NotificationService
from tazama.modules.prioritisation.service import PrioritisationService
from tazama.modules.recovery.service import RecoveryService
from tazama.modules.routing.service import RoutingService
from tazama.modules.site_identification.service import SiteIdentificationService
from tazama.modules.verification.service import VerificationService

_log = get_logger(__name__)


class DecisionLogAdapter:
    """Adapts `AuditService` to the `DecisionLog` port every module declares.

    Two small differences to bridge: `AuditService.record` returns the `Decision` it wrote
    (the port returns nothing), and it types `evidence` as audit's own `Evidence` while
    each module passes its own evidence objects. Those expose `to_dict()`, which is the
    shape `Decision.to_row()` needs, so anything with that method passes straight through.
    """

    __slots__ = ("_audit",)

    def __init__(self, audit: AuditService) -> None:
        self._audit = audit

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Sequence[Any] = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None:
        self._audit.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=outcome,
            inputs=dict(inputs),
            evidence=[as_evidence(item) for item in evidence],
            confidence=confidence,
            rule_version=rule_version,
        )


def as_evidence(item: Any) -> Evidence:
    """Coerce a module's evidence object (or a dict) into an audit `Evidence`."""
    if isinstance(item, Evidence):
        return item
    to_dict = getattr(item, "to_dict", None)
    data = to_dict() if callable(to_dict) else item
    if isinstance(data, dict):
        return Evidence(
            kind=str(data.get("kind", "")),
            ref=str(data.get("ref", "")),
            note=str(data.get("note", "")),
            weight=float(data.get("weight", 1.0)),
        )
    # Keep the decision rather than lose it over an unexpected evidence shape.
    return Evidence(kind="unknown", ref=str(item)[:80])


@dataclass(slots=True)
class Services:
    """Every service, built around one session. Attributes are created eagerly: they are
    cheap objects over the same session, and a half-built graph is harder to reason about
    than a complete one."""

    session: Session
    clock: Clock
    policies: Policies
    settings: Settings
    audit: AuditService
    decisions: DecisionLogAdapter
    outbox: SessionOutboxWriter
    ingestion: IngestionService
    enrichment: EnrichmentService
    site_identification: SiteIdentificationService
    correlation: CorrelationService
    incidents: IncidentService
    impact: ImpactService
    prioritisation: PrioritisationService
    routing: RoutingService
    notification: NotificationService
    recovery: RecoveryService
    verification: VerificationService


class _SubscriberPositions:
    """Maps ingestion's subscriber snapshot onto what site identification needs.

    Modules share no DTOs, so the translation belongs to the composition root rather than
    giving one module a dependency on another's vocabulary.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get(self, subscriber_id: str) -> Any:
        from tazama.modules.site_identification.models import SubscriberPosition

        snapshot = self._inner.get(subscriber_id)
        if snapshot is None:
            return None
        return SubscriberPosition(
            id=snapshot.id,
            region=snapshot.region,
            home_cell_id=snapshot.home_cell_id,
            last_cell_id=snapshot.last_cell_id,
            last_seen_at=snapshot.last_seen_at,
            is_roaming=snapshot.is_roaming,
        )


class _VerificationKpis:
    """Adapts `SqlKpiReader` to verification's `KpiReader` port.

    The vocabularies genuinely differ and neither is wrong: the repository speaks
    `cell_id` and `KpiName` because that is what the table holds, while verification
    speaks `element_id` and `str` because it verifies recovery of *elements*, which may be
    a cell today and a sector tomorrow. Translating here is what a composition root is
    for — and it is not cosmetic: the port is keyword-only with different parameter names,
    so passing the repository directly raised `TypeError` on the first call. Verification
    had never run against a real adapter.
    """

    __slots__ = ("_reader",)

    def __init__(self, reader: SqlKpiReader) -> None:
        self._reader = reader

    def baseline(self, *, element_id: str, kpi: str, window: TimeWindow) -> float | None:
        value = self._reader.baseline(element_id, _kpi_name(kpi), window)
        return None if value is None else float(value)

    def current(self, *, element_id: str, kpi: str, window: TimeWindow) -> float | None:
        # The repository has one windowed mean; "baseline" and "current" are the *windows*,
        # not different maths. Verification keeps them as separate methods so that a caller
        # cannot accidentally compare a window with itself.
        value = self._reader.baseline(element_id, _kpi_name(kpi), window)
        return None if value is None else float(value)


class _VerificationAlarms:
    """Adapts `SqlAlarmReader` to verification's `AlarmReader` port."""

    __slots__ = ("_reader",)

    def __init__(self, reader: SqlAlarmReader) -> None:
        self._reader = reader

    def open_alarms(self, *, element_ids: Sequence[str], at: datetime) -> tuple[str, ...]:
        """Ids of alarms still raised at `at`.

        `at` is a point, and the repository reads windows, so the lookback below is the
        translation. An hour is enough to catch an alarm raised before the quiet period
        began and still open — which is exactly the case verification must not miss, since
        a still-raised alarm means the fault has not cleared.
        """
        window = TimeWindow(start=at - timedelta(hours=1), end=at + timedelta(minutes=1))
        open_ids: list[str] = []
        for element_id in element_ids:
            open_ids.extend(
                record.alarm_id
                for record in self._reader.for_element(element_id, window)
                if record.cleared_at is None or record.cleared_at > at
            )
        return tuple(dict.fromkeys(open_ids))


class _VerificationComplaints:
    """Adapts `SqlComplaintReader` to verification's `ComplaintCounter` port."""

    __slots__ = ("_reader",)

    def __init__(self, reader: SqlComplaintReader) -> None:
        self._reader = reader

    def count_since(self, *, element_ids: Sequence[str], since: datetime) -> int:
        """Network complaints on these elements since `since`.

        The window ends a minute in the future rather than at `since`: `TimeWindow` is
        half-open and refuses a zero-length window, and a complaint arriving in the same
        second as the check should count.
        """
        if not element_ids:
            return 0
        window = TimeWindow(start=since, end=since + timedelta(days=30))
        wanted = set(element_ids)
        return sum(
            1
            for observation in self._reader.in_window(window, tuple(wanted))
            if observation.cell_id in wanted
        )


def _kpi_name(kpi: str) -> KpiName:
    """Verification names KPIs as strings; the repository needs the enum."""
    return kpi if isinstance(kpi, KpiName) else KpiName(kpi)


class _VerificationIncidents:
    """Projects the incident aggregate onto the view verification reads.

    Verification declares its own `IncidentView` precisely so it does not depend on the
    incidents module's vocabulary; the projection is this root's job. `breached_kpis` comes
    from the KPI facts for the affected cells, because that is what verification has to
    check has recovered.
    """

    __slots__ = ("_facts", "_links", "_store")

    def __init__(self, store: Any, links: Any, facts: Any) -> None:
        self._store = store
        self._links = links
        self._facts = facts

    def get(self, incident_id: str) -> Any:
        from tazama.modules.verification.models import IncidentView

        incident = self._store.get(incident_id)
        if incident is None:
            return None
        cells = tuple(self._links.cells_for(incident_id))
        breached: tuple[str, ...] = ()
        window = getattr(incident, "window", None)
        if cells and window is not None:
            try:
                breached = tuple(str(k) for k in self._facts.breached_kpis(cells, window))
            except Exception as exc:  # a missing KPI read must not block verification
                _log.warning(
                    "breached_kpis_unavailable",
                    extra={"incident_id": incident_id, "error": str(exc)},
                )
        return IncidentView(
            id=incident.id,
            status=str(incident.status),
            region=incident.region,
            detected_at=incident.detected_at,
            started_at=getattr(incident, "started_at", None),
            resolved_at=getattr(incident, "resolved_at", None),
            affected_elements=cells,
            breached_kpis=breached,
            cause=None if incident.cause is None else str(incident.cause),
        )


def build_services(
    session: Session,
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    policies: Policies | None = None,
) -> Services:
    """Wire every module to its adapters around one session."""
    settings = settings or get_settings()
    clock = clock or SystemClock()
    policies = policies or get_policies()

    from tazama.infrastructure.adapters.sms import LiveStubSmsGateway, MockSmsGateway
    from tazama.infrastructure.db.incident_repo import (
        CorrelationIncidentBridge,
        SqlIncidentLinkStore,
        SqlIncidentStore,
    )
    from tazama.infrastructure.db.ingestion_repo import (
        SqlCareCallRepository,
        SqlComplaintRepository,
        SqlSubscriberRepository,
    )
    from tazama.infrastructure.db.kpi_repo import (
        SqlAlarmReader,
        SqlCellDirectory,
        SqlCellStats,
        SqlComplaintReader,
        SqlKpiFacts,
        SqlKpiReader,
        SqlMaintenanceReader,
        SqlSubscriberStats,
    )
    from tazama.infrastructure.db.kpi_repo import (
        SqlTopologyReader as SqlCorrelationTopology,
    )
    from tazama.infrastructure.db.notification_repo import (
        SqlNotificationRepository,
        SqlSubscriberAudience,
    )
    from tazama.infrastructure.db.recovery_repo import SqlRecoveryRepository
    from tazama.infrastructure.db.topology_repo import SqlSiteDirectory, SqlTopologyReader
    from tazama.modules.notification.templates import FileTemplateRepository
    from tazama.modules.recovery.models import FileRunbookRepository

    audit = AuditService(sink=SqlAuditSink(session=session), clock=clock)
    decisions = DecisionLogAdapter(audit)
    outbox = SessionOutboxWriter(session, clock)
    subscribers = SqlSubscriberRepository(session=session)

    incident_store = SqlIncidentStore(session=session)
    # The link store stamps `linked_at`, so it needs the same clock as everything else —
    # without it a link cannot be written at all.
    link_store = SqlIncidentLinkStore(session=session, clock=clock)
    incidents = IncidentService(
        store=incident_store,
        links=link_store,
        publisher=outbox,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )
    bridge = CorrelationIncidentBridge(incidents)

    # `SMS_GATEWAY_MODE=mock` is the only value the settings model accepts in v1; the live
    # stub is wired so the branch exists, and it refuses to send rather than pretending.
    gateway = MockSmsGateway() if settings.SMS_GATEWAY_MODE == "mock" else LiveStubSmsGateway()

    return Services(
        session=session,
        clock=clock,
        policies=policies,
        settings=settings,
        audit=audit,
        decisions=decisions,
        outbox=outbox,
        ingestion=IngestionService(
            complaints=SqlComplaintRepository(session=session),
            care_calls=SqlCareCallRepository(session=session),
            subscribers=subscribers,
            publisher=outbox,
            decisions=decisions,
            pseudonymiser=Pseudonymiser(settings.PSEUDONYM_SALT.get_secret_value()),
            clock=clock,
            policies=policies,
        ),
        enrichment=EnrichmentService(
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
        site_identification=SiteIdentificationService(
            subscribers=_SubscriberPositions(subscribers),
            directory=SqlSiteDirectory(session=session),
            topology=SqlTopologyReader(session=session),
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
        correlation=CorrelationService(
            kpis=SqlKpiReader(session=session),
            alarms=SqlAlarmReader(session=session),
            maintenance=SqlMaintenanceReader(session=session),
            complaints=SqlComplaintReader(session=session),
            cells=SqlCellDirectory(session=session),
            topology=SqlCorrelationTopology(session=session),
            incidents=bridge,
            links=bridge,
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
        incidents=incidents,
        impact=ImpactService(
            incident_cells=link_store,
            cells=SqlCellStats(session=session),
            subscribers=SqlSubscriberStats(session=session),
            kpis=SqlKpiFacts(session=session),
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
        prioritisation=PrioritisationService(
            policies=policies,
            clock=clock,
            decisions=decisions,
            events=outbox,
        ),
        routing=RoutingService(
            policies=policies,
            clock=clock,
            decisions=decisions,
            events=outbox,
        ),
        notification=NotificationService(
            repository=SqlNotificationRepository(session=session),
            audience=SqlSubscriberAudience(session=session),
            templates=FileTemplateRepository(),
            gateway=gateway,
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
        recovery=RecoveryService(
            runbooks=FileRunbookRepository(),
            repository=SqlRecoveryRepository(session=session),
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
        verification=VerificationService(
            incidents=_VerificationIncidents(
                incident_store,
                link_store,
                SqlKpiFacts(session=session),
            ),
            kpis=_VerificationKpis(SqlKpiReader(session=session)),
            alarms=_VerificationAlarms(SqlAlarmReader(session=session)),
            complaints=_VerificationComplaints(SqlComplaintReader(session=session)),
            publisher=outbox,
            decisions=decisions,
            clock=clock,
            policies=policies,
        ),
    )


@contextmanager
def unit_of_work(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    policies: Policies | None = None,
) -> Iterator[Services]:
    """One transaction, one set of services. Commits on success, rolls back on error."""
    settings = settings or get_settings()
    session = session_factory(engine or get_engine(settings))()
    try:
        services = build_services(session, settings=settings, clock=clock, policies=policies)
        yield services
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
