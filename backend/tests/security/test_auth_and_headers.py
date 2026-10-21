"""Authentication, authorisation at the HTTP edge, and security headers."""

from __future__ import annotations

from datetime import UTC, datetime

import jwt
import pytest

from tazama.core.auth import authenticate_dev_user, issue_token, verify_token
from tazama.core.clock import FixedClock, SystemClock
from tazama.core.errors import Unauthenticated
from tazama.core.security import Principal

pytestmark = pytest.mark.security

SECRET = "test-jwt-secret-value-0123456789"

#: `verify_token` validates `exp` against the wall clock, so minting uses the real clock.
#: Domain logic elsewhere uses the injected FixedClock.
LIVE = SystemClock()


# ------------------------------------------------------------------ tokens


def test_round_trip() -> None:
    token = issue_token(
        Principal(subject="jane", role="noc_engineer", display_name="Jane"),
        secret=SECRET,
        clock=LIVE,
    )
    principal = verify_token(token, secret=SECRET)
    assert (principal.subject, principal.role) == ("jane", "noc_engineer")


def test_expired_token_is_refused() -> None:
    stale = issue_token(
        Principal(subject="jane", role="analyst"),
        secret=SECRET,
        clock=FixedClock(datetime(2020, 1, 1, tzinfo=UTC)),
        ttl_s=60,
    )
    with pytest.raises(Unauthenticated, match="expired"):
        verify_token(stale, secret=SECRET)


def test_future_dated_token_is_still_bounded() -> None:
    """A token minted far in the future is accepted only until its own expiry."""
    token = issue_token(
        Principal(subject="jane", role="analyst"),
        secret=SECRET,
        clock=FixedClock(datetime(2099, 1, 1, tzinfo=UTC)),
        ttl_s=60,
    )
    claims = jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        issuer="tazama",
        leeway=10**10,
    )
    assert claims["exp"] - claims["iat"] == 60


def test_wrong_signature_is_refused() -> None:
    token = issue_token(Principal(subject="jane", role="admin"), secret=SECRET, clock=LIVE)
    with pytest.raises(Unauthenticated):
        verify_token(token, secret="another-secret-entirely-01234567")


def test_alg_none_forgery_is_refused() -> None:
    forged = jwt.encode(
        {"iss": "tazama", "sub": "attacker", "role": "admin", "iat": 0, "exp": 9_999_999_999},
        key="",
        algorithm="none",
    )
    with pytest.raises(Unauthenticated):
        verify_token(forged, secret=SECRET)


def test_unknown_role_in_token_is_refused() -> None:
    forged = jwt.encode(
        {
            "iss": "tazama",
            "sub": "attacker",
            "role": "superuser",
            "iat": 1_700_000_000,
            "exp": 9_999_999_999,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(Unauthenticated):
        verify_token(forged, secret=SECRET)


def test_foreign_issuer_is_refused() -> None:
    forged = jwt.encode(
        {
            "iss": "evil",
            "sub": "attacker",
            "role": "admin",
            "iat": 1_700_000_000,
            "exp": 9_999_999_999,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(Unauthenticated):
        verify_token(forged, secret=SECRET)


def test_token_without_expiry_is_refused() -> None:
    forged = jwt.encode(
        {"iss": "tazama", "sub": "attacker", "role": "admin", "iat": 1_700_000_000},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(Unauthenticated):
        verify_token(forged, secret=SECRET)


# ------------------------------------------------------------- dev sign-in


def test_dev_login_happy_path() -> None:
    principal = authenticate_dev_user("noc", "noc", dev_users={"noc": "noc_engineer"})
    assert principal.role == "noc_engineer"


@pytest.mark.parametrize(
    ("username", "password"),
    [("noc", "wrong"), ("ghost", "ghost"), ("", "")],
)
def test_dev_login_failures_are_indistinguishable(username: str, password: str) -> None:
    with pytest.raises(Unauthenticated) as excinfo:
        authenticate_dev_user(username, password, dev_users={"noc": "noc_engineer"})
    # Same message for a bad password and an unknown user: no account enumeration.
    assert str(excinfo.value) == "Invalid credentials."


# ----------------------------------------------------------- the HTTP edge


def test_security_headers_are_present(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert "X-Correlation-ID" in headers


def test_hsts_only_over_tls(client) -> None:
    plain = client.get("/healthz")
    assert "Strict-Transport-Security" not in plain.headers
    behind_proxy = client.get("/healthz", headers={"x-forwarded-proto": "https"})
    assert behind_proxy.headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_whoami_requires_a_token(client) -> None:
    response = client.get("/api/v1/auth/whoami")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("/unauthenticated")


def test_whoami_rejects_a_forged_token(client) -> None:
    forged = jwt.encode(
        {
            "iss": "tazama",
            "sub": "attacker",
            "role": "admin",
            "iat": 1_700_000_000,
            "exp": 9_999_999_999,
        },
        "not-the-server-secret-0123456789",
        algorithm="HS256",
    )
    response = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_login_then_whoami(client) -> None:
    login = client.post("/api/v1/auth/login", json={"username": "care", "password": "care"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert login.json()["role"] == "care_agent"

    whoami = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"})
    assert whoami.status_code == 200
    body = whoami.json()
    assert body["role"] == "care_agent"
    assert "subscriber:lookup" in body["capabilities"]
    assert "explorer:query" not in body["capabilities"]


def test_login_rejects_malformed_username(client) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "care; DROP TABLE users--", "password": "care"},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/validation-failed")


def test_login_with_bad_password_is_401(client) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "care", "password": "nope"})
    assert response.status_code == 401


def test_oversized_body_is_refused(client) -> None:
    response = client.post(
        "/api/v1/auth/login",
        content=b"x" * 10,
        headers={"content-length": "2000000", "content-type": "application/json"},
    )
    assert response.status_code == 413


def test_correlation_id_is_echoed_and_sanitised(client) -> None:
    response = client.get("/healthz", headers={"X-Correlation-ID": "abc<script>123"})
    returned = response.headers["X-Correlation-ID"]
    assert "<" not in returned
    assert returned.startswith("abc")


def test_unknown_route_returns_problem_json(client) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
