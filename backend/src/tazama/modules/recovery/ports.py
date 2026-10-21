"""Ports for the recovery module.

Read this list for what is **not** here: there is no port that executes a step, runs a
command, calls an NMS write API or dispatches a change. v1 recommends, approves and
simulates. Adding an execution port is a reviewed architectural change, not a feature, and
`tests/unit/test_recovery.py` asserts the absence.

`EventPublisher` and `DecisionLog` keep the same shape in every Phase 2 module, so the
composition root can pass one adapter to all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.modules.recovery.models import RecoveryProposal, Runbook


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
class RunbookRepository(Protocol):
    """The reviewed runbook catalogue. Read-only: runbooks are documents under review."""

    def get(self, runbook_id: str) -> Runbook | None: ...

    def for_cause(self, cause: str) -> tuple[Runbook, ...]: ...

    def all(self) -> tuple[Runbook, ...]: ...


@runtime_checkable
class RecoveryRepository(Protocol):
    """Persistence for proposals, their approval trail and their simulated outcome."""

    def save(self, proposal: RecoveryProposal) -> RecoveryProposal: ...

    def get(self, action_id: str) -> RecoveryProposal | None: ...

    def for_incident(self, incident_id: str) -> tuple[RecoveryProposal, ...]: ...
