"""Ports for the impact module.

Every read is time-bounded or id-bounded, and the subscriber and enterprise counts are
aggregated in SQL: an incident on a transmission link can cover a few hundred cells and
tens of thousands of subscribers, and none of those rows belong in this process.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.domain.value_objects import KpiName, TimeWindow
from tazama.modules.impact.models import CellFacts, EnterpriseExposure


@runtime_checkable
class IncidentCellReader(Protocol):
    """Which cells an incident was actually built from (its `cell` links)."""

    def cells_for(self, incident_id: str) -> tuple[str, ...]: ...


@runtime_checkable
class CellStats(Protocol):
    def for_cells(self, cell_ids: Sequence[str]) -> tuple[CellFacts, ...]: ...


@runtime_checkable
class SubscriberStats(Protocol):
    def enterprise_exposure(self, cell_ids: Sequence[str]) -> EnterpriseExposure:
        """Enterprise accounts and subscribers homed on these cells, counted in SQL."""
        ...


@runtime_checkable
class KpiFacts(Protocol):
    def breached_kpis(
        self,
        cell_ids: Sequence[str],
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[KpiName, ...]:
        """Which KPI families breached on these cells in the window, aggregated in SQL."""
        ...


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
