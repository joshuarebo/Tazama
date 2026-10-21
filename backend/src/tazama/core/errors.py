"""Problem+json error model (RFC 9457) used by every API response.

One exception hierarchy, one renderer. Messages are safe to show a user: they never
include SQL, stack frames, credentials or raw identifiers.
"""

from __future__ import annotations

from typing import Any

PROBLEM_CONTENT_TYPE = "application/problem+json"
_BASE_TYPE = "https://tazama.internal/problems"


class TazamaError(Exception):
    """Base class. Subclasses map 1:1 onto a problem type and an HTTP status."""

    status: int = 500
    code: str = "internal-error"
    title: str = "Internal error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def to_problem(self, instance: str | None = None, correlation_id: str = "") -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{_BASE_TYPE}/{self.code}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        if correlation_id:
            problem["correlationId"] = correlation_id
        problem.update(self.extra)
        return problem


class ValidationFailed(TazamaError):
    status, code, title = 422, "validation-failed", "Validation failed"


class BadRequest(TazamaError):
    status, code, title = 400, "bad-request", "Bad request"


class Unauthenticated(TazamaError):
    status, code, title = 401, "unauthenticated", "Authentication required"


class Forbidden(TazamaError):
    """Authorisation failure. Deliberately vague so it cannot be used to enumerate."""

    status, code, title = 403, "forbidden", "Not permitted"


class NotFound(TazamaError):
    status, code, title = 404, "not-found", "Not found"


class Conflict(TazamaError):
    status, code, title = 409, "conflict", "Conflict"


class IdempotencyConflict(Conflict):
    code, title = "idempotency-conflict", "Idempotency key reused with a different body"


class PayloadTooLarge(TazamaError):
    status, code, title = 413, "payload-too-large", "Payload too large"


class RateLimited(TazamaError):
    status, code, title = 429, "rate-limited", "Too many requests"


class QueryRejected(TazamaError):
    """The SQL guard refused a Data Explorer query. `reason` names the rule that fired."""

    status, code, title = 400, "query-rejected", "Query rejected by the SQL guard"


class ReadOnlyViolation(TazamaError):
    """A write was attempted against a read-only target (live mode protection)."""

    status, code, title = 403, "read-only", "Write refused: target is read-only"


class PolicyViolation(TazamaError):
    """A policy in config/policies.yaml refused the action (e.g. missing approval)."""

    status, code, title = 403, "policy-violation", "Refused by policy"


class UpstreamUnavailable(TazamaError):
    status, code, title = 503, "upstream-unavailable", "A dependency is unavailable"


class AiDegraded(UpstreamUnavailable):
    """The LLM path is unusable; the caller must take the deterministic fallback."""

    code, title = "ai-degraded", "AI is degraded"


class AgentBudgetExceeded(TazamaError):
    """A bounded agent hit its step, token or wall-clock budget."""

    status, code, title = 504, "agent-budget-exceeded", "Agent budget exceeded"


class InjectionDetected(TazamaError):
    """Untrusted text tried to behave like an instruction."""

    status, code, title = 400, "injection-detected", "Input rejected by the prompt guard"
