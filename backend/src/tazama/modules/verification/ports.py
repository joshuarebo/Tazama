"""Ports for the verification module.

Each reader answers one question about a time window, and nothing writes: verification
decides whether closure is earned, it does not change the world. The verdict leaves through
`EventPublisher` and `DecisionLog`, which keep the same shape in every Phase 2 module.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.domain.value_objects import TimeWindow
from tazama.modules.verification.models import IncidentView


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
class IncidentReader(Protocol):
    def get(self, incident_id: str) -> IncidentView | None: ...


@runtime_checkable
class KpiReader(Protocol):
    """Aggregated KPI values over a window, per element.

    Two methods rather than one with a flag: the baseline is read from a window *before*
    the incident and the current value from a window after it, and conflating them is the
    bug this module exists to prevent.
    """

    def baseline(self, *, element_id: str, kpi: str, window: TimeWindow) -> float | None:
        """Mean value over a pre-incident window, or None when there is no data."""
        ...

    def current(self, *, element_id: str, kpi: str, window: TimeWindow) -> float | None:
        """Mean value over the most recent window."""
        ...


@runtime_checkable
class AlarmReader(Protocol):
    def open_alarms(self, *, element_ids: Sequence[str], at: datetime) -> tuple[str, ...]:
        """Ids of alarms still raised on these elements at `at`."""
        ...


@runtime_checkable
class ComplaintCounter(Protocol):
    def count_since(self, *, element_ids: Sequence[str], since: datetime) -> int:
        """Network complaints received on these elements since `since`."""
        ...
