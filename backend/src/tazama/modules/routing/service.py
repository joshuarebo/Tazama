"""Routing: a declared rule table, not a chain of ifs buried in a handler.

The table below is the routing policy. Reading it top to bottom tells you where any case
goes and why, and every decision stores the matched rule, so "why did this land with field
ops?" has an answer that does not require reading code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.domain.events import AggregateType, CaseRouted
from tazama.domain.value_objects import IncidentCause
from tazama.modules.routing.models import RoutingDecision, RoutingInputs, Team, sla_due
from tazama.modules.routing.ports import (
    CaseAssignmentWriter,
    DecisionLog,
    EventPublisher,
    TicketSink,
)

_log = get_logger(__name__)

Predicate = Callable[[RoutingInputs], bool]


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    matches: Predicate
    team: Team
    reason: str
    requires_human: bool = False
    escalate_to: Team | None = None


#: First match wins, so the order is the policy. Specific situations come before causes,
#: and causes before the catch-all.
RULES: tuple[Rule, ...] = (
    Rule(
        name="unresolvable-location",
        matches=lambda i: i.needs_human_location,
        team=Team.CARE_BACK_OFFICE,
        reason="the complaint could not be tied to a network element with enough confidence",
        requires_human=True,
    ),
    Rule(
        name="planned-maintenance",
        matches=lambda i: i.is_planned_maintenance or i.cause == IncidentCause.PLANNED_MAINTENANCE,
        team=Team.CHANGE_MANAGEMENT,
        reason="the degradation is covered by an approved maintenance window",
    ),
    Rule(
        name="non-network",
        matches=lambda i: not i.is_network or i.cause == IncidentCause.NON_NETWORK,
        team=Team.CARE_BACK_OFFICE,
        reason="the complaints are not about the network",
    ),
    Rule(
        name="billing-surge",
        matches=lambda i: (i.cause or "").startswith("billing"),
        team=Team.BILLING,
        reason="billing or bundle confusion, not a network fault",
    ),
    Rule(
        name="transmission",
        matches=lambda i: (
            i.cause == IncidentCause.TRANSMISSION_LINK_FAILURE
            or i.primary_node_kind == "transmission_link"
        ),
        team=Team.TRANSMISSION,
        reason="a transmission element is the shared cause across the affected sites",
        escalate_to=Team.FIELD_OPS,
    ),
    Rule(
        name="power",
        matches=lambda i: i.cause == IncidentCause.POWER_FAILURE,
        team=Team.POWER,
        reason="site power is the probable cause",
        escalate_to=Team.FIELD_OPS,
    ),
    Rule(
        name="cell-outage",
        matches=lambda i: i.cause == IncidentCause.CELL_OUTAGE,
        team=Team.FIELD_OPS,
        reason="a cell is down and needs someone on site",
    ),
    Rule(
        name="congestion",
        matches=lambda i: i.cause == IncidentCause.CONGESTION,
        team=Team.RAN_OPTIMISATION,
        reason="capacity, not a fault: parameters or capacity need attention",
    ),
    Rule(
        name="configuration-change",
        matches=lambda i: i.cause == IncidentCause.CONFIGURATION_CHANGE,
        team=Team.CHANGE_MANAGEMENT,
        reason="a recent configuration change lines up with the degradation",
    ),
    Rule(
        name="flapping",
        matches=lambda i: i.cause == IncidentCause.FLAPPING,
        team=Team.FIELD_OPS,
        reason="an element is flapping: intermittent hardware or transport",
    ),
    Rule(
        name="core",
        matches=lambda i: i.primary_node_kind in ("core", "aggregation"),
        team=Team.CORE_NETWORK,
        reason="the shared element sits in the core or aggregation layer",
    ),
    Rule(
        name="unknown-cause",
        matches=lambda i: i.cause in (None, "", IncidentCause.UNKNOWN),
        team=Team.RAN_OPTIMISATION,
        reason="no probable cause yet: RAN optimisation triages it",
        requires_human=True,
    ),
)

#: A P1 with enterprise exposure is escalated as well as assigned, so an SLA breach is
#: somebody's explicit problem rather than a queue position.
ENTERPRISE_ESCALATION_TEAM = Team.CORE_NETWORK


class RoutingService:
    def __init__(
        self,
        *,
        policies: Policies,
        clock: Clock,
        decisions: DecisionLog,
        events: EventPublisher,
        cases: CaseAssignmentWriter | None = None,
        tickets: TicketSink | None = None,
    ) -> None:
        self._policies = policies
        self._clock = clock
        self._decisions = decisions
        self._events = events
        self._cases = cases
        self._tickets = tickets

    def decide(self, inputs: RoutingInputs) -> RoutingDecision:
        """Match the rule table. Pure, so routing is testable without a database."""
        now = self._clock.now()
        for rule in RULES:
            if not rule.matches(inputs):
                continue
            escalate = rule.escalate_to
            reason = rule.reason
            if inputs.priority == "P1" and inputs.enterprise_accounts > 0 and escalate is None:
                escalate = ENTERPRISE_ESCALATION_TEAM
                reason = f"{reason}; escalated for enterprise SLA exposure"
            return RoutingDecision(
                team=rule.team,
                priority=inputs.priority,
                sla_due_at=sla_due(now, inputs.priority),
                reason=f"[{rule.name}] {reason}",
                rule_version=self._policies.rule_version,
                inputs=inputs,
                escalate_to=escalate,
                requires_human=rule.requires_human,
            )

        # The table ends with a catch-all, so this is unreachable — but a silent drop would
        # lose a case, so it fails loudly in logs and parks the case with a human.
        _log.error("routing_fell_through", extra={"inputs": inputs.to_dict()})
        return RoutingDecision(
            team=Team.CARE_BACK_OFFICE,
            priority=inputs.priority,
            sla_due_at=sla_due(now, inputs.priority),
            reason="[fallthrough] no routing rule matched; parked for a human",
            rule_version=self._policies.rule_version,
            inputs=inputs,
            requires_human=True,
        )

    def route_case(
        self,
        *,
        case_id: str,
        incident_id: str,
        inputs: RoutingInputs,
        summary: str = "",
    ) -> RoutingDecision:
        """Decide, persist, propose a ticket, audit and publish `CaseRouted`."""
        decision = self.decide(inputs)

        if self._cases is not None:
            self._cases.assign(case_id, decision)

        ticket_ref: str | None = None
        if self._tickets is not None:
            ticket_ref = self._tickets.create_or_update(
                case_id=case_id,
                team=str(decision.team),
                priority=decision.priority,
                summary=summary
                or f"{inputs.cause or 'unknown cause'} in {inputs.region or 'unknown region'}",
                details=decision.to_dict(),
            )

        self._decisions.record(
            action="route",
            entity_type="case",
            entity_id=case_id,
            outcome=str(decision.team),
            inputs={**decision.to_dict(), "ticket_ref": ticket_ref, "incident_id": incident_id},
            rule_version=decision.rule_version,
        )
        self._events.publish(
            CaseRouted(
                aggregate_type=AggregateType.CASE,
                aggregate_id=case_id,
                occurred_at=self._clock.now(),
                dedupe_key=f"{case_id}:{decision.team}:{decision.priority}",
                payload={
                    "incident_id": incident_id,
                    "team": str(decision.team),
                    "priority": decision.priority,
                    "sla_due_at": decision.sla_due_at.isoformat(),
                    "requires_human": decision.requires_human,
                    "ticket_ref": ticket_ref,
                    "reason": decision.reason,
                },
            ),
        )
        _log.info(
            "case_routed",
            extra={
                "case_id": case_id,
                "team": str(decision.team),
                "priority": decision.priority,
                "requires_human": decision.requires_human,
            },
        )
        return decision
