"""Audit ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tazama.modules.audit.models import Decision


@runtime_checkable
class AuditSink(Protocol):
    """Append-only. There is deliberately no update or delete in this interface."""

    def append(self, decision: Decision) -> None: ...

    def for_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[Decision, ...]: ...

    def recent(
        self,
        *,
        since: datetime | None = None,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
    ) -> tuple[Decision, ...]: ...
