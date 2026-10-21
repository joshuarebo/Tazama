"""Authentication: dev users (AUTH_MODE=dev) and OIDC-shaped JWTs (AUTH_MODE=oidc).

Authorisation is never decided here — `Principal.require()` in `core.security` does that,
server-side, on every request. This module only establishes *who* is calling.
"""

from __future__ import annotations

from datetime import timedelta

import jwt

from tazama.core.clock import Clock
from tazama.core.errors import Unauthenticated
from tazama.core.security import ROLES, Principal

ALGORITHM = "HS256"
ISSUER = "tazama"


def issue_token(
    principal: Principal,
    *,
    secret: str,
    clock: Clock,
    ttl_s: int = 3600,
) -> str:
    """Mint a short-lived access token for a dev-mode login."""
    now = clock.now()
    payload = {
        "iss": ISSUER,
        "sub": principal.subject,
        "role": principal.role,
        "name": principal.display_name or principal.subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_s)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def verify_token(token: str, *, secret: str, leeway_s: int = 10) -> Principal:
    """Validate signature, issuer and expiry, then return the caller.

    Expiry is enforced (no "never expires" tokens) and the algorithm is pinned, so a
    forged `alg: none` header is rejected rather than trusted.
    """
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            leeway=leeway_s,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthenticated("Session expired. Sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthenticated("Invalid credentials.") from exc

    role = str(claims.get("role", ""))
    if role not in ROLES:
        raise Unauthenticated("Invalid credentials.")
    return Principal(
        subject=str(claims["sub"]),
        role=role,
        display_name=str(claims.get("name") or claims["sub"]),
    )


def authenticate_dev_user(
    username: str,
    password: str,
    *,
    dev_users: dict[str, str],
) -> Principal:
    """Dev-mode login.

    In dev mode the password must equal the username: it exists to exercise the login
    flow, not to protect anything. `AUTH_MODE=dev` is refused outside local/dev by the
    settings validator, so this can never be the production path.
    """
    role = dev_users.get(username)
    if role is None or password != username:
        # One message for both cases: no account enumeration.
        raise Unauthenticated("Invalid credentials.")
    return Principal(subject=username, role=role, display_name=username)
