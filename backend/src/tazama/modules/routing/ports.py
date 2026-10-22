"""Routing ports. Same `DecisionLog` / `EventPublisher` shape as every Phase 2 module."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.modules.routing.models import RoutingDecision


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
class CaseAssignmentWriter(Protocol):
    def assign(self, case_id: str, decision: RoutingDecision) -> None: ...


@runtime_checkable
class TicketSink(Protocol):
    """An external work-management system (ServiceNow in production, a mock in v1).

    Routing proposes; it never assumes the external system accepted. The adapter returns a
    reference so the two sides can be reconciled later (see tech-debt: dual state).
    """

    def create_or_update(
        self,
        *,
        case_id: str,
        team: str,
        priority: str,
        summary: str,
        details: dict[str, Any],
    ) -> str: ...
