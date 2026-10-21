"""Composition root for the API: the only place that constructs adapters.

Modules declare ports; this module decides which implementation satisfies them, based on
settings. Nothing else in the codebase builds an adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request

from tazama.core.auth import verify_token
from tazama.core.clock import Clock, SystemClock
from tazama.core.errors import Unauthenticated
from tazama.core.security import Principal, Pseudonymiser
from tazama.core.settings import Settings, get_settings

if TYPE_CHECKING:  # avoids importing the module (and its heavy deps) at import time
    from tazama.modules.discovery.service import DiscoveryService


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


DiscoveryDep = Annotated["DiscoveryService", Depends(discovery_service)]
