"""Ports for the notification module.

Everything the service needs from the outside world is a Protocol here, satisfied
structurally by an adapter wired in the composition root. The module therefore never
imports infrastructure, and the service is testable with in-memory fakes.

`EventPublisher` and `DecisionLog` keep the same shape in every Phase 2 module, so the
composition root can pass one adapter to all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.modules.notification.models import (
    AudienceMember,
    NotificationProposal,
    SendReceipt,
    SmsMessage,
    Template,
)


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
class NotificationRepository(Protocol):
    """Persistence for proposals, plus the counting queries the rate limits need.

    The counts are queries rather than a scan in the service: the limits must hold across
    processes and restarts, so they are answered by the store, not by in-process state.
    """

    def save(self, proposal: NotificationProposal) -> NotificationProposal: ...

    def get(self, notification_id: str) -> NotificationProposal | None: ...

    def for_incident(self, incident_id: str) -> tuple[NotificationProposal, ...]: ...

    def count_for_subscriber_in_incident(self, subscriber_id: str, incident_id: str) -> int:
        """Proposals that were not suppressed, for `max_per_incident`."""
        ...

    def count_for_subscriber_since(self, subscriber_id: str, since: datetime) -> int:
        """Proposals since `since`, for `max_per_subscriber_per_day`."""
        ...

    def find_recent_duplicate(
        self,
        *,
        subscriber_id: str,
        template_id: str,
        incident_id: str,
        since: datetime,
    ) -> NotificationProposal | None:
        """The same (subscriber, template, incident) inside the dedupe window, if any."""
        ...

    def count_proposed_since(self, since: datetime) -> int:
        """System-wide proposal count, for the storm guard."""
        ...


@runtime_checkable
class SubscriberAudience(Protocol):
    """Who may be told about an incident. Returns pseudonym tokens only."""

    def for_incident(
        self, incident_id: str, *, limit: int = 1000
    ) -> tuple[AudienceMember, ...]: ...

    def get(self, subscriber_id: str) -> AudienceMember | None: ...

    def is_opted_out(self, subscriber_id: str) -> bool: ...


@runtime_checkable
class TemplateRepository(Protocol):
    """The approved template catalogue. There is no `put`: templates are reviewed files."""

    def get(self, template_id: str) -> Template | None: ...

    def all(self) -> tuple[Template, ...]: ...


@runtime_checkable
class SmsGateway(Protocol):
    """The SMS egress boundary.

    `mode()` is part of the port because the service refuses to send through anything but
    `mock` in v1, and that refusal must be checkable without importing an adapter.
    """

    def mode(self) -> str: ...

    def send(self, message: SmsMessage) -> SendReceipt: ...
