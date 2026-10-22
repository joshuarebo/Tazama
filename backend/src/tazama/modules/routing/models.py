"""Routing DTOs: which team owns this, by when, and why."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class Team(StrEnum):
    """Queues a case can land in. Named after the work, not the org chart."""

    TRANSMISSION = "transmission"
    FIELD_OPS = "field_ops"
    RAN_OPTIMISATION = "ran_optimisation"
    CORE_NETWORK = "core_network"
    POWER = "power"
    CARE_BACK_OFFICE = "care_back_office"
    BILLING = "billing"
    CHANGE_MANAGEMENT = "change_management"
    DATA_QUALITY = "data_quality"


#: SLA per priority, in minutes to first meaningful action. Modelled values: an operator
#: would take these from its own SLA schedule, so they live in one visible place.
SLA_MINUTES: dict[str, int] = {"P1": 30, "P2": 120, "P3": 480, "P4": 1440}


@dataclass(frozen=True, slots=True)
class RoutingInputs:
    cause: str | None
    priority: str
    region: str | None = None
    primary_node_kind: str | None = None
    is_network: bool = True
    is_planned_maintenance: bool = False
    needs_human_location: bool = False
    enterprise_accounts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause": self.cause,
            "priority": self.priority,
            "region": self.region,
            "primary_node_kind": self.primary_node_kind,
            "is_network": self.is_network,
            "is_planned_maintenance": self.is_planned_maintenance,
            "needs_human_location": self.needs_human_location,
            "enterprise_accounts": self.enterprise_accounts,
        }


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    team: Team
    priority: str
    sla_due_at: datetime
    reason: str
    rule_version: str
    inputs: RoutingInputs
    escalate_to: Team | None = None
    requires_human: bool = False

    @property
    def sla_minutes(self) -> int:
        return SLA_MINUTES.get(self.priority, SLA_MINUTES["P4"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "team": str(self.team),
            "priority": self.priority,
            "sla_due_at": self.sla_due_at.isoformat(),
            "sla_minutes": self.sla_minutes,
            "reason": self.reason,
            "rule_version": self.rule_version,
            "escalate_to": str(self.escalate_to) if self.escalate_to else None,
            "requires_human": self.requires_human,
            "inputs": self.inputs.to_dict(),
        }


def sla_due(decided_at: datetime, priority: str) -> datetime:
    return decided_at + timedelta(minutes=SLA_MINUTES.get(priority, SLA_MINUTES["P4"]))
