"""Recovery recommendation: pick a runbook, get it approved, simulate it. Never run it.

Why simulation rather than execution: the value of this module is the discipline — a
runbook chosen from a reviewed catalogue, an approval trail with separation of duties for
high risk, a modelled outcome an engineer can argue with, and a rollback that exists before
the change does. Wiring an execution path onto that is a day's work; building the
discipline afterwards never happens.

So: `recommend` → `approve`/`reject` → `simulate`, and `propose_rollback` for the way back.
There is no `execute`, and `test_recovery.py` asserts there is no way to add one by
accident.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tazama.core.clock import Clock
from tazama.core.errors import Conflict, NotFound, PolicyViolation, ValidationFailed
from tazama.core.ids import get_correlation_id, new_id
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.core.security import Principal
from tazama.domain.events import (
    AggregateType,
    DomainEvent,
    RecoveryApproved,
    RecoveryRecommended,
)
from tazama.domain.value_objects import IncidentCause
from tazama.modules.recovery.models import (
    NEVER_TIER_TOOLS,
    IncidentBrief,
    RecommendationInputs,
    RecoveryProposal,
    RecoveryStatus,
    RiskTier,
    Runbook,
    SimulatedOutcome,
    ordered_steps,
)
from tazama.modules.recovery.ports import (
    DecisionLog,
    EventPublisher,
    RecoveryRepository,
    RunbookRepository,
)

_log = get_logger(__name__)

#: Action name in `config/policies.yaml` that governs recovery approval.
APPROVAL_ACTION = "recovery_execute"

ENTITY_TYPE = "recovery_action"

#: States from which an approval is still meaningful. `SIMULATED` is included because
#: looking at the modelled outcome before approving is the intended workflow — the first
#: version refused it, which made "simulate, then approve" impossible.
APPROVABLE_STATUSES: frozenset[RecoveryStatus] = frozenset(
    {
        RecoveryStatus.PROPOSED,
        RecoveryStatus.AWAITING_SECOND_APPROVAL,
        RecoveryStatus.SIMULATED,
    },
)

#: How confident the *simulation* is, by risk tier. A high-risk change has more ways to
#: surprise you, so its modelled outcome is claimed less strongly.
_SIMULATION_CONFIDENCE: dict[RiskTier, float] = {
    RiskTier.LOW: 0.7,
    RiskTier.MEDIUM: 0.55,
    RiskTier.HIGH: 0.4,
}

_SIMULATION_CAVEATS: tuple[str, ...] = (
    "Modelled from the runbook's declared expected effect, not from a trial run.",
    "No command was issued and no element was changed: execution is out of scope in v1.",
    "KPI movement is an expectation to verify afterwards, not a prediction to rely on.",
)


class RecoveryService:
    def __init__(
        self,
        *,
        runbooks: RunbookRepository,
        repository: RecoveryRepository,
        publisher: EventPublisher,
        decisions: DecisionLog,
        policies: Policies,
        clock: Clock,
    ) -> None:
        self._runbooks = runbooks
        self._repo = repository
        self._publisher = publisher
        self._decisions = decisions
        self._policies = policies
        self._clock = clock

    # ----------------------------------------------------------- recommend

    def recommend(
        self,
        incident: IncidentBrief,
        diagnosis_hint: str | None = None,
        *,
        principal: Principal | None = None,
    ) -> RecoveryProposal:
        """Choose a runbook for the incident's cause and record why.

        `diagnosis_hint` is the DiagnosisAgent's suggested cause or runbook id. It is a
        *hint*: it narrows the catalogue lookup and is recorded, but an unknown hint falls
        back to the incident's own cause rather than failing — the AI does not get to block
        a recovery proposal, and it does not get to invent one either.
        """
        if principal is not None:
            principal.require("recovery:recommend")

        cause = self._resolve_cause(incident, diagnosis_hint)
        candidates = self._runbooks.for_cause(cause)
        if not candidates:
            raise NotFound(
                f"No runbook in the catalogue covers cause {cause!r}; the incident needs a "
                f"human and a new runbook.",
            )
        # Lowest risk first: the catalogue may hold several for one cause, and the safest
        # change that achieves the expected effect is the one to put in front of a human.
        chosen = sorted(candidates, key=lambda r: (_risk_order(r.risk_tier), r.id))[0]

        rule = self._policies.approval_for(APPROVAL_ACTION)
        required = 2 if rule.requires_two_approvers(str(chosen.risk_tier)) else 1
        requires_approval = chosen.requires_approval or rule.required

        now = self._clock.now()
        proposal = RecoveryProposal(
            id=new_id("RCV", self._clock),
            incident_id=incident.id,
            runbook_id=chosen.id,
            status=RecoveryStatus.PROPOSED,
            risk_tier=chosen.risk_tier,
            proposed_by=principal.subject if principal else "recovery_rules",
            proposed_at=now,
            rationale=(
                f"cause={cause}; runbook={chosen.id} ({chosen.risk_tier} risk); "
                f"expected effect: {chosen.expected_effect}"
            ),
            requires_approval=requires_approval,
            required_approvals=required if requires_approval else 0,
            affected_elements=incident.affected_elements,
        )
        stored = self._repo.save(proposal)

        inputs = RecommendationInputs(
            incident_id=incident.id,
            cause=cause,
            diagnosis_hint=diagnosis_hint,
            candidates=tuple(r.id for r in candidates),
            chosen=chosen.id,
            reason="lowest risk tier that covers the cause",
            extra={
                "risk_tier": str(chosen.risk_tier),
                "requires_approval": requires_approval,
                "required_approvals": stored.required_approvals,
                "owner": chosen.owner,
            },
        )
        self._publish(
            RecoveryRecommended(
                aggregate_type=AggregateType.RECOVERY,
                aggregate_id=stored.id,
                occurred_at=now,
                dedupe_key=f"{incident.id}:{chosen.id}:recommended",
                correlation_id=get_correlation_id(),
                payload={
                    "incident_id": incident.id,
                    "runbook_id": chosen.id,
                    "risk_tier": str(chosen.risk_tier),
                    "requires_approval": requires_approval,
                    "required_approvals": stored.required_approvals,
                    "execution": "simulated_only",
                },
            ),
        )
        self._decisions.record(
            action="recovery_recommend",
            entity_type=ENTITY_TYPE,
            entity_id=stored.id,
            outcome="proposed",
            inputs=inputs.to_dict(),
            rule_version=self._policies.rule_version,
        )
        return stored

    # ------------------------------------------------------------ approval

    def approve(self, action_id: str, principal: Principal) -> RecoveryProposal:
        """Record one approval. High risk needs two, from two different people."""
        principal.require("recovery:approve")
        rule = self._policies.approval_for(APPROVAL_ACTION)
        if rule.roles and principal.role not in rule.roles:
            raise PolicyViolation(
                f"Role {principal.role!r} may not approve a recovery action "
                f"(policy allows: {', '.join(rule.roles)}).",
            )
        proposal = self._require(action_id)
        if proposal.status not in APPROVABLE_STATUSES:
            raise Conflict(
                f"Recovery action {action_id} is {proposal.status} and can no longer be approved.",
            )
        if principal.subject in proposal.approvals:
            raise PolicyViolation(
                "The second approver must be a different person; this approval is already "
                "recorded against you.",
            )
        if proposal.proposed_by == principal.subject and proposal.required_approvals > 1:
            raise PolicyViolation(
                "A high-risk recovery may not be approved by the person who proposed it.",
            )

        now = self._clock.now()
        updated = self._repo.save(proposal.with_approval(approver=principal.subject, at=now))
        if updated.status is RecoveryStatus.APPROVED:
            self._publish(
                RecoveryApproved(
                    aggregate_type=AggregateType.RECOVERY,
                    aggregate_id=updated.id,
                    occurred_at=now,
                    dedupe_key=f"{updated.id}:approved",
                    correlation_id=get_correlation_id(),
                    payload={
                        "incident_id": updated.incident_id,
                        "runbook_id": updated.runbook_id,
                        "approvals": list(updated.approvals),
                        "risk_tier": str(updated.risk_tier),
                        "execution": "simulated_only",
                    },
                ),
            )
        self._decisions.record(
            action="recovery_approve",
            entity_type=ENTITY_TYPE,
            entity_id=updated.id,
            outcome=str(updated.status),
            inputs={
                "approver": principal.subject,
                "approver_role": principal.role,
                "approvals": list(updated.approvals),
                "required_approvals": updated.required_approvals,
                "risk_tier": str(updated.risk_tier),
            },
            rule_version=self._policies.rule_version,
        )
        return updated

    def reject(self, action_id: str, principal: Principal, reason: str) -> RecoveryProposal:
        principal.require("recovery:approve")
        clean = reason.strip()
        if not clean:
            raise ValidationFailed("A rejection reason is required.")
        proposal = self._require(action_id)
        if proposal.status in (RecoveryStatus.REJECTED, RecoveryStatus.APPROVED):
            raise Conflict(f"Recovery action {action_id} is already {proposal.status}.")
        rejected = self._repo.save(proposal.rejected(reason=clean[:200]))
        self._decisions.record(
            action="recovery_reject",
            entity_type=ENTITY_TYPE,
            entity_id=rejected.id,
            outcome="rejected",
            inputs={"approver": principal.subject, "reason": rejected.rejected_reason},
            rule_version=self._policies.rule_version,
        )
        return rejected

    # ---------------------------------------------------------- simulation

    def simulate(self, action_id: str) -> SimulatedOutcome:
        """Model the outcome. This is the closest v1 gets to doing anything.

        The result is stored on `recovery_action.simulated_outcome` with `simulation: true`
        and `executed: false`, so no later reader can mistake it for a change record.
        """
        proposal = self._require(action_id)
        runbook = self._runbooks.get(proposal.runbook_id)
        if runbook is None:
            raise NotFound(
                f"Runbook {proposal.runbook_id} is no longer in the catalogue; the "
                f"simulation cannot be reproduced.",
            )
        if proposal.status is RecoveryStatus.REJECTED:
            raise Conflict("A rejected recovery action is not simulated.")

        now = self._clock.now()
        outcome = SimulatedOutcome(
            runbook_id=runbook.id,
            expected_kpis=runbook.expected_kpis,
            affected_elements=proposal.affected_elements,
            estimated_minutes=runbook.estimated_minutes,
            rollback_plan=ordered_steps(runbook.rollback),
            expected_effect=runbook.expected_effect,
            simulated_at=now,
            confidence=_SIMULATION_CONFIDENCE[runbook.risk_tier],
            caveats=_SIMULATION_CAVEATS,
        )
        stored = proposal.with_simulation(outcome)
        if stored.status is RecoveryStatus.PROPOSED:
            # A simulation is not an approval; the status only records that it has been run.
            stored = _with_status(stored, RecoveryStatus.SIMULATED)
        self._repo.save(stored)
        self._decisions.record(
            action="recovery_simulate",
            entity_type=ENTITY_TYPE,
            entity_id=proposal.id,
            outcome="simulated",
            inputs={
                "runbook_id": runbook.id,
                "risk_tier": str(runbook.risk_tier),
                "estimated_minutes": runbook.estimated_minutes,
                "affected_elements": list(proposal.affected_elements),
                "executed": False,
            },
            confidence=outcome.confidence,
            rule_version=self._policies.rule_version,
        )
        return outcome

    # ------------------------------------------------------------ rollback

    def propose_rollback(
        self, action: RecoveryProposal, *, principal: Principal | None = None
    ) -> RecoveryProposal:
        """Create a linked proposal that models the way back from `action`.

        The rollback inherits the forward change's risk tier: undoing a high-risk change at
        03:00 is not a low-risk activity, whatever the step list looks like.
        """
        if principal is not None:
            principal.require("recovery:recommend")
        if action.is_rollback:
            raise ValidationFailed(
                f"Recovery action {action.id} is itself a rollback; chained rollbacks are "
                f"refused as a loop guard.",
            )
        runbook = self._runbooks.get(action.runbook_id)
        if runbook is None:
            raise NotFound(f"Runbook {action.runbook_id} is no longer in the catalogue.")
        if not runbook.rollback:
            raise ValidationFailed(f"Runbook {runbook.id} declares no rollback steps.")

        rule = self._policies.approval_for(APPROVAL_ACTION)
        required = 2 if rule.requires_two_approvers(str(runbook.risk_tier)) else 1
        now = self._clock.now()
        rollback = RecoveryProposal(
            id=new_id("RCV", self._clock),
            incident_id=action.incident_id,
            runbook_id=runbook.id,
            status=RecoveryStatus.PROPOSED,
            risk_tier=runbook.risk_tier,
            proposed_by=principal.subject if principal else "recovery_rules",
            proposed_at=now,
            rationale=f"rollback of {action.id}: {runbook.expected_effect}",
            requires_approval=True,
            required_approvals=required,
            rollback_of=action.id,
            affected_elements=action.affected_elements,
        )
        stored = self._repo.save(rollback)
        self._publish(
            RecoveryRecommended(
                aggregate_type=AggregateType.RECOVERY,
                aggregate_id=stored.id,
                occurred_at=now,
                dedupe_key=f"{action.id}:rollback",
                correlation_id=get_correlation_id(),
                payload={
                    "incident_id": stored.incident_id,
                    "runbook_id": runbook.id,
                    "rollback_of": action.id,
                    "risk_tier": str(stored.risk_tier),
                    "execution": "simulated_only",
                },
            ),
        )
        self._decisions.record(
            action="recovery_propose_rollback",
            entity_type=ENTITY_TYPE,
            entity_id=stored.id,
            outcome="proposed",
            inputs={
                "rollback_of": action.id,
                "runbook_id": runbook.id,
                "steps": [step.action for step in ordered_steps(runbook.rollback)],
            },
            rule_version=self._policies.rule_version,
        )
        return stored

    # ------------------------------------------------------------- helpers

    def never_tier_tools(self) -> tuple[str, ...]:
        """Tools this module refuses to own, cross-checked against the policy file.

        Exposed so a test can assert the module and `config/policies.yaml` agree: if a
        future edit downgrades `runbook_execute` from `never`, the assertion fails here.
        """
        budget = self._policies.ai.diagnosis_agent
        return tuple(tool for tool in NEVER_TIER_TOOLS if budget.tier(tool) == "never")

    def get(self, action_id: str) -> RecoveryProposal:
        """One proposal, or `NotFound`.

        Public because callers that need to act on a proposal (the API proposing a
        rollback, for instance) hold an id, not the object.
        """
        return self._require(action_id)

    def _require(self, action_id: str) -> RecoveryProposal:
        proposal = self._repo.get(action_id)
        if proposal is None:
            raise NotFound(f"Unknown recovery action {action_id}.")
        return proposal

    def _resolve_cause(self, incident: IncidentBrief, hint: str | None) -> str:
        """A hint only counts if it names a real cause; otherwise the incident's own wins."""
        known = {str(cause) for cause in IncidentCause}
        if hint and hint in known:
            return hint
        if hint:
            _log.info("ignoring unrecognised diagnosis hint", extra={"hint": hint})
        if incident.cause and incident.cause in known:
            return incident.cause
        return str(IncidentCause.UNKNOWN)

    def _publish(self, event: DomainEvent) -> None:
        self._publisher.publish(event)


def _risk_order(tier: RiskTier) -> int:
    return {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}[tier]


def _with_status(proposal: RecoveryProposal, status: RecoveryStatus) -> RecoveryProposal:
    return replace(proposal, status=status)


def runbook_summary(runbook: Runbook) -> dict[str, object]:
    """Flat, UI-friendly view of a runbook. No execution affordance anywhere in it."""
    return {
        "id": runbook.id,
        "title": runbook.title,
        "cause": runbook.cause,
        "risk_tier": str(runbook.risk_tier),
        "owner": runbook.owner,
        "requires_approval": runbook.requires_approval,
        "estimated_minutes": runbook.estimated_minutes,
        "steps": [
            {"n": s.n, "action": s.action, "tier": s.tier} for s in ordered_steps(runbook.steps)
        ],
        "rollback": [{"n": s.n, "action": s.action} for s in ordered_steps(runbook.rollback)],
        "expected_effect": runbook.expected_effect,
    }


def simulated_at_or_none(proposal: RecoveryProposal) -> datetime | None:
    """When the stored simulation was produced, if there is one."""
    outcome = proposal.simulated_outcome
    if not outcome:
        return None
    raw = outcome.get("simulated_at")
    return datetime.fromisoformat(str(raw)) if raw else None
