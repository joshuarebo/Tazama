"""Prioritisation: a transparent weighted rule, with room for a model later.

Design choices worth defending:
  * **Weights live in `config/policies.yaml`** and must sum to 1.0 (the loader enforces it),
    so a score is comparable across incidents and a reviewer can change the balance without
    a code change.
  * **Each factor is normalised against a saturation point** rather than scaled to the
    current batch. Batch-relative scaling would make an incident's priority depend on what
    else happened that hour, which is indefensible to an operations manager.
  * **Overrides are explicit and recorded.** Two situations outrank the arithmetic: an
    enterprise SLA breach in progress, and a total outage. Both are stated here, not hidden
    in a threshold.
  * **An ML score never replaces the rule.** When Phase 3 provides one it is blended, and
    both versions are stored on the decision.
"""

from __future__ import annotations

from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.domain.events import AggregateType, CasePrioritized
from tazama.modules.prioritisation.models import (
    SATURATION,
    FactorContribution,
    PriorityDecision,
    PriorityInputs,
)
from tazama.modules.prioritisation.ports import (
    CaseWriter,
    DecisionLog,
    EventPublisher,
    PriorityScorer,
)

_log = get_logger(__name__)

#: How much weight an ML score may take from the rule once one exists. Capped low on
#: purpose: the rule stays the thing a human can argue with.
MAX_MODEL_BLEND = 0.3

#: An SLA risk at or above this, with at least one enterprise account, forces P1.
SLA_OVERRIDE_RISK = 0.9

#: A total outage (availability at zero on every affected cell) forces at least P2 however
#: few subscribers it touches — "small but completely dark" is not a P4.
TOTAL_OUTAGE_MIN_PRIORITY = "P2"


class PrioritisationService:
    def __init__(
        self,
        *,
        policies: Policies,
        clock: Clock,
        decisions: DecisionLog,
        events: EventPublisher,
        cases: CaseWriter | None = None,
        scorer: PriorityScorer | None = None,
        model_blend: float = 0.0,
    ) -> None:
        self._policies = policies
        self._clock = clock
        self._decisions = decisions
        self._events = events
        self._cases = cases
        self._scorer = scorer
        self._blend = max(0.0, min(model_blend, MAX_MODEL_BLEND))

    # ---------------------------------------------------------- scoring

    def score(self, inputs: PriorityInputs, *, total_outage: bool = False) -> PriorityDecision:
        """Score an incident. Pure: no I/O, so it is trivially testable and replayable."""
        weights = self._policies.prioritisation.weights
        contributions: list[FactorContribution] = []
        for factor, weight in sorted(weights.items()):
            raw = self._raw_value(inputs, factor)
            contributions.append(
                FactorContribution(
                    factor=factor,
                    raw_value=raw,
                    normalised=_normalise(factor, raw),
                    weight=weight,
                ),
            )

        rule_score = round(sum(c.contribution for c in contributions), 4)
        model_version: str | None = None
        score = rule_score
        if self._scorer is not None and self._blend > 0:
            model_score, model_version = self._scorer.score(
                {c.factor: c.normalised for c in contributions},
            )
            score = round((1 - self._blend) * rule_score + self._blend * model_score, 4)

        priority = self._policies.priority_for(score)
        overrides: list[str] = []

        if inputs.sla_risk >= SLA_OVERRIDE_RISK and inputs.enterprise_accounts > 0:
            if priority != "P1":
                overrides.append(
                    f"SLA risk {inputs.sla_risk:.2f} with {inputs.enterprise_accounts} "
                    "enterprise account(s): raised to P1",
                )
            priority = "P1"
        if total_outage and priority in ("P3", "P4"):
            overrides.append("total outage on every affected cell: raised to P2")
            priority = TOTAL_OUTAGE_MIN_PRIORITY
        if inputs.is_silent_sufferer and priority == "P4":
            # Nobody complained, so the arithmetic under-reads it; degraded service is
            # still degraded service.
            overrides.append("degraded KPIs with no complaints: raised to P3")
            priority = "P3"

        return PriorityDecision(
            score=score,
            priority=priority,
            inputs=inputs,
            contributions=tuple(contributions),
            rule_version=self._policies.rule_version,
            model_version=model_version,
            decided_at=self._clock.now(),
            overrides=tuple(overrides),
        )

    # ------------------------------------------------------ orchestration

    def prioritise_incident(
        self,
        *,
        incident_id: str,
        inputs: PriorityInputs,
        case_id: str | None = None,
        total_outage: bool = False,
    ) -> PriorityDecision:
        """Score, persist, audit and publish `CasePrioritized`."""
        decision = self.score(inputs, total_outage=total_outage)

        if self._cases is not None and case_id:
            self._cases.set_priority(
                case_id,
                priority=decision.priority,
                score=decision.score,
                decision=decision.to_dict(),
                rule_version=decision.rule_version,
                model_version=decision.model_version,
            )

        self._decisions.record(
            action="prioritise",
            entity_type="incident",
            entity_id=incident_id,
            outcome=decision.priority,
            inputs=decision.to_dict(),
            confidence=decision.score,
            rule_version=decision.rule_version,
        )
        self._events.publish(
            CasePrioritized(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=incident_id,
                occurred_at=decision.decided_at or self._clock.now(),
                dedupe_key=f"{incident_id}:{decision.priority}:{decision.score}",
                payload={
                    "priority": decision.priority,
                    "score": decision.score,
                    "case_id": case_id,
                    "rule_version": decision.rule_version,
                    "overrides": list(decision.overrides),
                },
            ),
        )
        _log.info(
            "incident_prioritised",
            extra={
                "incident_id": incident_id,
                "priority": decision.priority,
                "score": decision.score,
            },
        )
        return decision

    # ----------------------------------------------------------- helpers

    def _raw_value(self, inputs: PriorityInputs, factor: str) -> float:
        match factor:
            case "affected_subscribers":
                return float(inputs.affected_subscribers)
            case "enterprise_accounts":
                return float(inputs.enterprise_accounts)
            case "duration_minutes":
                return float(inputs.duration_minutes)
            case "sla_risk":
                return float(inputs.sla_risk)
            case "repeat_complaints":
                return float(inputs.repeat_complaints)
            case "complaint_count":
                return float(inputs.complaint_count)
            case _:
                # An unknown weight is a configuration mistake, but it must not crash the
                # pipeline at 03:00: it contributes nothing and says so in the log.
                _log.warning("unknown_priority_factor", extra={"factor": factor})
                return 0.0


def _normalise(factor: str, raw: float) -> float:
    """Map a raw value onto [0, 1] against its saturation point.

    `sla_risk` is already a ratio, so it passes through clamped.
    """
    if factor == "sla_risk":
        return max(0.0, min(raw, 1.0))
    saturation = SATURATION.get(factor)
    if not saturation:
        return max(0.0, min(raw, 1.0))
    return round(max(0.0, min(raw / saturation, 1.0)), 4)
