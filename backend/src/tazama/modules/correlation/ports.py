"""Ports for the correlation module.

Everything correlation needs from outside is a Protocol here, satisfied structurally by an
adapter in `infrastructure/`. The module never imports an adapter, so the engine can be
driven entirely by in-memory fakes in a unit test and by SQL in production.

Two rules the adapters must honour, because the engine cannot check them:
  * every read is **time-bounded** — a window is always part of the signature;
  * KPI health is **aggregated in SQL**. `breaches()` returns one row per cell, never the
    835k underlying samples.

`DecisionLog` and `EventPublisher` have the same shape in every Phase 2 module, so the
composition root passes one adapter to all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.domain.value_objects import KpiName, NodeKind, TimeWindow
from tazama.modules.correlation.models import (
    AlarmRecord,
    AlarmTransition,
    CellInfo,
    ComplaintCount,
    ComplaintObservation,
    IncidentCandidate,
    IncidentRef,
    KpiBreach,
    KpiSample,
    MaintenanceCover,
    TopologyNodeInfo,
)


@runtime_checkable
class KpiReader(Protocol):
    """Cell KPIs. Breach detection happens in the database, not in this process."""

    def series(
        self,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> tuple[KpiSample, ...]:
        """One KPI's samples for one cell, ascending by bucket."""
        ...

    def breaches(
        self,
        window: TimeWindow,
        granularity: str = "1h",
        *,
        cell_ids: Sequence[str] | None = None,
        min_severity: float = 0.0,
    ) -> tuple[KpiBreach, ...]:
        """Per-cell KPI health for the window, one row per cell.

        `min_severity` is the share of KPI samples that must breach before a cell is
        reported at all; it is what keeps a single noisy KPI out of the engine.
        """
        ...

    def baseline(
        self,
        cell_id: str,
        kpi: KpiName,
        window: TimeWindow,
        granularity: str = "1h",
    ) -> float | None:
        """Mean value of one KPI over a reference window, or None when there is no data."""
        ...


@runtime_checkable
class AlarmReader(Protocol):
    def active(self, window: TimeWindow) -> tuple[AlarmRecord, ...]:
        """Alarms raised in, or still open across, the window."""
        ...

    def for_element(self, element_id: str, window: TimeWindow) -> tuple[AlarmRecord, ...]: ...

    def transitions(self, element_id: str, window: TimeWindow) -> tuple[AlarmTransition, ...]:
        """Raise and clear events on one element, ascending — the flapping input."""
        ...


@runtime_checkable
class MaintenanceReader(Protocol):
    def covering(self, element_id: str, window: TimeWindow) -> tuple[MaintenanceCover, ...]:
        """Approved windows on this element whose time overlaps `window`."""
        ...


@runtime_checkable
class ComplaintReader(Protocol):
    def in_window(
        self,
        window: TimeWindow,
        cell_ids: Sequence[str] | None = None,
    ) -> tuple[ComplaintObservation, ...]:
        """Located complaints whose **event time** falls in the window."""
        ...

    def counts_by_cell(self, window: TimeWindow) -> tuple[ComplaintCount, ...]:
        """Raw and distinct-by-dedupe-key counts per cell, aggregated in SQL."""
        ...

    def baseline_rate(self, cell_id: str, window: TimeWindow) -> float:
        """Complaints per hour for this cell over a reference window.

        Per-cell, not global: a quiet rural cell and a busy Nairobi cell have nothing to
        say to each other, and "silence as signal" is only meaningful against a cell's own
        recent history.
        """
        ...


@runtime_checkable
class CellDirectory(Protocol):
    def get(self, cell_id: str) -> CellInfo | None: ...

    def by_region(self, region: str) -> tuple[CellInfo, ...]: ...

    def with_geo(self, cell_ids: Sequence[str]) -> tuple[CellInfo, ...]: ...


@runtime_checkable
class TopologyReader(Protocol):
    def upstream_of(self, node_id: str) -> tuple[TopologyNodeInfo, ...]:
        """Ancestors nearest-first: cell -> site -> link -> aggregation -> core."""
        ...

    def descendants_of(self, node_id: str, kind: NodeKind | None = None) -> tuple[str, ...]:
        """Every node below this one, optionally restricted to one kind."""
        ...

    def shared_upstream(self, cell_ids: Sequence[str]) -> TopologyNodeInfo | None:
        """The nearest element every one of these cells hangs off, if there is one."""
        ...


@runtime_checkable
class IncidentRepository(Protocol):
    """Correlation's view of the incidents module (wired to it in the composition root)."""

    def open_or_update(self, candidate: IncidentCandidate) -> IncidentRef:
        """Idempotent on `candidate.key`: a replay converges on the same incident."""
        ...

    def find_candidates(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[IncidentRef, ...]:
        """Open or recently closed incidents rooted on any of these elements."""
        ...

    def reopen(self, incident_id: str, *, reason: str) -> IncidentRef: ...


@runtime_checkable
class IncidentLinkRepository(Protocol):
    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        """Idempotent. Returns False when the link already existed."""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> bool: ...


@runtime_checkable
class DecisionLog(Protocol):
    """Append-only "why" record. Identical signature in every Phase 2 module."""

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
