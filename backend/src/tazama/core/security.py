"""Security primitives shared by every layer (CLAUDE.md section 6).

Contents: pseudonymisation, masking, RBAC, tokens, rate limiting, safe paths and output
sanitisation. Higher-level controls build on these:
  * the SQL guard lives in `modules/discovery`
  * the prompt-injection guard and output filters live in `ai/llm/redaction.py`
  * audit writing lives in `modules/audit`
"""

from __future__ import annotations

import base64
import hmac
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Final

from tazama.core.errors import Forbidden, RateLimited

# --------------------------------------------------------------------- RBAC

CARE_AGENT: Final = "care_agent"
NOC_ENGINEER: Final = "noc_engineer"
ANALYST: Final = "analyst"
MANAGER: Final = "manager"
ADMIN: Final = "admin"

ROLES: Final[tuple[str, ...]] = (CARE_AGENT, NOC_ENGINEER, ANALYST, MANAGER, ADMIN)

#: Capability -> roles allowed. Endpoints declare a capability; never a bare role check.
#: Least privilege: admin is listed explicitly rather than treated as a wildcard.
PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    "incident:read": frozenset({CARE_AGENT, NOC_ENGINEER, ANALYST, MANAGER, ADMIN}),
    "incident:approve": frozenset({NOC_ENGINEER, MANAGER, ADMIN}),
    "incident:write": frozenset({NOC_ENGINEER, ADMIN}),
    "complaint:read": frozenset({CARE_AGENT, NOC_ENGINEER, ANALYST, MANAGER, ADMIN}),
    "complaint:write": frozenset({CARE_AGENT, ADMIN}),
    "subscriber:lookup": frozenset({CARE_AGENT, ADMIN}),
    "notification:propose": frozenset({CARE_AGENT, NOC_ENGINEER, ADMIN}),
    "notification:approve": frozenset({MANAGER, NOC_ENGINEER, ADMIN}),
    "recovery:recommend": frozenset({NOC_ENGINEER, ADMIN}),
    "recovery:approve": frozenset({MANAGER, ADMIN}),
    "insights:read": frozenset({ANALYST, MANAGER, NOC_ENGINEER, ADMIN}),
    "explorer:query": frozenset({ANALYST, ADMIN}),
    "explorer:export": frozenset({ANALYST, ADMIN}),
    "catalog:read": frozenset({ANALYST, MANAGER, NOC_ENGINEER, ADMIN}),
    "report:read": frozenset({MANAGER, ANALYST, NOC_ENGINEER, ADMIN}),
    "report:send": frozenset({ADMIN}),
    "ai:ask": frozenset({NOC_ENGINEER, ANALYST, CARE_AGENT, MANAGER, ADMIN}),
    "ai:quality": frozenset({ANALYST, MANAGER, ADMIN}),
    "audit:read": frozenset({ADMIN, MANAGER}),
    "admin:manage": frozenset({ADMIN}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller. Authorisation is always decided server-side from this."""

    subject: str
    role: str
    display_name: str = ""

    def can(self, capability: str) -> bool:
        allowed = PERMISSIONS.get(capability)
        if allowed is None:
            # Unknown capability: deny. A typo must never silently grant access.
            return False
        return self.role in allowed

    def require(self, capability: str) -> None:
        if not self.can(capability):
            raise Forbidden("You do not have permission to perform this action.")


# ----------------------------------------------------------- pseudonymisation


class Pseudonymiser:
    """HMAC-SHA256 pseudonymisation of subscriber identifiers.

    The same input and salt always yield the same token (so analytics can group by
    subscriber), and the token cannot be reversed without the salt. Raw MSISDNs never
    cross the ingestion boundary: analytics, AI, logs, exports and the UI see tokens.
    """

    __slots__ = ("_salt",)

    def __init__(self, salt: str) -> None:
        if not salt:
            raise ValueError("PSEUDONYM_SALT must not be empty")
        self._salt = salt.encode("utf-8")

    def token(self, raw: str, *, namespace: str = "msisdn") -> str:
        normalised = unicodedata.normalize("NFKC", raw).strip()
        if not normalised:
            raise ValueError("cannot pseudonymise an empty identifier")
        digest = hmac.new(
            self._salt,
            f"{namespace}:{normalised}".encode(),
            sha256,
        ).digest()
        short = base64.b32encode(digest)[:16].decode("ascii")
        return f"SUB-{short}"

    def matches(self, token: str, raw: str, *, namespace: str = "msisdn") -> bool:
        """Constant-time comparison — avoids a timing oracle on lookup endpoints."""
        return hmac.compare_digest(token, self.token(raw, namespace=namespace))


def mask_msisdn(raw: str) -> str:
    """Last-resort display masking, e.g. `2547XXXXX678`. Prefer a pseudonym token."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 6:
        return "***"
    return f"{digits[:4]}{'X' * (len(digits) - 7)}{digits[-3:]}"


def mask_value(value: str, keep: int = 2) -> str:
    """Generic masking for catalog previews and CSV exports."""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


# --------------------------------------------------------------- rate limiting


@dataclass(slots=True)
class TokenBucket:
    """Sliding-window limiter. Used per principal for API, LLM and tool calls."""

    limit: int
    window_s: float = 60.0
    _hits: deque[float] = field(default_factory=deque)

    def allow(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_s
        while self._hits and self._hits[0] <= cutoff:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(current)
        return True

    def remaining(self, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_s
        while self._hits and self._hits[0] <= cutoff:
            self._hits.popleft()
        return max(0, self.limit - len(self._hits))


class RateLimiter:
    """Keyed collection of buckets with a bounded number of keys (no unbounded growth)."""

    def __init__(self, limit: int, window_s: float = 60.0, max_keys: int = 10_000) -> None:
        self._limit = limit
        self._window = window_s
        self._max_keys = max_keys
        self._buckets: dict[str, TokenBucket] = {}

    def check(self, key: str) -> None:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                # Shed the oldest key rather than grow without bound.
                self._buckets.pop(next(iter(self._buckets)))
            bucket = TokenBucket(limit=self._limit, window_s=self._window)
            self._buckets[key] = bucket
        if not bucket.allow():
            raise RateLimited(
                f"Rate limit of {self._limit} requests per {int(self._window)}s exceeded.",
                retryAfter=int(self._window),
            )

    def remaining(self, key: str) -> int:
        bucket = self._buckets.get(key)
        return self._limit if bucket is None else bucket.remaining()


# ------------------------------------------------------------------ safe paths


def safe_path(base: Path, candidate: str) -> Path:
    """Resolve `candidate` under `base`, refusing traversal and absolute escapes."""
    if not candidate or candidate.strip() != candidate:
        raise Forbidden("Invalid file name.")
    if "\x00" in candidate:
        raise Forbidden("Invalid file name.")
    base_resolved = base.resolve()
    target = (base_resolved / candidate).resolve()
    if base_resolved != target and base_resolved not in target.parents:
        raise Forbidden("Path escapes the permitted directory.")
    return target


# ---------------------------------------------------------- output sanitisation

_SCRIPTISH = re.compile(
    r"(?is)<\s*(script|iframe|object|embed|style|svg|math)\b.*?(?:>.*?<\s*/\s*\1\s*>|/?>)",
)
_EVENT_ATTR = re.compile(r"(?i)\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)")
_DANGEROUS_URI = re.compile(r"(?i)\b(javascript|vbscript|data)\s*:")
_HTML_TAG = re.compile(r"<[^>]{0,200}>")


def sanitise_output(text: str, *, max_len: int = 5000, strip_html: bool = True) -> str:
    """Strip active content from anything rendered to a user or posted to Teams.

    Defence in depth: the frontend escapes by default, but model and complaint text can
    reach a card, a CSV or a log, so it is cleaned here too.
    """
    cleaned = _SCRIPTISH.sub("", text)
    cleaned = _EVENT_ATTR.sub("", cleaned)
    cleaned = _DANGEROUS_URI.sub("blocked:", cleaned)
    if strip_html:
        cleaned = _HTML_TAG.sub("", cleaned)
    cleaned = cleaned.replace("\x00", "")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + " […truncated]"
    return cleaned


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
