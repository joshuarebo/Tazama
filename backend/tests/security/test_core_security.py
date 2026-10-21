"""Security primitives: pseudonymisation, RBAC, rate limits, paths, output filters."""

from __future__ import annotations

import pytest

from tazama.core.errors import Forbidden, RateLimited
from tazama.core.security import (
    ADMIN,
    ANALYST,
    CARE_AGENT,
    MANAGER,
    NOC_ENGINEER,
    PERMISSIONS,
    ROLES,
    Principal,
    Pseudonymiser,
    RateLimiter,
    TokenBucket,
    constant_time_equals,
    mask_msisdn,
    mask_value,
    safe_path,
    sanitise_output,
)

pytestmark = pytest.mark.security


# ---------------------------------------------------------- pseudonymisation


def test_pseudonym_is_deterministic(pseudonymiser: Pseudonymiser) -> None:
    assert pseudonymiser.token("254712345678") == pseudonymiser.token("254712345678")


def test_pseudonym_depends_on_salt() -> None:
    a = Pseudonymiser("salt-one-0123456789").token("254712345678")
    b = Pseudonymiser("salt-two-0123456789").token("254712345678")
    assert a != b


def test_pseudonym_does_not_contain_the_identifier(pseudonymiser: Pseudonymiser) -> None:
    token = pseudonymiser.token("254712345678")
    assert "712345678" not in token
    assert token.startswith("SUB-")
    assert len(token) == 20


def test_pseudonym_namespaces_are_separate(pseudonymiser: Pseudonymiser) -> None:
    assert pseudonymiser.token("X1", namespace="msisdn") != pseudonymiser.token(
        "X1",
        namespace="account",
    )


def test_pseudonym_matches_constant_time(pseudonymiser: Pseudonymiser) -> None:
    token = pseudonymiser.token("254712345678")
    assert pseudonymiser.matches(token, "254712345678")
    assert not pseudonymiser.matches(token, "254712345679")


def test_empty_identifier_is_refused(pseudonymiser: Pseudonymiser) -> None:
    with pytest.raises(ValueError, match="empty identifier"):
        pseudonymiser.token("   ")


def test_empty_salt_is_refused() -> None:
    with pytest.raises(ValueError, match="PSEUDONYM_SALT"):
        Pseudonymiser("")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("254712345678", "2547XXXXX678"), ("0712345678", "0712XXX678"), ("12", "***")],
)
def test_mask_msisdn(raw: str, expected: str) -> None:
    assert mask_msisdn(raw) == expected


def test_mask_value_keeps_a_prefix_only() -> None:
    assert mask_value("Nairobi") == "Na*****"
    assert mask_value("ab") == "**"


# -------------------------------------------------------------------- RBAC


def test_every_capability_lists_only_known_roles() -> None:
    for capability, roles in PERMISSIONS.items():
        assert roles, capability
        assert roles <= set(ROLES), capability


def test_unknown_capability_denies() -> None:
    principal = Principal(subject="u", role=ADMIN)
    assert principal.can("incident:teleport") is False
    with pytest.raises(Forbidden):
        principal.require("incident:teleport")


@pytest.mark.parametrize(
    ("role", "capability", "allowed"),
    [
        (CARE_AGENT, "subscriber:lookup", True),
        (CARE_AGENT, "explorer:query", False),
        (CARE_AGENT, "recovery:approve", False),
        (NOC_ENGINEER, "incident:approve", True),
        (NOC_ENGINEER, "report:send", False),
        (ANALYST, "explorer:query", True),
        (ANALYST, "subscriber:lookup", False),
        (ANALYST, "incident:write", False),
        (MANAGER, "notification:approve", True),
        (MANAGER, "explorer:query", False),
        (ADMIN, "admin:manage", True),
    ],
)
def test_role_matrix(role: str, capability: str, allowed: bool) -> None:
    assert Principal(subject="u", role=role).can(capability) is allowed


def test_least_privilege_sensitive_capabilities() -> None:
    """Only an admin may send a report or manage the platform."""
    assert PERMISSIONS["report:send"] == frozenset({ADMIN})
    assert PERMISSIONS["admin:manage"] == frozenset({ADMIN})
    # Nobody may approve recovery without being a manager or an admin.
    assert PERMISSIONS["recovery:approve"] <= frozenset({MANAGER, ADMIN})


# ----------------------------------------------------------- rate limiting


def test_token_bucket_blocks_over_the_limit() -> None:
    bucket = TokenBucket(limit=3, window_s=60)
    assert [bucket.allow(now=100.0) for _ in range(4)] == [True, True, True, False]
    assert bucket.remaining(now=100.0) == 0


def test_token_bucket_window_slides() -> None:
    bucket = TokenBucket(limit=2, window_s=10)
    assert bucket.allow(now=0.0) is True
    assert bucket.allow(now=1.0) is True
    assert bucket.allow(now=2.0) is False
    assert bucket.allow(now=11.5) is True


def test_rate_limiter_raises_with_retry_after() -> None:
    limiter = RateLimiter(limit=1, window_s=60)
    limiter.check("user-a")
    with pytest.raises(RateLimited) as excinfo:
        limiter.check("user-a")
    assert excinfo.value.extra["retryAfter"] == 60
    limiter.check("user-b")  # a different caller is unaffected


def test_rate_limiter_key_count_is_bounded() -> None:
    limiter = RateLimiter(limit=5, window_s=60, max_keys=10)
    for index in range(50):
        limiter.check(f"key-{index}")
    assert len(limiter._buckets) <= 10


# -------------------------------------------------------------- safe paths


@pytest.mark.parametrize(
    "candidate",
    [
        "../secrets.txt",
        "../../etc/passwd",
        "sub/../../escape.csv",
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        "export .csv ",
        "nul\x00.csv",
    ],
)
def test_path_traversal_is_refused(tmp_path, candidate: str) -> None:
    with pytest.raises(Forbidden):
        safe_path(tmp_path, candidate)


def test_safe_path_allows_a_plain_name(tmp_path) -> None:
    assert safe_path(tmp_path, "complaints.csv").parent == tmp_path.resolve()
    assert safe_path(tmp_path, "sub/complaints.csv").name == "complaints.csv"


# ------------------------------------------------------ output sanitisation


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<script src='x'></script>",
        "<IFRAME SRC=javascript:alert(1)></IFRAME>",
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        "<a href='javascript:alert(1)'>click</a>",
        "<object data='data:text/html;base64,PHNjcmlwdD4='></object>",
    ],
)
def test_active_content_is_stripped(payload: str) -> None:
    cleaned = sanitise_output(payload)
    lowered = cleaned.lower()
    assert "<script" not in lowered
    assert "<iframe" not in lowered
    assert "onerror" not in lowered
    assert "onload" not in lowered
    assert "javascript:" not in lowered


def test_sanitise_truncates_long_output() -> None:
    cleaned = sanitise_output("x" * 10_000, max_len=100)
    assert len(cleaned) < 150
    assert cleaned.endswith("[…truncated]")


def test_sanitise_keeps_ordinary_text() -> None:
    text = "Site NRB-012 availability dropped to 0% at 14:30 EAT. Habari, tuko site."
    assert sanitise_output(text) == text


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
