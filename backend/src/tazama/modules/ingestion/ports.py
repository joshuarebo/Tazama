"""Ports for the ingestion module.

Declared here, in the consumer, and satisfied structurally by adapters in
`infrastructure/`. Ingestion therefore imports no module and no infrastructure, and the
service is testable with in-memory fakes.

`DecisionLog` is deliberately identical, byte for byte, to the port of the same name in
`modules/site_identification/ports.py`: the composition root passes one adapter to every
module, so the signatures must not drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.modules.ingestion.models import (
    CareCallRecord,
    ComplaintRecord,
    StoredCareCall,
    StoredComplaint,
    SubscriberSnapshot,
)


@runtime_checkable
class ComplaintRepository(Protocol):
    """Reads and writes `complaint`. The service never sees SQL."""

    def insert(self, record: ComplaintRecord) -> str:
        """Write the row and return its id."""
        ...

    def find_by_dedupe_key(self, dedupe_key: str) -> StoredComplaint | None:
        """The complaint already stored under this computed key, if any."""
        ...

    def find_by_idempotency_key(self, idempotency_key: str) -> StoredComplaint | None:
        """The complaint already stored under this caller-supplied key, if any.

        Takes the *raw* key: the adapter applies `idempotency_dedupe_key` so the
        namespacing rule lives in one place.
        """
        ...


@runtime_checkable
class CareCallRepository(Protocol):
    """Reads and writes `care_call`."""

    def insert(self, record: CareCallRecord) -> str: ...

    def find_by_dedupe_key(self, dedupe_key: str) -> StoredCareCall | None: ...

    def find_by_idempotency_key(self, idempotency_key: str) -> StoredCareCall | None: ...


@runtime_checkable
class SubscriberRepository(Protocol):
    """Pseudonymised subscriber state. There is no raw-identifier method, by design."""

    def get(self, subscriber_id: str) -> SubscriberSnapshot | None: ...

    def upsert_seen(
        self,
        *,
        subscriber_id: str,
        seen_at: datetime,
        region: str | None = None,
        cell_id: str | None = None,
        msisdn_format: str | None = None,
    ) -> None:
        """Record that this subscriber was seen, creating the row if it is new.

        Must never move `last_seen_at` backwards: late and out-of-order events are normal
        here, and a replayed day-old complaint must not overwrite today's serving cell.
        """
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Appends a domain event to the outbox, in the caller's transaction.

    Returns False when the event was already published — `DomainEvent.event_id` is
    deterministic, so a retried ingest is a no-op rather than a duplicate.
    """

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
