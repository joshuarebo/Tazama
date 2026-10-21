"""Prioritisation and routing: every decision must be explainable and reproducible."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest

from tazama.core.policy import load_policies
from tazama.domain.events import EventType
from tazama.domain.value_objects import IncidentCause
from tazama.infrastructure.eventbus import InMemoryEventBus
from tazama.modules.prioritisation.models import PriorityInputs
from tazama.modules.prioritisation.service import PrioritisationService
from tazama.modules.routing.models import SLA_MINUTES, RoutingInputs, Team
from tazama.modules.routing.service import RULES, RoutingService


class FakeDecisionLog:
    """Records what would have been audited, in the shared `DecisionLog` shape."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

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
    ) -> None:
        self.entries.append(
            {
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "outcome": outcome,
                "inputs": inputs,
                "evidence": list(evidence),
                "confidence": confidence,
                "rule_version": rule_version,
            },
        )


class FakeCaseWriter:
    def __init__(self) -> None:
        self.assigned: dict[str, Any] = {}

    def set_priority(
        self,
        case_id: str,
        *,
        priority: str,
        score: float,
        decision: dict[str, Any],
        rule_version: str,
        model_version: str | None,
    ) -> None:
        self.assigned[case_id] = {
            "priority": priority,
            "score": score,
            "rule_version": rule_version,
            "model_version": model_version,
            "decision": decision,
        }

    def assign(self, case_id: str, decision: Any) -> None:
        self.assigned[case_id] = decision


class FakeTicketSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_or_update(
        self,
        *,
        case_id: str,
        team: str,
        priority: str,
        summary: str,
        details: dict[str, Any],
    ) -> str:
        self.calls.append(
            {
                "case_id": case_id,
                "team": team,
                "priority": priority,
                "summary": summary,
                "details": details,
            },
        )
        return f"TKT-{len(self.calls):04d}"


@pytest.fixture
def policies(repo_root):
    return load_policies(repo_root / "config" / "policies.yaml")


@pytest.fixture
def prioritiser(policies, frozen_clock):
    return PrioritisationService(
        policies=policies,
        clock=frozen_clock,
        decisions=FakeDecisionLog(),
        events=InMemoryEventBus(),
    )


# ----------------------------------------------------------- prioritisation


def test_quiet_incident_is_low_priority(prioritiser) -> None:
    decision = prioritiser.score(PriorityInputs(affected_subscribers=120, duration_minutes=5))
    assert decision.priority == "P4"
    assert decision.score < 0.35


def test_major_outage_is_p1(prioritiser) -> None:
    decision = prioritiser.score(
        PriorityInputs(
            affected_subscribers=48_000,
            enterprise_accounts=20,
            duration_minutes=230,
            sla_risk=0.85,
            repeat_complaints=40,
            complaint_count=180,
        ),
    )
    assert decision.priority == "P1"
    assert decision.score >= 0.80


def test_score_is_bounded_and_monotonic(prioritiser) -> None:
    smaller = prioritiser.score(PriorityInputs(affected_subscribers=1_000, duration_minutes=10))
    bigger = prioritiser.score(PriorityInputs(affected_subscribers=20_000, duration_minutes=60))
    assert 0.0 <= smaller.score <= 1.0
    assert 0.0 <= bigger.score <= 1.0
    assert bigger.score > smaller.score


def test_saturation_caps_absurd_inputs(prioritiser) -> None:
    """A million affected subscribers must not produce a score above 1."""
    decision = prioritiser.score(
        PriorityInputs(
            affected_subscribers=5_000_000,
            enterprise_accounts=5_000,
            duration_minutes=100_000,
            sla_risk=5.0,
            repeat_complaints=9_999,
            complaint_count=50_000,
        ),
    )
    assert decision.score <= 1.0
    assert all(c.normalised <= 1.0 for c in decision.contributions)


def test_every_weighted_factor_is_explained(prioritiser, policies) -> None:
    decision = prioritiser.score(PriorityInputs(affected_subscribers=5_000))
    assert {c.factor for c in decision.contributions} == set(policies.prioritisation.weights)
    assert "score" in decision.explanation
    for contribution in decision.contributions:
        assert contribution.explain() in decision.explanation


def test_contributions_sum_to_the_score(prioritiser) -> None:
    decision = prioritiser.score(
        PriorityInputs(affected_subscribers=10_000, enterprise_accounts=3, duration_minutes=60),
    )
    assert decision.score == pytest.approx(
        sum(c.contribution for c in decision.contributions),
        abs=1e-3,
    )


def test_sla_risk_with_enterprise_forces_p1(prioritiser) -> None:
    decision = prioritiser.score(
        PriorityInputs(affected_subscribers=200, enterprise_accounts=2, sla_risk=0.95),
    )
    assert decision.priority == "P1"
    assert any("SLA risk" in o for o in decision.overrides)


def test_sla_risk_without_enterprise_does_not_force_p1(prioritiser) -> None:
    decision = prioritiser.score(PriorityInputs(affected_subscribers=200, sla_risk=0.95))
    assert decision.priority != "P1"
    assert decision.overrides == ()


def test_total_outage_floor(prioritiser) -> None:
    decision = prioritiser.score(PriorityInputs(affected_subscribers=400), total_outage=True)
    assert decision.priority == "P2"
    assert any("total outage" in o for o in decision.overrides)


def test_silent_sufferers_are_not_left_at_p4(prioritiser) -> None:
    decision = prioritiser.score(
        PriorityInputs(affected_subscribers=300, complaint_count=0, is_silent_sufferer=True),
    )
    assert decision.priority == "P3"
    assert any("no complaints" in o for o in decision.overrides)


def test_decision_records_the_rule_version(prioritiser, policies) -> None:
    assert prioritiser.score(PriorityInputs()).rule_version == policies.rule_version


def test_scoring_is_deterministic(prioritiser) -> None:
    inputs = PriorityInputs(affected_subscribers=7_777, duration_minutes=33, sla_risk=0.4)
    assert prioritiser.score(inputs).score == prioritiser.score(inputs).score


def test_unknown_weight_is_survivable(policies, frozen_clock) -> None:
    """A typo in the policy file must not take the pipeline down."""
    broken = policies.prioritisation.weights | {"moon_phase": 0.0}
    service = PrioritisationService(
        policies=policies.__class__(
            version=policies.version,
            updated=policies.updated,
            owner=policies.owner,
            approvals=policies.approvals,
            confidence=policies.confidence,
            correlation=policies.correlation,
            prioritisation=type(policies.prioritisation)(
                weights=broken,
                p1_threshold=policies.prioritisation.p1_threshold,
                p2_threshold=policies.prioritisation.p2_threshold,
                p3_threshold=policies.prioritisation.p3_threshold,
            ),
            notification=policies.notification,
            verification=policies.verification,
            ai=policies.ai,
        ),
        clock=frozen_clock,
        decisions=FakeDecisionLog(),
        events=InMemoryEventBus(),
    )
    decision = service.score(PriorityInputs(affected_subscribers=1_000))
    assert any(c.factor == "moon_phase" and c.contribution == 0.0 for c in decision.contributions)


def test_model_blend_is_capped(policies, frozen_clock) -> None:
    class Scorer:
        def score(self, features: dict[str, float]) -> tuple[float, str]:
            return 1.0, "surge-v9"

    service = PrioritisationService(
        policies=policies,
        clock=frozen_clock,
        decisions=FakeDecisionLog(),
        events=InMemoryEventBus(),
        scorer=Scorer(),
        model_blend=0.9,  # asks for 90%
    )
    rule_only = PrioritisationService(
        policies=policies,
        clock=frozen_clock,
        decisions=FakeDecisionLog(),
        events=InMemoryEventBus(),
    ).score(PriorityInputs(affected_subscribers=1_000))
    blended = service.score(PriorityInputs(affected_subscribers=1_000))
    # The model can move the score by at most MAX_MODEL_BLEND, and is recorded.
    assert blended.model_version == "surge-v9"
    assert blended.score - rule_only.score <= 0.31


def test_prioritise_incident_persists_audits_and_publishes(policies, frozen_clock) -> None:
    log, bus, cases = FakeDecisionLog(), InMemoryEventBus(), FakeCaseWriter()
    service = PrioritisationService(
        policies=policies,
        clock=frozen_clock,
        decisions=log,
        events=bus,
        cases=cases,
    )
    decision = service.prioritise_incident(
        incident_id="INC-1",
        case_id="CASE-1",
        inputs=PriorityInputs(affected_subscribers=30_000, enterprise_accounts=4, sla_risk=0.5),
    )
    assert cases.assigned["CASE-1"]["priority"] == decision.priority
    assert log.entries[0]["action"] == "prioritise"
    assert log.entries[0]["rule_version"] == policies.rule_version
    assert bus.types() == (EventType.CASE_PRIORITIZED,)
    assert bus.events[0].payload["priority"] == decision.priority


def test_prioritisation_is_idempotent_on_the_bus(policies, frozen_clock) -> None:
    bus = InMemoryEventBus()
    service = PrioritisationService(
        policies=policies,
        clock=frozen_clock,
        decisions=FakeDecisionLog(),
        events=bus,
    )
    inputs = PriorityInputs(affected_subscribers=30_000)
    service.prioritise_incident(incident_id="INC-1", inputs=inputs)
    service.prioritise_incident(incident_id="INC-1", inputs=inputs)
    # Same fact, same deterministic event id: published once.
    assert len(bus) == 1


# ------------------------------------------------------------------ routing


@pytest.fixture
def router(policies, frozen_clock):
    return RoutingService(
        policies=policies,
        clock=frozen_clock,
        decisions=FakeDecisionLog(),
        events=InMemoryEventBus(),
    )


@pytest.mark.parametrize(
    ("inputs", "team"),
    [
        (
            RoutingInputs(cause=IncidentCause.TRANSMISSION_LINK_FAILURE, priority="P1"),
            Team.TRANSMISSION,
        ),
        (RoutingInputs(cause=IncidentCause.CELL_OUTAGE, priority="P2"), Team.FIELD_OPS),
        (RoutingInputs(cause=IncidentCause.CONGESTION, priority="P3"), Team.RAN_OPTIMISATION),
        (RoutingInputs(cause=IncidentCause.POWER_FAILURE, priority="P2"), Team.POWER),
        (
            RoutingInputs(cause=IncidentCause.CONFIGURATION_CHANGE, priority="P3"),
            Team.CHANGE_MANAGEMENT,
        ),
        (RoutingInputs(cause=IncidentCause.FLAPPING, priority="P3"), Team.FIELD_OPS),
        (
            RoutingInputs(cause=IncidentCause.PLANNED_MAINTENANCE, priority="P4"),
            Team.CHANGE_MANAGEMENT,
        ),
        (RoutingInputs(cause=IncidentCause.NON_NETWORK, priority="P4"), Team.CARE_BACK_OFFICE),
        (RoutingInputs(cause=None, priority="P3"), Team.RAN_OPTIMISATION),
        (
            RoutingInputs(cause=IncidentCause.UNKNOWN, priority="P2", primary_node_kind="core"),
            Team.CORE_NETWORK,
        ),
    ],
)
def test_routing_table(router, inputs: RoutingInputs, team: Team) -> None:
    assert router.decide(inputs).team is team


def test_unresolvable_location_goes_to_a_human_first(router) -> None:
    decision = router.decide(
        RoutingInputs(
            cause=IncidentCause.CELL_OUTAGE,
            priority="P1",
            needs_human_location=True,
        ),
    )
    # The most specific rule wins over the cause.
    assert decision.team is Team.CARE_BACK_OFFICE
    assert decision.requires_human is True


def test_non_network_never_reaches_a_network_team(router) -> None:
    decision = router.decide(RoutingInputs(cause=None, priority="P2", is_network=False))
    assert decision.team is Team.CARE_BACK_OFFICE


def test_enterprise_p1_is_escalated(router) -> None:
    decision = router.decide(
        RoutingInputs(cause=IncidentCause.CELL_OUTAGE, priority="P1", enterprise_accounts=3),
    )
    assert decision.escalate_to is not None
    assert "enterprise SLA" in decision.reason


def test_transmission_escalates_to_field_ops(router) -> None:
    decision = router.decide(
        RoutingInputs(cause=IncidentCause.TRANSMISSION_LINK_FAILURE, priority="P1"),
    )
    assert decision.escalate_to is Team.FIELD_OPS


@pytest.mark.parametrize(("priority", "minutes"), sorted(SLA_MINUTES.items()))
def test_sla_due_follows_priority(router, frozen_clock, priority: str, minutes: int) -> None:
    decision = router.decide(RoutingInputs(cause=IncidentCause.CELL_OUTAGE, priority=priority))
    assert decision.sla_due_at - frozen_clock.now() == timedelta(minutes=minutes)
    assert decision.sla_minutes == minutes


def test_reason_names_the_matched_rule(router) -> None:
    reason = router.decide(RoutingInputs(cause=IncidentCause.CONGESTION, priority="P3")).reason
    assert reason.startswith("[congestion]")


def test_rule_names_are_unique() -> None:
    names = [rule.name for rule in RULES]
    assert len(names) == len(set(names))


def test_every_cause_is_routed(router) -> None:
    """No cause may fall through to the error path."""
    for cause in IncidentCause:
        decision = router.decide(RoutingInputs(cause=cause, priority="P3"))
        assert "fallthrough" not in decision.reason, cause


def test_route_case_persists_audits_tickets_and_publishes(policies, frozen_clock) -> None:
    log, bus, cases, tickets = (
        FakeDecisionLog(),
        InMemoryEventBus(),
        FakeCaseWriter(),
        FakeTicketSink(),
    )
    service = RoutingService(
        policies=policies,
        clock=frozen_clock,
        decisions=log,
        events=bus,
        cases=cases,
        tickets=tickets,
    )
    decision = service.route_case(
        case_id="CASE-1",
        incident_id="INC-1",
        inputs=RoutingInputs(cause=IncidentCause.TRANSMISSION_LINK_FAILURE, priority="P1"),
        summary="Link down, 12 sites affected",
    )
    assert cases.assigned["CASE-1"] is decision
    assert tickets.calls[0]["team"] == "transmission"
    assert log.entries[0]["action"] == "route"
    assert log.entries[0]["inputs"]["ticket_ref"] == "TKT-0001"
    assert bus.types() == (EventType.CASE_ROUTED,)
    assert bus.events[0].payload["team"] == "transmission"


def test_routing_without_a_ticket_sink_still_works(router) -> None:
    """The external work system is optional; TAZAMA must not depend on it being up."""
    decision = router.route_case(
        case_id="CASE-2",
        incident_id="INC-2",
        inputs=RoutingInputs(cause=IncidentCause.CELL_OUTAGE, priority="P2"),
    )
    assert decision.team is Team.FIELD_OPS
