"""Prioritisation DTOs.

Every score carries the inputs that produced it and the contribution of each factor, so a
P1 can be defended — or disputed — months later. That is the whole point of a transparent
rule over an opaque model at this stage (PRD section 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class PriorityInputs:
    """What prioritisation is allowed to look at.

    Deliberately small: anything not in here cannot influence priority, which keeps the
    decision explainable and keeps subscriber-level data out of it.
    """

    affected_subscribers: int = 0
    enterprise_accounts: int = 0
    duration_minutes: float = 0.0
    sla_risk: float = 0.0
    repeat_complaints: int = 0
    complaint_count: int = 0
    region: str | None = None
    cause: str | None = None
    is_silent_sufferer: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_subscribers": self.affected_subscribers,
            "enterprise_accounts": self.enterprise_accounts,
            "duration_minutes": self.duration_minutes,
            "sla_risk": self.sla_risk,
            "repeat_complaints": self.repeat_complaints,
            "complaint_count": self.complaint_count,
            "region": self.region,
            "cause": self.cause,
            "is_silent_sufferer": self.is_silent_sufferer,
        }


@dataclass(frozen=True, slots=True)
class FactorContribution:
    """One factor's share of the final score."""

    factor: str
    raw_value: float
    normalised: float
    weight: float

    @property
    def contribution(self) -> float:
        return round(self.normalised * self.weight, 4)

    def explain(self) -> str:
        return (
            f"{self.factor}: {self.raw_value:g} -> {self.normalised:.2f} "
            f"x weight {self.weight:.2f} = {self.contribution:.3f}"
        )


@dataclass(frozen=True, slots=True)
class PriorityDecision:
    score: float
    priority: str
    inputs: PriorityInputs
    contributions: tuple[FactorContribution, ...]
    rule_version: str
    model_version: str | None = None
    decided_at: datetime | None = None
    overrides: tuple[str, ...] = ()

    @property
    def explanation(self) -> str:
        lines = [f"score {self.score:.3f} -> {self.priority} ({self.rule_version})"]
        lines += [f"  {c.explain()}" for c in self.contributions]
        lines += [f"  override: {reason}" for reason in self.overrides]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "priority": self.priority,
            "inputs": self.inputs.to_dict(),
            "contributions": [
                {
                    "factor": c.factor,
                    "raw": c.raw_value,
                    "normalised": c.normalised,
                    "weight": c.weight,
                    "contribution": c.contribution,
                }
                for c in self.contributions
            ],
            "rule_version": self.rule_version,
            "model_version": self.model_version,
            "overrides": list(self.overrides),
        }


#: Values at which a factor counts as "as bad as it gets" for normalisation. They are
#: modelled for the seeded network and are UNVERIFIED against real traffic; they live here
#: rather than in the policy file because they are scaling constants, not judgements.
SATURATION: dict[str, float] = {
    "affected_subscribers": 50_000.0,
    "enterprise_accounts": 25.0,
    "duration_minutes": 240.0,
    "repeat_complaints": 50.0,
    "complaint_count": 200.0,
}
