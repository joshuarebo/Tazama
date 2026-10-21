"""Ports for site identification.

Declared in the consumer and satisfied structurally by adapters in `infrastructure/db`.
The `DecisionLog` protocol is byte-identical to the one in `modules/ingestion/ports.py`:
the composition root hands the same adapter to both.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.modules.site_identification.models import (
    CellInfo,
    NameMatch,
    SiteInfo,
    SubscriberPosition,
    TopologyHop,
)


@runtime_checkable
class SubscriberLookup(Protocol):
    """Where the network last saw a (pseudonymised) subscriber."""

    def get(self, subscriber_id: str) -> SubscriberPosition | None: ...


@runtime_checkable
class SiteDirectory(Protocol):
    """Names and geography of sites and cells."""

    def cells_for_region(self, region: str, *, limit: int = 200) -> tuple[CellInfo, ...]:
        """Active cells in a region, bounded. Used to widen a region-only match."""
        ...

    def search_by_name(
        self,
        tokens: Sequence[str],
        *,
        limit: int = 50,
    ) -> tuple[NameMatch, ...]:
        """Sites, cells and regions whose names contain any of these tokens.

        The tokens come from untrusted complaint text, so the adapter must bind them as
        parameters and escape LIKE metacharacters — never build a pattern by concatenation.
        Matching is intentionally generous; the service does the ranking.
        """
        ...

    def get_cell(self, cell_id: str) -> CellInfo | None: ...

    def get_site(self, site_id: str) -> SiteInfo | None: ...


@runtime_checkable
class TopologyReader(Protocol):
    """Reads the network graph: who shares a site, and what sits upstream."""

    def siblings_of_cell(self, cell_id: str, *, limit: int = 50) -> tuple[CellInfo, ...]:
        """Other cells on the same site as this one (the cell itself excluded)."""
        ...

    def cells_under_site(self, site_id: str, *, limit: int = 50) -> tuple[CellInfo, ...]: ...

    def upstream_of(self, node_id: str, *, max_depth: int = 6) -> tuple[TopologyHop, ...]:
        """Walk parent edges towards the core, nearest first.

        `max_depth` bounds the walk, which also makes a cyclic edge table harmless.
        """
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Appends a domain event to the outbox, in the caller's transaction."""

    def publish(self, event: DomainEvent) -> bool: ...


@runtime_checkable
class DecisionLog(Protocol):
    """Append-only record of why the system did what it did (PRD section 8, security 8)."""

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
