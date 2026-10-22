"""Composition root for the API: the only place that constructs adapters.

Modules declare ports; this module decides which implementation satisfies them, based on
settings. Nothing else in the codebase builds an adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from tazama.core.auth import verify_token
from tazama.core.clock import Clock, SystemClock
from tazama.core.errors import Unauthenticated
from tazama.core.policy import Policies, get_policies
from tazama.core.security import Principal, Pseudonymiser
from tazama.core.settings import Settings, get_settings
from tazama.infrastructure.eventbus import SessionOutboxWriter
from tazama.modules.audit.models import Evidence
from tazama.modules.audit.service import AuditService
from tazama.modules.discovery.service import DiscoveryService
from tazama.modules.ingestion.service import IngestionService
from tazama.modules.notification.service import NotificationService
from tazama.modules.recovery.service import RecoveryService
from tazama.modules.site_identification.service import SiteIdentificationService


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


@lru_cache(maxsize=1)
def _clock() -> Clock:
    return SystemClock()


def clock_dep() -> Clock:
    return _clock()


ClockDep = Annotated[Clock, Depends(clock_dep)]


@lru_cache(maxsize=1)
def _pseudonymiser(salt: str) -> Pseudonymiser:
    return Pseudonymiser(salt)


def pseudonymiser_dep(settings: SettingsDep) -> Pseudonymiser:
    return _pseudonymiser(settings.PSEUDONYM_SALT.get_secret_value())


PseudonymiserDep = Annotated[Pseudonymiser, Depends(pseudonymiser_dep)]


def current_principal(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the caller from the bearer token. No token, no access."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("Sign in to continue.")
    token = authorization.split(" ", 1)[1].strip()
    return verify_token(token, secret=settings.JWT_SECRET.get_secret_value())


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def requires(capability: str) -> Callable[[Principal], Principal]:
    """Endpoint guard: `dependencies=[Depends(requires("incident:approve"))]`.

    Every mutating or sensitive endpoint declares a capability. Authorisation is decided
    here, on the server, from the token — never from a client-supplied role.
    """

    def _guard(principal: PrincipalDep) -> Principal:
        principal.require(capability)
        return principal

    return _guard


def client_ip(request: Request) -> str:
    """Best-effort client address for audit rows (never used for authorisation)."""
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------- discovery


def build_discovery_service(settings: Settings | None = None) -> DiscoveryService:
    """Wire the discovery module to its adapters.

    This is the composition root in action: the module declared ports, infrastructure
    implemented them, and the two meet here and nowhere else. `scripts/catalog.py` calls
    this function too, so the CLI and the API cannot drift apart.
    """
    from tazama.infrastructure.db.discovery_repo import (
        SqlCatalogReader,
        SqlCoverageReader,
        SqlQueryAuditSink,
        SqlQueryRunner,
        SqlSavedQueryStore,
        SqlSeedProvenanceReader,
    )
    from tazama.infrastructure.db.engine import get_engine
    from tazama.infrastructure.db.models import EXPLORER_ALLOWLIST
    from tazama.modules.discovery.service import DiscoveryContext
    from tazama.modules.discovery.sql_guard import GuardConfig

    settings = settings or get_settings()
    engine = get_engine(settings)
    guard_config = GuardConfig(
        allowed_tables=EXPLORER_ALLOWLIST,
        max_rows=settings.SQL_EXPLORER_MAX_ROWS,
        timeout_s=settings.SQL_EXPLORER_TIMEOUT_S,
        dialect="oracle" if settings.DB_BACKEND == "oracle" else "sqlite",
        # Nobody holds an unmask capability in v1: PII is masked for every caller.
        allow_pii_columns=False,
    )
    return DiscoveryService(
        catalog=SqlCatalogReader(engine),
        coverage=SqlCoverageReader(engine),
        runner=SqlQueryRunner(engine),
        audit=SqlQueryAuditSink(engine),
        saved_queries=SqlSavedQueryStore(engine),
        seed_provenance=SqlSeedProvenanceReader(engine),
        clock=_clock(),
        context=DiscoveryContext(
            app_mode=settings.APP_MODE,
            db_backend=settings.DB_BACKEND,
            guard_config=guard_config,
        ),
    )


def discovery_service(settings: SettingsDep) -> DiscoveryService:
    return build_discovery_service(settings)


DiscoveryDep = Annotated[DiscoveryService, Depends(discovery_service)]


# ------------------------------------------------------- unit of work


def request_session(settings: SettingsDep) -> Iterator[Session]:
    """One transaction per request.

    Handing the same session to the repositories, the outbox writer and the audit sink is
    what makes "the state change and its events commit together" true at the HTTP edge,
    not just inside the worker. A failing request rolls all three back.
    """
    from tazama.infrastructure.db.engine import get_engine, session_factory

    session = session_factory(get_engine(settings))()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(request_session)]


def policies_dep() -> Policies:
    return get_policies()


PoliciesDep = Annotated[Policies, Depends(policies_dep)]


def audit_service(session: SessionDep, clock: ClockDep) -> AuditService:
    """Audit bound to the request's transaction, so a decision row rolls back with it."""
    from tazama.infrastructure.db.audit_repo import SqlAuditSink

    return AuditService(sink=SqlAuditSink(session=session), clock=clock)


AuditDep = Annotated[AuditService, Depends(audit_service)]


class DecisionLogAdapter:
    """Adapts `AuditService` to the `DecisionLog` port the modules declare.

    They differ in two small ways, and bridging them here is the composition root's job:
    `AuditService.record` returns the `Decision` it wrote (useful to a caller that wants
    it; the port does not), and it types `evidence` as audit's own `Evidence` while a
    module passes its own evidence objects. Each module's evidence exposes `to_dict()`,
    which is the shape `Decision.to_row()` needs, so anything with that method passes
    straight through and anything else is wrapped.
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
        inputs: dict[str, object],
        evidence: Sequence[object] = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None:
        self._audit.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=outcome,
            inputs=dict(inputs),
            evidence=[_as_evidence(item) for item in evidence],
            confidence=confidence,
            rule_version=rule_version,
        )


def _as_evidence(item: object) -> Evidence:
    if isinstance(item, Evidence):
        return item
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return Evidence(
            kind=str(data.get("kind", "")),
            ref=str(data.get("ref", "")),
            note=str(data.get("note", "")),
            weight=float(data.get("weight", 1.0)),
        )
    if isinstance(item, dict):
        return Evidence(
            kind=str(item.get("kind", "")),
            ref=str(item.get("ref", "")),
            note=str(item.get("note", "")),
            weight=float(item.get("weight", 1.0)),
        )
    # Last resort: keep the decision rather than lose it over an unexpected shape.
    return Evidence(kind="unknown", ref=str(item)[:80])


def decision_log(audit: AuditDep) -> DecisionLogAdapter:
    return DecisionLogAdapter(audit)


DecisionLogDep = Annotated[DecisionLogAdapter, Depends(decision_log)]


def outbox_writer(session: SessionDep, clock: ClockDep) -> SessionOutboxWriter:
    return SessionOutboxWriter(session, clock)


OutboxDep = Annotated[SessionOutboxWriter, Depends(outbox_writer)]


# ------------------------------------------------------------ ingestion


def ingestion_service(
    settings: SettingsDep,
    session: SessionDep,
    clock: ClockDep,
    decisions: DecisionLogDep,
    outbox: OutboxDep,
    policies: PoliciesDep,
) -> IngestionService:
    from tazama.infrastructure.db.ingestion_repo import (
        SqlCareCallRepository,
        SqlComplaintRepository,
        SqlSubscriberRepository,
    )

    return IngestionService(
        complaints=SqlComplaintRepository(session=session),
        care_calls=SqlCareCallRepository(session=session),
        subscribers=SqlSubscriberRepository(session=session),
        publisher=outbox,
        decisions=decisions,
        pseudonymiser=_pseudonymiser(settings.PSEUDONYM_SALT.get_secret_value()),
        clock=clock,
        policies=policies,
    )


IngestionDep = Annotated[IngestionService, Depends(ingestion_service)]


def site_identification_service(
    session: SessionDep,
    clock: ClockDep,
    decisions: DecisionLogDep,
    outbox: OutboxDep,
    policies: PoliciesDep,
) -> SiteIdentificationService:
    from tazama.infrastructure.db.ingestion_repo import SqlSubscriberRepository
    from tazama.infrastructure.db.topology_repo import SqlSiteDirectory, SqlTopologyReader
    from tazama.modules.site_identification.models import SubscriberPosition

    class _Positions:
        """Maps the ingestion subscriber snapshot onto what site identification needs.

        Modules do not share DTOs, so the translation lives here in the composition root
        rather than giving one module a dependency on another's vocabulary.
        """

        def __init__(self, inner: SqlSubscriberRepository) -> None:
            self._inner = inner

        def get(self, subscriber_id: str) -> SubscriberPosition | None:
            snapshot = self._inner.get(subscriber_id)
            if snapshot is None:
                return None
            return SubscriberPosition(
                id=snapshot.id,
                home_cell_id=snapshot.home_cell_id,
                last_cell_id=snapshot.last_cell_id,
                last_seen_at=snapshot.last_seen_at,
                region=snapshot.region,
                is_roaming=snapshot.is_roaming,
            )

    return SiteIdentificationService(
        subscribers=_Positions(SqlSubscriberRepository(session=session)),
        directory=SqlSiteDirectory(session=session),
        topology=SqlTopologyReader(session=session),
        publisher=outbox,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )


SiteIdDep = Annotated[SiteIdentificationService, Depends(site_identification_service)]


# --------------------------------------------------------- notification


def notification_service(
    settings: SettingsDep,
    session: SessionDep,
    clock: ClockDep,
    decisions: DecisionLogDep,
    outbox: OutboxDep,
    policies: PoliciesDep,
) -> NotificationService:
    from tazama.infrastructure.adapters.sms import LiveStubSmsGateway, MockSmsGateway
    from tazama.infrastructure.db.notification_repo import (
        SqlNotificationRepository,
        SqlSubscriberAudience,
    )
    from tazama.modules.notification.templates import FileTemplateRepository

    # `SMS_GATEWAY_MODE=mock` is the only value the settings model accepts in v1; the live
    # stub is wired here so the branch exists and is exercised, and it refuses to send.
    gateway = MockSmsGateway() if settings.SMS_GATEWAY_MODE == "mock" else LiveStubSmsGateway()

    return NotificationService(
        repository=SqlNotificationRepository(session=session),
        audience=SqlSubscriberAudience(session=session),
        templates=FileTemplateRepository(),
        gateway=gateway,
        publisher=outbox,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )


NotificationDep = Annotated[NotificationService, Depends(notification_service)]


# ------------------------------------------------------------- recovery


def recovery_service(
    session: SessionDep,
    clock: ClockDep,
    decisions: DecisionLogDep,
    outbox: OutboxDep,
    policies: PoliciesDep,
) -> RecoveryService:
    from tazama.infrastructure.db.recovery_repo import SqlRecoveryRepository
    from tazama.modules.recovery.models import FileRunbookRepository

    return RecoveryService(
        runbooks=FileRunbookRepository(),
        repository=SqlRecoveryRepository(session=session),
        publisher=outbox,
        decisions=decisions,
        clock=clock,
        policies=policies,
    )


RecoveryDep = Annotated[RecoveryService, Depends(recovery_service)]
