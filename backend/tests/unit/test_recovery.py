"""Recovery module: runbook catalogue, recommendation, approval, simulation, rollback.

The headline assertion in this file is a negative one: there is no execution path. A test
walks the module's public surface and the port protocols and fails if anything that could
run a command appears.

PRD section 15 edge cases named here:
  * "planned maintenance suppression" — the maintenance runbook suppresses rather than
    changes anything.
  * "flapping alarms and alarm storms" — the flapping runbook explicitly refuses to raise a
    customer-facing incident.
  * "non-network complaint surges" — the non-network runbook routes away from the incident
    engine.
  * "live mode accidental write attempts" — no port in this slice can write to the network,
    and the policy's `never` tier is asserted rather than assumed.
  * "duplicate and out-of-order events" — repeated saves of one proposal are idempotent.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tazama.core.clock import FixedClock
from tazama.core.errors import Conflict, Forbidden, NotFound, PolicyViolation, ValidationFailed
from tazama.core.policy import Policies, load_policies
from tazama.core.security import PERMISSIONS, Principal
from tazama.domain.events import DomainEvent, EventType
from tazama.domain.value_objects import IncidentCause
from tazama.modules.recovery import ports as recovery_ports
from tazama.modules.recovery.models import (
    NEVER_TIER_TOOLS,
    FileRunbookRepository,
    IncidentBrief,
    RecoveryProposal,
    RecoveryStatus,
    RiskTier,
    load_runbooks,
    ordered_steps,
)
from tazama.modules.recovery.service import RecoveryService, runbook_summary

NOW = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)

NOC = Principal(subject="noc-1", role="noc_engineer")
MANAGER_A = Principal(subject="mgr-a", role="manager")
MANAGER_B = Principal(subject="mgr-b", role="manager")
ADMIN = Principal(subject="adm-1", role="admin")
ANALYST = Principal(subject="an-1", role="analyst")

CONGESTION = IncidentBrief(
    id="INC_CONG",
    region="Nairobi",
    status="mitigating",
    cause=str(IncidentCause.CONGESTION),
    affected_elements=("CELL_1", "CELL_2"),
)
TRANSMISSION = IncidentBrief(
    id="INC_TX",
    region="Mombasa",
    status="mitigating",
    cause=str(IncidentCause.TRANSMISSION_LINK_FAILURE),
    affected_elements=("LINK_9",),
)


# ------------------------------------------------------------------- fakes


@dataclass(slots=True)
class FakeRepo:
    rows: dict[str, RecoveryProposal] = field(default_factory=dict)

    def save(self, proposal: RecoveryProposal) -> RecoveryProposal:
        self.rows[proposal.id] = proposal
        return proposal

    def get(self, action_id: str) -> RecoveryProposal | None:
        return self.rows.get(action_id)

    def for_incident(self, incident_id: str) -> tuple[RecoveryProposal, ...]:
        return tuple(r for r in self.rows.values() if r.incident_id == incident_id)


@dataclass(slots=True)
class RecordingPublisher:
    events: list[DomainEvent] = field(default_factory=list)

    def publish(self, event: DomainEvent) -> bool:
        self.events.append(event)
        return True

    def of(self, event_type: EventType) -> list[DomainEvent]:
        return [e for e in self.events if e.event_type is event_type]


@dataclass(slots=True)
class RecordingDecisions:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Any = (),
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
                "confidence": confidence,
                "rule_version": rule_version,
            },
        )

    def actions(self) -> list[str]:
        return [e["action"] for e in self.entries]


@dataclass(slots=True)
class Harness:
    service: RecoveryService
    repo: FakeRepo
    runbooks: FileRunbookRepository
    publisher: RecordingPublisher
    decisions: RecordingDecisions
    clock: FixedClock


@pytest.fixture
def policies() -> Policies:
    return load_policies()


@pytest.fixture
def runbooks() -> FileRunbookRepository:
    return FileRunbookRepository()


@pytest.fixture
def harness(policies: Policies, runbooks: FileRunbookRepository) -> Harness:
    repo = FakeRepo()
    publisher = RecordingPublisher()
    decisions = RecordingDecisions()
    clock = FixedClock(NOW)
    service = RecoveryService(
        runbooks=runbooks,
        repository=repo,
        publisher=publisher,
        decisions=decisions,
        policies=policies,
        clock=clock,
    )
    return Harness(service, repo, runbooks, publisher, decisions, clock)


# ========================================================= the catalogue


def test_catalogue_covers_the_required_causes(runbooks: FileRunbookRepository):
    causes = {r.cause for r in runbooks.all()}
    required = {
        str(IncidentCause.TRANSMISSION_LINK_FAILURE),
        str(IncidentCause.CELL_OUTAGE),
        str(IncidentCause.CONGESTION),
        str(IncidentCause.POWER_FAILURE),
        str(IncidentCause.CONFIGURATION_CHANGE),
        str(IncidentCause.PLANNED_MAINTENANCE),
        str(IncidentCause.FLAPPING),
    }
    assert required <= causes


def test_catalogue_has_seven_or_eight_runbooks(runbooks: FileRunbookRepository):
    assert 7 <= len(runbooks.all()) <= 8


def test_runbook_ids_are_stable_and_readable(runbooks: FileRunbookRepository):
    ids = {r.id for r in runbooks.all()}
    assert ids == {
        "RBK-CELL-OUTAGE-01",
        "RBK-TRANSMISSION-01",
        "RBK-CONGESTION-01",
        "RBK-POWER-01",
        "RBK-CONFIG-01",
        "RBK-MAINTENANCE-01",
        "RBK-FLAPPING-01",
        "RBK-NON-NETWORK-01",
    }


def test_every_runbook_has_ordered_steps_and_a_rollback(runbooks: FileRunbookRepository):
    for runbook in runbooks.all():
        assert runbook.steps
        assert runbook.rollback
        assert [s.n for s in ordered_steps(runbook.steps)] == sorted(s.n for s in runbook.steps)
        assert runbook.expected_effect
        assert runbook.owner


def test_every_runbook_declares_a_risk_tier_and_an_estimate(runbooks: FileRunbookRepository):
    for runbook in runbooks.all():
        assert runbook.risk_tier in tuple(RiskTier)
        assert runbook.estimated_minutes > 0


def test_high_risk_runbooks_require_approval(runbooks: FileRunbookRepository):
    high = [r for r in runbooks.all() if r.is_high_risk]
    assert high
    assert all(r.requires_approval for r in high)


def test_no_runbook_step_claims_an_execute_tier(runbooks: FileRunbookRepository):
    for runbook in runbooks.all():
        for step in runbook.steps:
            assert step.tier in ("read", "propose")


def test_the_planned_maintenance_runbook_suppresses_rather_than_changes(
    runbooks: FileRunbookRepository,
):
    """PRD section 15: "planned maintenance suppression"."""
    runbook = runbooks.get("RBK-MAINTENANCE-01")
    assert runbook is not None
    assert runbook.cause == str(IncidentCause.PLANNED_MAINTENANCE)
    assert any("uppress" in step.action for step in runbook.steps)


def test_the_flapping_runbook_refuses_a_customer_facing_incident(
    runbooks: FileRunbookRepository,
):
    """PRD section 15: "flapping alarms and alarm storms"."""
    runbook = runbooks.get("RBK-FLAPPING-01")
    assert runbook is not None
    assert any("Do not raise a customer-facing" in step.action for step in runbook.steps)


def test_the_non_network_runbook_routes_away_from_the_incident_engine(
    runbooks: FileRunbookRepository,
):
    """PRD section 15: "non-network complaint surges"."""
    runbook = runbooks.get("RBK-NON-NETWORK-01")
    assert runbook is not None
    assert any("billing operations" in step.action for step in runbook.steps)


def _write(tmp_path, body: str):
    path = tmp_path / "runbooks.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_GOOD = """
runbooks:
  - id: R1
    title: One
    cause: congestion
    risk_tier: low
    owner: ran
    steps: ["look"]
    rollback: ["undo"]
    expected_effect: better
"""


def test_loader_accepts_a_minimal_valid_catalogue(tmp_path):
    catalogue = load_runbooks(_write(tmp_path, _GOOD))
    assert catalogue["R1"].steps[0].action == "look"


def test_loader_refuses_an_unknown_cause(tmp_path):
    with pytest.raises(ValidationFailed, match="is not an IncidentCause"):
        load_runbooks(_write(tmp_path, _GOOD.replace("congestion", "gremlins")))


def test_loader_refuses_an_unknown_risk_tier(tmp_path):
    with pytest.raises(ValidationFailed, match="risk_tier"):
        load_runbooks(_write(tmp_path, _GOOD.replace("risk_tier: low", "risk_tier: extreme")))


def test_loader_refuses_a_runbook_with_no_rollback(tmp_path):
    with pytest.raises(ValidationFailed, match="rollback must not be empty"):
        load_runbooks(_write(tmp_path, _GOOD.replace('    rollback: ["undo"]\n', "")))


def test_loader_refuses_a_runbook_with_no_expected_effect(tmp_path):
    with pytest.raises(ValidationFailed, match="expected_effect"):
        load_runbooks(_write(tmp_path, _GOOD.replace("expected_effect: better", "")))


def test_loader_refuses_a_high_risk_runbook_that_waives_approval(tmp_path):
    body = _GOOD.replace("risk_tier: low", "risk_tier: high") + "    requires_approval: false\n"
    with pytest.raises(ValidationFailed, match="always requires approval"):
        load_runbooks(_write(tmp_path, body))


def test_loader_refuses_a_duplicate_id(tmp_path):
    body = _GOOD + _GOOD.split("runbooks:")[1]
    with pytest.raises(ValidationFailed, match="duplicate runbook id"):
        load_runbooks(_write(tmp_path, body))


def test_loader_refuses_a_step_tier_that_is_not_read_or_propose(tmp_path):
    body = _GOOD.replace('steps: ["look"]', "steps: [{action: go, tier: execute}]")
    with pytest.raises(ValidationFailed, match="tier must be read or propose"):
        load_runbooks(_write(tmp_path, body))


def test_loader_refuses_a_missing_file(tmp_path):
    with pytest.raises(ValidationFailed, match="not found"):
        load_runbooks(tmp_path / "absent.yaml")


def test_loader_refuses_an_empty_catalogue(tmp_path):
    with pytest.raises(ValidationFailed, match="non-empty list"):
        load_runbooks(_write(tmp_path, "runbooks: []\n"))


# ========================================================== recommend


def test_recommend_picks_a_runbook_for_the_cause(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    assert proposal.runbook_id == "RBK-CONGESTION-01"
    assert proposal.status is RecoveryStatus.PROPOSED
    assert proposal.affected_elements == CONGESTION.affected_elements


def test_recommend_requires_the_recommend_capability(harness: Harness):
    with pytest.raises(Forbidden):
        harness.service.recommend(CONGESTION, principal=ANALYST)


def test_recommend_publishes_recovery_recommended_marked_simulated_only(harness: Harness):
    harness.service.recommend(CONGESTION, principal=NOC)
    event = harness.publisher.of(EventType.RECOVERY_RECOMMENDED)[0]
    assert event.payload["execution"] == "simulated_only"
    assert event.payload["runbook_id"] == "RBK-CONGESTION-01"


def test_recommend_records_the_candidates_it_saw(harness: Harness):
    harness.service.recommend(CONGESTION, principal=NOC)
    entry = harness.decisions.entries[0]
    assert entry["action"] == "recovery_recommend"
    assert entry["inputs"]["candidates"] == ["RBK-CONGESTION-01"]
    assert entry["inputs"]["chosen"] == "RBK-CONGESTION-01"
    assert entry["rule_version"]


def test_a_diagnosis_hint_overrides_the_incident_cause(harness: Harness):
    unknown = replace(CONGESTION, cause=str(IncidentCause.UNKNOWN))
    proposal = harness.service.recommend(unknown, str(IncidentCause.POWER_FAILURE), principal=NOC)
    assert proposal.runbook_id == "RBK-POWER-01"


def test_an_unrecognised_hint_falls_back_to_the_incident_cause(harness: Harness):
    """The AI does not get to block a proposal, and it does not get to invent one."""
    proposal = harness.service.recommend(CONGESTION, "please run rm -rf", principal=NOC)
    assert proposal.runbook_id == "RBK-CONGESTION-01"
    assert harness.decisions.entries[0]["inputs"]["diagnosis_hint"] == "please run rm -rf"


def test_a_cause_with_no_runbook_is_not_found(harness: Harness):
    orphan = replace(CONGESTION, cause=str(IncidentCause.UNKNOWN))
    with pytest.raises(NotFound, match="No runbook"):
        harness.service.recommend(orphan, principal=NOC)


def test_recommend_without_a_principal_attributes_the_rules(harness: Harness):
    proposal = harness.service.recommend(CONGESTION)
    assert proposal.proposed_by == "recovery_rules"


def test_high_risk_recommendation_needs_two_approvals(harness: Harness):
    proposal = harness.service.recommend(TRANSMISSION, principal=NOC)
    assert proposal.risk_tier is RiskTier.HIGH
    assert proposal.required_approvals == 2


def test_low_risk_recommendation_still_needs_one_approval(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    assert proposal.requires_approval is True
    assert proposal.required_approvals == 1


# =========================================================== approval


def test_approve_requires_the_approve_capability(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    with pytest.raises(Forbidden):
        harness.service.approve(proposal.id, NOC)


def test_approve_requires_a_role_the_policy_names(harness: Harness, policies: Policies):
    rule = policies.approval_for("recovery_execute")
    assert "noc_engineer" not in rule.roles
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    approved = harness.service.approve(proposal.id, MANAGER_A)
    assert approved.status is RecoveryStatus.APPROVED


def test_one_approval_completes_a_low_risk_action(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    approved = harness.service.approve(proposal.id, MANAGER_A)
    assert approved.approvals == (MANAGER_A.subject,)
    assert harness.publisher.of(EventType.RECOVERY_APPROVED)


def test_a_high_risk_action_waits_for_a_second_approver(harness: Harness):
    proposal = harness.service.recommend(TRANSMISSION, principal=NOC)
    first = harness.service.approve(proposal.id, MANAGER_A)
    assert first.status is RecoveryStatus.AWAITING_SECOND_APPROVAL
    assert harness.publisher.of(EventType.RECOVERY_APPROVED) == []
    second = harness.service.approve(proposal.id, ADMIN)
    assert second.status is RecoveryStatus.APPROVED
    assert second.approvals == (MANAGER_A.subject, ADMIN.subject)
    assert harness.publisher.of(EventType.RECOVERY_APPROVED)


def test_the_second_approver_must_be_a_different_person(harness: Harness):
    proposal = harness.service.recommend(TRANSMISSION, principal=NOC)
    harness.service.approve(proposal.id, MANAGER_A)
    with pytest.raises(PolicyViolation, match="different person"):
        harness.service.approve(proposal.id, MANAGER_A)


def test_the_proposer_may_not_approve_a_high_risk_action(harness: Harness):
    # `admin` is the one role holding both `recovery:recommend` and `recovery:approve`,
    # which is exactly the account separation of duties has to stop.
    proposal = harness.service.recommend(TRANSMISSION, principal=ADMIN)
    with pytest.raises(PolicyViolation, match="person who proposed it"):
        harness.service.approve(proposal.id, ADMIN)


def test_approving_a_completed_action_is_a_conflict(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.approve(proposal.id, MANAGER_A)
    with pytest.raises(Conflict):
        harness.service.approve(proposal.id, MANAGER_B)


def test_approving_an_unknown_action_is_not_found(harness: Harness):
    with pytest.raises(NotFound):
        harness.service.approve("RCV_missing", MANAGER_A)


def test_reject_requires_a_reason(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    with pytest.raises(ValidationFailed, match="reason is required"):
        harness.service.reject(proposal.id, MANAGER_A, " ")


def test_reject_records_the_reason(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    rejected = harness.service.reject(proposal.id, MANAGER_A, "window is wrong")
    assert rejected.status is RecoveryStatus.REJECTED
    assert rejected.rejected_reason == "window is wrong"
    assert "recovery_reject" in harness.decisions.actions()


def test_a_rejected_action_cannot_be_rejected_again(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.reject(proposal.id, MANAGER_A, "no")
    with pytest.raises(Conflict):
        harness.service.reject(proposal.id, MANAGER_A, "still no")


# ========================================================= simulation


def test_simulate_returns_a_modelled_outcome(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    outcome = harness.service.simulate(proposal.id)
    assert outcome.simulation is True
    assert outcome.executed is False
    assert outcome.affected_elements == CONGESTION.affected_elements
    assert outcome.estimated_minutes == 60
    assert outcome.rollback_plan
    assert [k.kpi for k in outcome.expected_kpis] == [
        "prb_utilisation_pct",
        "dl_throughput_mbps",
    ]


def test_simulated_outcome_is_stored_and_clearly_marked(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.simulate(proposal.id)
    stored = harness.repo.get(proposal.id)
    assert stored is not None
    assert stored.simulated_outcome is not None
    assert stored.simulated_outcome["simulation"] is True
    assert stored.simulated_outcome["executed"] is False
    assert "SIMULATION ONLY" in stored.simulated_outcome["disclaimer"]
    assert stored.status is RecoveryStatus.SIMULATED


def test_simulation_confidence_is_lower_for_higher_risk(harness: Harness):
    low = harness.service.simulate(harness.service.recommend(CONGESTION, principal=NOC).id)
    high = harness.service.simulate(harness.service.recommend(TRANSMISSION, principal=NOC).id)
    assert high.confidence < low.confidence


def test_simulation_carries_caveats_that_say_nothing_ran(harness: Harness):
    outcome = harness.service.simulate(harness.service.recommend(CONGESTION, principal=NOC).id)
    assert any("No command was issued" in caveat for caveat in outcome.caveats)


def test_simulating_an_approved_action_does_not_change_its_status(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.approve(proposal.id, MANAGER_A)
    harness.service.simulate(proposal.id)
    stored = harness.repo.get(proposal.id)
    assert stored is not None
    assert stored.status is RecoveryStatus.APPROVED


def test_a_rejected_action_is_not_simulated(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.reject(proposal.id, MANAGER_A, "no")
    with pytest.raises(Conflict):
        harness.service.simulate(proposal.id)


def test_simulate_records_a_decision_stating_nothing_was_executed(harness: Harness):
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.simulate(proposal.id)
    entry = next(e for e in harness.decisions.entries if e["action"] == "recovery_simulate")
    assert entry["inputs"]["executed"] is False
    assert entry["confidence"] is not None


# ========================================================== rollback


def test_propose_rollback_creates_a_linked_proposal(harness: Harness):
    forward = harness.service.recommend(CONGESTION, principal=NOC)
    rollback = harness.service.propose_rollback(forward, principal=NOC)
    assert rollback.rollback_of == forward.id
    assert rollback.is_rollback is True
    assert rollback.incident_id == forward.incident_id
    assert rollback.id != forward.id


def test_a_rollback_inherits_the_forward_risk_tier(harness: Harness):
    forward = harness.service.recommend(TRANSMISSION, principal=NOC)
    rollback = harness.service.propose_rollback(forward, principal=NOC)
    assert rollback.risk_tier is RiskTier.HIGH
    assert rollback.required_approvals == 2


def test_a_rollback_of_a_rollback_is_refused(harness: Harness):
    forward = harness.service.recommend(CONGESTION, principal=NOC)
    rollback = harness.service.propose_rollback(forward, principal=NOC)
    with pytest.raises(ValidationFailed, match="chained rollbacks"):
        harness.service.propose_rollback(rollback, principal=NOC)


def test_propose_rollback_records_the_rollback_steps(harness: Harness):
    forward = harness.service.recommend(CONGESTION, principal=NOC)
    harness.service.propose_rollback(forward, principal=NOC)
    entry = next(e for e in harness.decisions.entries if e["action"] == "recovery_propose_rollback")
    assert entry["inputs"]["rollback_of"] == forward.id
    assert entry["inputs"]["steps"]


# ================================================ execution is impossible


_EXECUTION_WORDS = re.compile(
    r"execut|dispatch|run_command|network_command|apply_change|push_config|invoke|shell",
    re.IGNORECASE,
)


def test_no_public_method_in_the_service_suggests_execution():
    names = [name for name in dir(RecoveryService) if not name.startswith("_")]
    offenders = [name for name in names if _EXECUTION_WORDS.search(name)]
    assert offenders == [], f"recovery service exposes {offenders}"


def test_no_port_protocol_exposes_an_execution_method():
    for _, obj in inspect.getmembers(recovery_ports, inspect.isclass):
        if obj.__module__ != recovery_ports.__name__:
            continue
        offenders = [
            name for name in dir(obj) if not name.startswith("_") and _EXECUTION_WORDS.search(name)
        ]
        assert offenders == [], f"{obj.__name__} exposes {offenders}"


def test_no_execute_capability_exists_in_the_rbac_table():
    offenders = [name for name in PERMISSIONS if "execute" in name]
    assert offenders == []
    assert "recovery:execute" not in PERMISSIONS


def test_the_policy_never_tier_is_honoured(harness: Harness, policies: Policies):
    assert harness.service.never_tier_tools() == NEVER_TIER_TOOLS
    for tool in NEVER_TIER_TOOLS:
        assert policies.ai.diagnosis_agent.tier(tool) == "never"


def test_an_unknown_tool_is_never_tier_by_default(policies: Policies):
    assert policies.ai.diagnosis_agent.tier("brand_new_tool") == "never"


def test_runbook_summary_contains_no_execution_affordance(runbooks: FileRunbookRepository):
    runbook = runbooks.get("RBK-CONFIG-01")
    assert runbook is not None
    summary = runbook_summary(runbook)
    assert set(summary) == {
        "id",
        "title",
        "cause",
        "risk_tier",
        "owner",
        "requires_approval",
        "estimated_minutes",
        "steps",
        "rollback",
        "expected_effect",
    }
    assert all(step["tier"] in ("read", "propose") for step in summary["steps"])


def test_saving_the_same_proposal_twice_is_idempotent(harness: Harness):
    """PRD section 15: "duplicate and out-of-order events"."""
    proposal = harness.service.recommend(CONGESTION, principal=NOC)
    harness.repo.save(proposal)
    harness.repo.save(proposal)
    assert len(harness.repo.rows) == 1
