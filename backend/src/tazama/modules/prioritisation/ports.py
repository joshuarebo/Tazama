"""Prioritisation ports.

`DecisionLog` and `EventPublisher` keep the same shape in every Phase 2 module, so the
composition root can pass one adapter to all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent


@runtime_checkable
class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> bool: ...


@runtime_checkable
class DecisionLog(Protocol):
    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Sequence[Any] = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None: ...


@runtime_checkable
class PriorityScorer(Protocol):
    """Optional ML score (Phase 3). Absent in Phase 2, and the rules stand alone.

    When a model is present its score is blended, never substituted: the rule remains
    explainable and the model version is recorded alongside the rule version.
    """

    def score(self, features: dict[str, float]) -> tuple[float, str]: ...


@runtime_checkable
class CaseWriter(Protocol):
    """Persists the priority decision on the case record."""

    def set_priority(
        self,
        case_id: str,
        *,
        priority: str,
        score: float,
        decision: dict[str, Any],
        rule_version: str,
        model_version: str | None,
    ) -> None: ...
