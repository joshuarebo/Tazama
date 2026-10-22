"""The persisted case lifecycle, declared once (ADR 0004).

Every status change in the system goes through `advance()`. An illegal transition raises
rather than silently writing a status, and the transition table below is the documentation
— there is no second place where "what can follow what" is encoded.

Design notes:
  * Transitions are `(state, trigger) -> state`, so a trigger that does not apply in the
    current state is a clear error naming both.
  * Each transition declares the event it emits, so the outbox cannot drift from the state.
  * Terminal states are explicit. `CLOSED` can still be reopened, because verification can
    fail after closure (PRD section 8) — that is a transition, not a special case.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tazama.domain.events import EventType


class CaseState(StrEnum):
    """Where a complaint/incident case is in its life."""

    RECEIVED = "received"
    VALIDATED = "validated"
    ENRICHED = "enriched"
    LOCATED = "located"
    CORRELATED = "correlated"
    DIAGNOSING = "diagnosing"
    PRIORITISED = "prioritised"
    ROUTED = "routed"
    NOTIFYING = "notifying"
    MITIGATING = "mitigating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"
    #: Parked for a human: low confidence, policy refusal, or an unresolvable location.
    HUMAN_REVIEW = "human_review"
    #: Deliberately not an incident: planned maintenance, non-network, duplicate.
    SUPPRESSED = "suppressed"


class Trigger(StrEnum):
    VALIDATE = "validate"
    ENRICH = "enrich"
    LOCATE = "locate"
    CORRELATE = "correlate"
    DIAGNOSE = "diagnose"
    PRIORITISE = "prioritise"
    ROUTE = "route"
    PROPOSE_NOTIFICATION = "propose_notification"
    RECOMMEND_RECOVERY = "recommend_recovery"
    VERIFY = "verify"
    RESOLVE = "resolve"
    CLOSE = "close"
    REOPEN = "reopen"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    RESUME_FROM_HUMAN = "resume_from_human"
    SUPPRESS = "suppress"


class IllegalTransition(RuntimeError):
    """The lifecycle refused a status change."""

    def __init__(self, state: CaseState, trigger: Trigger) -> None:
        super().__init__(
            f"cannot {trigger} a case in state {state}; "
            f"allowed from here: {', '.join(sorted(t for t in allowed_triggers(state))) or 'none'}",
        )
        self.state = state
        self.trigger = trigger


@dataclass(frozen=True, slots=True)
class Transition:
    source: CaseState
    trigger: Trigger
    target: CaseState
    emits: EventType | None = None
    note: str = ""


#: The whole lifecycle. Read this table to understand the product's flow.
TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        CaseState.RECEIVED, Trigger.VALIDATE, CaseState.VALIDATED, EventType.COMPLAINT_VALIDATED
    ),
    Transition(
        CaseState.VALIDATED, Trigger.ENRICH, CaseState.ENRICHED, EventType.COMPLAINT_ENRICHED
    ),
    Transition(CaseState.ENRICHED, Trigger.LOCATE, CaseState.LOCATED, EventType.SITE_IDENTIFIED),
    Transition(CaseState.LOCATED, Trigger.CORRELATE, CaseState.CORRELATED, EventType.CORRELATED),
    # A degradation-first case enters at CORRELATED: there is no complaint to validate.
    Transition(
        CaseState.CORRELATED, Trigger.DIAGNOSE, CaseState.DIAGNOSING, EventType.DIAGNOSIS_COMPLETED
    ),
    Transition(
        CaseState.DIAGNOSING, Trigger.PRIORITISE, CaseState.PRIORITISED, EventType.CASE_PRIORITIZED
    ),
    # Diagnosis is optional: rules can prioritise straight from correlation.
    Transition(
        CaseState.CORRELATED, Trigger.PRIORITISE, CaseState.PRIORITISED, EventType.CASE_PRIORITIZED
    ),
    Transition(CaseState.PRIORITISED, Trigger.ROUTE, CaseState.ROUTED, EventType.CASE_ROUTED),
    Transition(
        CaseState.ROUTED,
        Trigger.PROPOSE_NOTIFICATION,
        CaseState.NOTIFYING,
        EventType.NOTIFICATION_PROPOSED,
    ),
    Transition(
        CaseState.ROUTED,
        Trigger.RECOMMEND_RECOVERY,
        CaseState.MITIGATING,
        EventType.RECOVERY_RECOMMENDED,
    ),
    Transition(
        CaseState.NOTIFYING,
        Trigger.RECOMMEND_RECOVERY,
        CaseState.MITIGATING,
        EventType.RECOVERY_RECOMMENDED,
    ),
    Transition(CaseState.MITIGATING, Trigger.VERIFY, CaseState.VERIFYING),
    Transition(CaseState.NOTIFYING, Trigger.VERIFY, CaseState.VERIFYING),
    Transition(CaseState.ROUTED, Trigger.VERIFY, CaseState.VERIFYING),
    Transition(
        CaseState.VERIFYING,
        Trigger.RESOLVE,
        CaseState.RESOLVED,
        EventType.VERIFICATION_COMPLETED,
        note="KPIs recovered, alarms cleared and the quiet period passed",
    ),
    Transition(
        CaseState.VERIFYING,
        Trigger.REOPEN,
        CaseState.REOPENED,
        EventType.CASE_REOPENED,
        note="verification failed: the problem is still there",
    ),
    Transition(CaseState.RESOLVED, Trigger.CLOSE, CaseState.CLOSED, EventType.CASE_CLOSED),
    Transition(
        CaseState.CLOSED,
        Trigger.REOPEN,
        CaseState.REOPENED,
        EventType.CASE_REOPENED,
        note="a closed case can come back: recurrence or a failed fix",
    ),
    Transition(CaseState.REOPENED, Trigger.CORRELATE, CaseState.CORRELATED, EventType.CORRELATED),
    Transition(CaseState.REOPENED, Trigger.DIAGNOSE, CaseState.DIAGNOSING),
    Transition(CaseState.REOPENED, Trigger.PRIORITISE, CaseState.PRIORITISED),
    # Suppression is reachable wherever the evidence says "this is not an incident".
    Transition(
        CaseState.LOCATED,
        Trigger.SUPPRESS,
        CaseState.SUPPRESSED,
        note="planned maintenance, duplicate or non-network",
    ),
    Transition(CaseState.CORRELATED, Trigger.SUPPRESS, CaseState.SUPPRESSED),
    Transition(CaseState.ENRICHED, Trigger.SUPPRESS, CaseState.SUPPRESSED),
    Transition(CaseState.VALIDATED, Trigger.SUPPRESS, CaseState.SUPPRESSED),
    Transition(CaseState.RECEIVED, Trigger.SUPPRESS, CaseState.SUPPRESSED),
    Transition(CaseState.DIAGNOSING, Trigger.SUPPRESS, CaseState.SUPPRESSED),
)

#: Human review is reachable from any non-terminal state, and returns to it afterwards.
_HUMAN_REVIEWABLE: frozenset[CaseState] = frozenset(
    {
        CaseState.RECEIVED,
        CaseState.VALIDATED,
        CaseState.ENRICHED,
        CaseState.LOCATED,
        CaseState.CORRELATED,
        CaseState.DIAGNOSING,
        CaseState.PRIORITISED,
        CaseState.ROUTED,
        CaseState.NOTIFYING,
        CaseState.MITIGATING,
        CaseState.VERIFYING,
        CaseState.REOPENED,
    },
)

#: No trigger leaves these. `CLOSED` is not here: a closed case can be reopened.
TERMINAL_STATES: frozenset[CaseState] = frozenset({CaseState.SUPPRESSED})


def _build_table() -> dict[tuple[CaseState, Trigger], Transition]:
    table: dict[tuple[CaseState, Trigger], Transition] = {}
    for transition in TRANSITIONS:
        key = (transition.source, transition.trigger)
        if key in table:
            raise AssertionError(f"duplicate transition declared for {key}")
        table[key] = transition
    for state in _HUMAN_REVIEWABLE:
        table[(state, Trigger.ESCALATE_TO_HUMAN)] = Transition(
            source=state,
            trigger=Trigger.ESCALATE_TO_HUMAN,
            target=CaseState.HUMAN_REVIEW,
            emits=EventType.HUMAN_REVIEW_REQUESTED,
            note="confidence below threshold, or policy requires a human",
        )
    # Returning from review resumes the stage that escalated, recorded on the case.
    table[(CaseState.HUMAN_REVIEW, Trigger.RESUME_FROM_HUMAN)] = Transition(
        source=CaseState.HUMAN_REVIEW,
        trigger=Trigger.RESUME_FROM_HUMAN,
        target=CaseState.CORRELATED,
        note="a human resolved the ambiguity; re-enter the pipeline at correlation",
    )
    table[(CaseState.HUMAN_REVIEW, Trigger.SUPPRESS)] = Transition(
        source=CaseState.HUMAN_REVIEW,
        trigger=Trigger.SUPPRESS,
        target=CaseState.SUPPRESSED,
        note="a human judged it not an incident",
    )
    return table


TRANSITION_TABLE: dict[tuple[CaseState, Trigger], Transition] = _build_table()


def allowed_triggers(state: CaseState) -> frozenset[Trigger]:
    return frozenset(trigger for (source, trigger) in TRANSITION_TABLE if source == state)


def can(state: CaseState, trigger: Trigger) -> bool:
    return (state, trigger) in TRANSITION_TABLE


def advance(state: CaseState, trigger: Trigger) -> Transition:
    """Apply a trigger, or refuse it.

    Returns the transition (target state plus the event to emit) so the caller writes the
    state change and its event in one transaction.
    """
    transition = TRANSITION_TABLE.get((state, trigger))
    if transition is None:
        raise IllegalTransition(state, trigger)
    return transition


def is_terminal(state: CaseState) -> bool:
    return state in TERMINAL_STATES


def is_open(state: CaseState) -> bool:
    """Open means "still needs attention" — what the Command Center counts."""
    return state not in {CaseState.CLOSED, CaseState.SUPPRESSED, CaseState.RESOLVED}


def reachable_states() -> frozenset[CaseState]:
    """Every state reachable from RECEIVED or CORRELATED (the two entry points)."""
    seen: set[CaseState] = set()
    frontier = [CaseState.RECEIVED, CaseState.CORRELATED]
    while frontier:
        state = frontier.pop()
        if state in seen:
            continue
        seen.add(state)
        for (source, _), transition in TRANSITION_TABLE.items():
            if source == state and transition.target not in seen:
                frontier.append(transition.target)
    return frozenset(seen)
