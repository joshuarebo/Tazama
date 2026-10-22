"""Audit DTOs: the "why" record behind every decision (PRD section 13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Evidence:
    """One piece of support for a decision. Always a reference, never a copy of the data.

    Storing `kind` + `ref` keeps audit rows small and keeps them honest: an auditor can go
    and read the row the decision was based on, instead of trusting a snapshot.
    """

    kind: str  # alarm | kpi | complaint | care_call | topology | maintenance | document
    ref: str
    note: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "note": self.note, "weight": self.weight}


@dataclass(frozen=True, slots=True)
class Decision:
    """A decision worth explaining later.

    `inputs` holds the values the rule or model actually saw, and the three version fields
    say which rule, model and prompt produced the outcome — without them, an audit row
    cannot be reproduced once the code has moved on.
    """

    actor: str
    actor_role: str | None
    action: str
    entity_type: str
    entity_id: str
    outcome: str
    occurred_at: datetime
    inputs: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    confidence: float | None = None
    rule_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    correlation_id: str | None = None
    client_ip: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "outcome": self.outcome,
            "inputs": dict(self.inputs),
            "evidence": [item.to_dict() for item in self.evidence],
            "confidence": self.confidence,
            "rule_version": self.rule_version,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "correlation_id": self.correlation_id,
            "client_ip": self.client_ip,
        }


#: Actor used when the system itself decided. Written out explicitly so an audit reader can
#: tell automation from a person at a glance.
SYSTEM_ACTOR = "system"
