"""Audit service: one way to record a decision, used by every other module.

Keeping this in one place is what makes "every decision is explainable" true rather than
aspirational — a module cannot write a decision without supplying an actor, the inputs, the
versions and the evidence, because the signature demands them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from tazama.core.clock import Clock
from tazama.core.ids import get_correlation_id
from tazama.core.logging import get_logger, scrub
from tazama.core.security import Principal
from tazama.modules.audit.models import SYSTEM_ACTOR, Decision, Evidence
from tazama.modules.audit.ports import AuditSink

_log = get_logger(__name__)

#: Input payloads are summaries, not dumps: an audit row is read by a person.
MAX_INPUT_KEYS = 40
MAX_EVIDENCE_ITEMS = 50


class AuditService:
    def __init__(self, *, sink: AuditSink, clock: Clock) -> None:
        self._sink = sink
        self._clock = clock

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        actor: str = SYSTEM_ACTOR,
        actor_role: str | None = None,
        inputs: dict[str, Any] | None = None,
        evidence: Sequence[Evidence] = (),
        confidence: float | None = None,
        rule_version: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        client_ip: str | None = None,
    ) -> Decision:
        """Append one decision. Inputs are scrubbed and bounded before storage."""
        trimmed = dict(list((inputs or {}).items())[:MAX_INPUT_KEYS])
        decision = Decision(
            actor=actor,
            actor_role=actor_role,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=outcome,
            occurred_at=self._clock.now(),
            # Audit rows are read widely, so identifiers and secrets are scrubbed here too.
            inputs=scrub(trimmed),
            evidence=tuple(evidence[:MAX_EVIDENCE_ITEMS]),
            confidence=confidence,
            rule_version=rule_version,
            model_version=model_version,
            prompt_version=prompt_version,
            correlation_id=get_correlation_id() or None,
            client_ip=client_ip,
        )
        self._sink.append(decision)
        _log.info(
            "decision_recorded",
            extra={
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "outcome": outcome,
                "actor": actor,
            },
        )
        return decision

    def record_human_action(
        self,
        *,
        principal: Principal,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> Decision:
        """Convenience for approvals and rejections: the actor is the signed-in person."""
        return self.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=outcome,
            actor=principal.subject,
            actor_role=principal.role,
            inputs=inputs,
            client_ip=client_ip,
        )

    def history(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[Decision, ...]:
        """Everything that happened to one entity, newest first."""
        return self._sink.for_entity(entity_type, entity_id, limit=limit)

    def recent(
        self,
        *,
        since: datetime | None = None,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
    ) -> tuple[Decision, ...]:
        return self._sink.recent(since=since, action=action, actor=actor, limit=limit)
