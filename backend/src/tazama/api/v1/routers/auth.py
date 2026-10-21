"""Sign-in and whoami.

Dev mode issues a short-lived JWT for a known local user. OIDC mode is wired as a stub
that reports where to send the browser; token validation is shared with dev mode, so the
authorisation path does not fork.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field

from tazama.api.deps import ClockDep, PrincipalDep, SettingsDep
from tazama.core.auth import authenticate_dev_user, issue_token
from tazama.core.errors import BadRequest
from tazama.core.logging import get_logger
from tazama.core.security import PERMISSIONS

router = APIRouter(prefix="/auth", tags=["auth"])
_log = get_logger(__name__)


class LoginRequest(BaseModel):
    # Bounds are part of the input-validation layer, not a nicety.
    username: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth token type, not a credential
    expires_in: int
    role: str
    subject: str


class WhoAmI(BaseModel):
    subject: str
    role: str
    display_name: str
    capabilities: list[str]


@router.post("/login", response_model=TokenResponse, summary="Dev-mode sign in")
async def login(
    body: LoginRequest,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenResponse:
    if settings.AUTH_MODE != "dev":
        raise BadRequest("This deployment uses OIDC. Start the flow at /api/v1/auth/oidc.")
    principal = authenticate_dev_user(
        body.username,
        body.password,
        dev_users=settings.dev_users,
    )
    token = issue_token(
        principal,
        secret=settings.JWT_SECRET.get_secret_value(),
        clock=clock,
        ttl_s=settings.JWT_TTL_S,
    )
    _log.info("login_ok", extra={"subject": principal.subject, "role": principal.role})
    return TokenResponse(
        access_token=token,
        expires_in=settings.JWT_TTL_S,
        role=principal.role,
        subject=principal.subject,
    )


@router.get("/whoami", response_model=WhoAmI, summary="The authenticated caller")
async def whoami(principal: PrincipalDep) -> WhoAmI:
    return WhoAmI(
        subject=principal.subject,
        role=principal.role,
        display_name=principal.display_name,
        capabilities=sorted(cap for cap, roles in PERMISSIONS.items() if principal.role in roles),
    )


class OidcInfo(BaseModel):
    mode: str
    issuer: str
    client_id: str
    note: str


@router.get("/oidc", response_model=OidcInfo, summary="OIDC configuration (no secrets)")
async def oidc_info(settings: SettingsDep) -> OidcInfo:
    return OidcInfo(
        mode=settings.AUTH_MODE,
        issuer=settings.OIDC_ISSUER,
        client_id=settings.OIDC_CLIENT_ID,
        note=(
            "Authorization-code + PKCE against the issuer above; the resulting id token is "
            "validated by core.auth.verify_token. Configure OIDC_* and set AUTH_MODE=oidc."
        ),
    )
