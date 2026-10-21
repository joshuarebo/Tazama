"""Composition root for the API: the only place that constructs adapters.

Modules declare ports; this module decides which implementation satisfies them, based on
settings. Nothing else in the codebase builds an adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, Request

from tazama.core.auth import verify_token
from tazama.core.clock import Clock, SystemClock
from tazama.core.errors import Unauthenticated
from tazama.core.security import Principal, Pseudonymiser
from tazama.core.settings import Settings, get_settings


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
