"""problem+json rendering (PRD section 8)."""

from __future__ import annotations

import pytest

from tazama.core.errors import (
    AgentBudgetExceeded,
    AiDegraded,
    Conflict,
    Forbidden,
    IdempotencyConflict,
    InjectionDetected,
    NotFound,
    PolicyViolation,
    QueryRejected,
    RateLimited,
    ReadOnlyViolation,
    TazamaError,
    Unauthenticated,
    ValidationFailed,
)


def test_problem_document_shape() -> None:
    problem = NotFound("Incident INC_1 does not exist.").to_problem(
        instance="/api/v1/incidents/INC_1",
        correlation_id="CID1",
    )
    assert problem["type"] == "https://tazama.internal/problems/not-found"
    assert problem["title"] == "Not found"
    assert problem["status"] == 404
    assert problem["detail"] == "Incident INC_1 does not exist."
    assert problem["instance"] == "/api/v1/incidents/INC_1"
    assert problem["correlationId"] == "CID1"


def test_extra_fields_are_carried() -> None:
    error = ValidationFailed("bad", errors=[{"field": "msisdn", "message": "invalid"}])
    problem = error.to_problem()
    assert problem["errors"][0]["field"] == "msisdn"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ValidationFailed("x"), 422, "validation-failed"),
        (Unauthenticated("x"), 401, "unauthenticated"),
        (Forbidden("x"), 403, "forbidden"),
        (NotFound("x"), 404, "not-found"),
        (Conflict("x"), 409, "conflict"),
        (IdempotencyConflict("x"), 409, "idempotency-conflict"),
        (RateLimited("x"), 429, "rate-limited"),
        (QueryRejected("x"), 400, "query-rejected"),
        (ReadOnlyViolation("x"), 403, "read-only"),
        (PolicyViolation("x"), 403, "policy-violation"),
        (AiDegraded("x"), 503, "ai-degraded"),
        (AgentBudgetExceeded("x"), 504, "agent-budget-exceeded"),
        (InjectionDetected("x"), 400, "injection-detected"),
    ],
)
def test_status_and_code_mapping(error: TazamaError, status: int, code: str) -> None:
    assert error.status == status
    assert error.code == code
    assert error.to_problem()["type"].endswith(f"/{code}")


def test_forbidden_message_does_not_enumerate() -> None:
    message = Forbidden("You do not have permission to perform this action.").detail
    assert "role" not in message.lower()
    assert "admin" not in message.lower()


def test_ai_degraded_is_an_upstream_failure() -> None:
    assert AiDegraded("llm down").status == 503
