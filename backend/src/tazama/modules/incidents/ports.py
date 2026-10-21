"""Ports for the incidents module.

`IncidentStore.update` carries `expected_version` because the incident aggregate is
written from several directions at once — the network detector, the complaint path, the
verification worker — and a lost update there is a lost outage. The adapter turns the
expected version into `UPDATE ... WHERE version = :expected` and raises `Conflict` when
no row matched.

`DecisionLog` and `EventPublisher` keep the same shape in every Phase 2 module, so the
composition root can pass one adapter to all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.domain.value_objects import TimeWindow
from tazama.modules.incidents.models import Incident, IncidentLink, SimilarityQuery


@runtime_checkable
class IncidentStore(Protocol):
    def get(self, incident_id: str) -> Incident | None: ...

    def insert(self, incident: Incident) -> Incident:
        """Insert, or return the row that already exists under the same deterministic id."""
        ...

    def update(self, incident: Incident, *, expected_version: int) -> Incident:
        """Persist a change. Raises `Conflict` when the stored version has moved on."""
        ...

    def find_by_nodes(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[Incident, ...]:
        """Incidents rooted on any of these elements whose window overlaps `window`."""
        ...

    def find_similar(self, query: SimilarityQuery) -> tuple[Incident, ...]:
        """Structured-filter similarity, newest first."""
        ...


@runtime_checkable
class LinkStore(Protocol):
    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        """Idempotent on (incident, entity_type, entity). False when it already existed."""
        ...

    def for_incident(self, incident_id: str) -> tuple[IncidentLink, ...]: ...

    def move(self, *, from_incident_id: str, to_incident_id: str) -> int:
        """Re-point links during a merge. Returns how many moved. Deletes nothing."""
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
