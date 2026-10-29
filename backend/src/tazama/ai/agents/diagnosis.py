"""DiagnosisAgent: why is this incident happening, and what should be done about it.

The five agent layers, made concrete here (`docs/architecture/agent-layers.md`):

* **Context.** A stable system prompt first (the gateway owns it, so it is byte-identical
  on every call and the provider's cache can hold it), then the evidence, then the goal
  restated at the tail. Evidence is passed by value but *summarised* — the agent gets
  counts and extremes, not 720 KPI rows, because a model handed a wall of numbers answers
  about the wall.
* **Loop.** A deterministic filter runs first and often answers alone. The model is only
  asked when the filter is unsure, the loop is bounded by the harness, and an external
  checker decides when it is done — never the model.
* **Cheap-decision gate.** The fast model triages; the reasoning model only writes when
  triage says it is worth it. Unsure escalates, which means a human, not a retry.
* **Harness.** Tool tier ceiling `propose`, so the agent can recommend a runbook and
  nothing more. No tool it holds can act.
* **Evals.** Diagnosis accuracy is measured against the seeded ground truth in
  `ai/evals/`, on every change.

The deterministic path is not a fallback bolted on afterwards — it is the primary path. The
seeded scenarios are diagnosable from the evidence by rule, and a rule that gets it right
is better than a model that usually does: it is explainable, free, and cannot be talked
into a different answer by the text of a complaint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tazama.ai.agents.harness import AgentTrace, Budget, BudgetExhausted, ToolRegistry
from tazama.ai.agents.tools import CEILINGS, GRANTS
from tazama.ai.llm.gateway import LlmGateway
from tazama.ai.llm.schemas import Diagnosis
from tazama.core.logging import get_logger
from tazama.domain.value_objects import IncidentCause

_log = get_logger(__name__)

AGENT = "DiagnosisAgent"

#: Below this, the answer goes to a human rather than onto the card as a conclusion.
CONFIDENCE_FLOOR = 0.55

#: What the deterministic rules need before they will claim a cause on their own. Above
#: this the model is not consulted at all — not to save money, but because a rule that is
#: certain is a better answer than a model that agrees.
RULE_CERTAINTY = 0.80

_SYSTEM = """\
You diagnose mobile-network incidents from evidence that has already been gathered for you.

Rules you must follow:
- Choose a probable cause only from the enum in the schema.
- Every cause you give must be supported by at least one piece of evidence you were
  actually shown. Do not infer an alarm or a KPI that is not in the evidence.
- If the evidence does not distinguish between causes, say so with a low confidence rather
  than picking the most dramatic one.
- Recommend a runbook only from the catalogue you were given, by id.
- You never execute anything. Your output is a recommendation for a human engineer.
"""


@dataclass(frozen=True, slots=True)
class DiagnosisOutcome:
    """The conclusion, how it was reached, and whether a human is needed."""

    incident_id: str
    cause: IncidentCause | None
    confidence: float
    evidence: tuple[dict[str, Any], ...]
    summary: str
    runbook_id: str | None
    method: str
    trace: AgentTrace
    needs_human: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "cause": str(self.cause) if self.cause else None,
            "confidence": round(self.confidence, 4),
            "evidence": list(self.evidence),
            "summary": self.summary,
            "runbook_id": self.runbook_id,
            "method": self.method,
            "needs_human": self.needs_human,
            "trace": self.trace.to_dict(),
        }


class DiagnosisAgent:
    """Bounded, tool-using, and unable to act."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gateway: LlmGateway | None = None,
        budget: Budget | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._budget_template = budget or Budget(max_steps=4, max_tool_calls=10, max_seconds=25.0)

    def diagnose(self, incident_id: str) -> DiagnosisOutcome:
        """Diagnose, or say why not. Never raises.

        A budget firing is a *stop rule*, not an error: the loop was supposed to end, and
        the caller still needs an answer. Letting `BudgetExhausted` escape would turn a
        working safety mechanism into an outage in the pipeline step that called it.
        """
        state: dict[str, Any] = {"steps": [], "calls": [], "ruled": None, "runbooks": []}
        try:
            return self._run(incident_id, state)
        except BudgetExhausted as stop:
            state["steps"].append(f"stopped: {stop}")
            return self._from_rules_or_unknown(
                incident_id,
                state["ruled"],
                state["runbooks"],
                state["steps"],
                state["calls"],
                self._exhausted_budget(),
                str(stop),
            )

    def _exhausted_budget(self) -> Budget:
        """A budget object for the trace when the real one has already fired."""
        spent = Budget(
            max_steps=self._budget_template.max_steps,
            max_tool_calls=self._budget_template.max_tool_calls,
            max_seconds=self._budget_template.max_seconds,
        )
        spent.steps = spent.max_steps
        spent.tool_calls = spent.max_tool_calls
        return spent

    def _run(self, incident_id: str, state: dict[str, Any]) -> DiagnosisOutcome:
        budget = Budget(
            max_steps=self._budget_template.max_steps,
            max_tool_calls=self._budget_template.max_tool_calls,
            max_seconds=self._budget_template.max_seconds,
        )
        steps: list[str] = state["steps"]
        calls: list[Any] = state["calls"]
        allow = GRANTS[AGENT]
        ceiling = CEILINGS[AGENT]

        def call(name: str, **arguments: Any) -> Any:
            record = self._registry.invoke(
                name,
                arguments,
                allow=allow,
                max_tier=ceiling,  # type: ignore[arg-type]
                budget=budget,
            )
            calls.append(record)
            return record.result if record.allowed and not record.error else None

        budget.tick_step()
        steps.append("gather evidence")
        incident = call("get_incident", incident_id=incident_id)
        if incident is None:
            return self._unknown(incident_id, steps, calls, budget, "incident not found")

        alarms = call("get_alarms", incident_id=incident_id) or []
        topology = call("get_topology", incident_id=incident_id) or []
        maintenance = call("get_maintenance", incident_id=incident_id) or []
        complaints = call("get_complaint_summary", incident_id=incident_id) or {}
        runbooks = call("list_runbooks") or []
        state["runbooks"] = runbooks

        budget.tick_step()
        steps.append("deterministic rules")
        ruled = state["ruled"] = _rule_diagnosis(
            incident=incident,
            alarms=alarms,
            topology=topology,
            maintenance=maintenance,
            complaints=complaints,
        )

        # The external checker: a rule that is certain *is* the answer. The model is asked
        # only where the rules genuinely cannot separate two causes.
        if ruled is not None and ruled[1] >= RULE_CERTAINTY:
            cause, confidence, evidence, summary = ruled
            return self._finish(
                incident_id=incident_id,
                cause=cause,
                confidence=confidence,
                evidence=evidence,
                summary=summary,
                runbooks=runbooks,
                method="rules",
                steps=steps,
                calls=calls,
                budget=budget,
                stopped="deterministic rules were conclusive",
            )

        if self._gateway is None or not self._gateway.configured:
            return self._from_rules_or_unknown(
                incident_id, ruled, runbooks, steps, calls, budget, "no model configured",
            )

        budget.tick_step()
        steps.append("ask the model")
        result = self._gateway.complete(
            task="diagnose_incident",
            system=_SYSTEM,
            user=_evidence_prompt(
                incident=incident,
                alarms=alarms,
                topology=topology,
                maintenance=maintenance,
                complaints=complaints,
                runbooks=runbooks,
                rule_hint=ruled,
            ),
            prompt_version="diagnosis-v1",
        )

        if not isinstance(result.parsed, Diagnosis):
            steps.append("model unusable; using the deterministic answer")
            return self._from_rules_or_unknown(
                incident_id, ruled, runbooks, steps, calls, budget, result.note or "model degraded",
            )

        verified, note = _verify(result.parsed, alarms=alarms, topology=topology)
        if not verified:
            # Grounding check: a cause whose evidence does not exist in what the agent was
            # shown is a fabrication, however confident it sounds.
            steps.append(f"model output rejected: {note}")
            _log.warning("diagnosis_ungrounded", extra={"incident_id": incident_id, "note": note})
            return self._from_rules_or_unknown(
                incident_id, ruled, runbooks, steps, calls, budget, note,
            )

        return self._finish(
            incident_id=incident_id,
            cause=result.parsed.probable_cause,
            confidence=result.parsed.confidence,
            evidence=tuple(item.model_dump() for item in result.parsed.evidence),
            summary=result.parsed.summary,
            runbooks=runbooks,
            method="llm",
            steps=steps,
            calls=calls,
            budget=budget,
            stopped="model answered and the answer was grounded",
            proposed_runbook=result.parsed.recommended_runbook_id,
        )

    # ------------------------------------------------------------------ helpers

    def _finish(
        self,
        *,
        incident_id: str,
        cause: IncidentCause | None,
        confidence: float,
        evidence: tuple[dict[str, Any], ...],
        summary: str,
        runbooks: list[dict[str, Any]],
        method: str,
        steps: list[str],
        calls: list[Any],
        budget: Budget,
        stopped: str,
        proposed_runbook: str | None = None,
    ) -> DiagnosisOutcome:
        runbook_id = _pick_runbook(cause, runbooks, preferred=proposed_runbook)
        needs_human = confidence < CONFIDENCE_FLOOR or cause is None

        if runbook_id and not needs_human:
            # The one `propose` call the agent is allowed. It writes nothing: the proposal
            # is returned for a human to approve (PRD §11).
            record = self._registry.invoke(
                "propose_runbook",
                {
                    "incident_id": incident_id,
                    "runbook_id": runbook_id,
                    "justification": (
                        summary
                        or f"diagnosed as {cause} with confidence {confidence:.2f}"
                    ),
                },
                allow=GRANTS[AGENT],
                max_tier=CEILINGS[AGENT],  # type: ignore[arg-type]
                budget=budget,
            )
            calls.append(record)
            steps.append("propose a runbook for approval")

        return DiagnosisOutcome(
            incident_id=incident_id,
            cause=cause,
            confidence=confidence,
            evidence=evidence,
            summary=summary,
            runbook_id=runbook_id,
            method=method,
            needs_human=needs_human,
            trace=AgentTrace(
                agent=AGENT,
                goal=f"diagnose {incident_id}",
                steps=tuple(steps),
                tool_calls=tuple(calls),
                budget=budget.summary(),
                stopped_because=stopped,
                degraded=method != "llm",
                confidence=confidence,
                escalated=needs_human,
            ),
        )

    def _from_rules_or_unknown(
        self,
        incident_id: str,
        ruled: tuple[IncidentCause, float, tuple[dict[str, Any], ...], str] | None,
        runbooks: list[dict[str, Any]],
        steps: list[str],
        calls: list[Any],
        budget: Budget,
        note: str,
    ) -> DiagnosisOutcome:
        if ruled is None:
            return self._unknown(incident_id, steps, calls, budget, note)
        cause, confidence, evidence, summary = ruled
        return self._finish(
            incident_id=incident_id,
            cause=cause,
            confidence=confidence,
            evidence=evidence,
            summary=summary,
            runbooks=runbooks,
            method="rules",
            steps=steps,
            calls=calls,
            budget=budget,
            stopped=f"deterministic answer used ({note})",
        )

    def _unknown(
        self,
        incident_id: str,
        steps: list[str],
        calls: list[Any],
        budget: Budget,
        note: str,
    ) -> DiagnosisOutcome:
        """No cause. Said plainly, and routed to a human."""
        return DiagnosisOutcome(
            incident_id=incident_id,
            cause=None,
            confidence=0.0,
            evidence=(),
            summary="",
            runbook_id=None,
            method="none",
            needs_human=True,
            trace=AgentTrace(
                agent=AGENT,
                goal=f"diagnose {incident_id}",
                steps=tuple(steps),
                tool_calls=tuple(calls),
                budget=budget.summary(),
                stopped_because=note,
                degraded=True,
                confidence=0.0,
                escalated=True,
            ),
        )


# ====================================================== the deterministic path


def _rule_diagnosis(
    *,
    incident: dict[str, Any],
    alarms: list[dict[str, Any]],
    topology: list[dict[str, Any]],
    maintenance: list[dict[str, Any]],
    complaints: dict[str, Any],
) -> tuple[IncidentCause, float, tuple[dict[str, Any], ...], str] | None:
    """Diagnose from evidence by rule, in order of how conclusive each signal is.

    The order is the point. A maintenance window that covers the incident explains it
    completely, so it is checked first and nothing else matters. A transmission alarm with
    several cells on one upstream node is next, because it explains the *shape* as well as
    the fault. Congestion is last among the positives because high utilisation accompanies
    plenty of other faults.
    """
    evidence: list[dict[str, Any]] = []

    # 1. Planned maintenance explains everything and suppresses the rest.
    if maintenance:
        window = maintenance[0]
        return (
            IncidentCause.PLANNED_MAINTENANCE,
            0.95,
            (
                {
                    "kind": "maintenance",
                    "ref": str(window.get("id", "maintenance")),
                    "note": "a planned maintenance window covers this incident",
                },
            ),
            "Planned maintenance covers the affected window; no fault is implied.",
        )

    alarm_types = [str(a.get("alarm_type", "")) for a in alarms]
    critical = [a for a in alarms if str(a.get("severity", "")) == "critical"]
    shared_upstream = [
        node for node in topology if int(node.get("affected_cells", 0) or 0) > 1
    ]

    # 2. A transmission fault on a shared node: explains the fault and the fan-out.
    if any("transmission" in kind or "link" in kind for kind in alarm_types) and shared_upstream:
        node = shared_upstream[0]
        evidence = [
            {
                "kind": "alarm",
                "ref": str(alarms[0].get("id", "alarm")),
                "note": f"{alarm_types[0]} raised on the upstream path",
            },
            {
                "kind": "topology",
                "ref": str(node.get("node_id", "node")),
                "note": f"{node.get('affected_cells')} affected cells share this node",
            },
        ]
        return (
            IncidentCause.TRANSMISSION_LINK_FAILURE,
            0.90,
            tuple(evidence),
            "A transmission alarm on a node shared by the affected cells explains both the "
            "outage and its footprint.",
        )

    # 3. Power: unambiguous when the alarm says so.
    if any("power" in kind or "mains" in kind or "battery" in kind for kind in alarm_types):
        return (
            IncidentCause.POWER_FAILURE,
            0.88,
            (
                {
                    "kind": "alarm",
                    "ref": str(alarms[0].get("id", "alarm")),
                    "note": "a power alarm is active on the affected site",
                },
            ),
            "A power alarm on the affected site accounts for the loss of service.",
        )

    # 4. A single cell down with a critical alarm.
    if critical and int(incident.get("affected_cells", 0) or 0) <= 2:
        return (
            IncidentCause.CELL_OUTAGE,
            0.85,
            (
                {
                    "kind": "alarm",
                    "ref": str(critical[0].get("id", "alarm")),
                    "note": f"critical {critical[0].get('alarm_type')} on the affected cell",
                },
            ),
            "A critical alarm on a small number of cells is a local cell outage.",
        )

    # 5. Flapping: the alarm pattern is the diagnosis.
    if any(bool(a.get("is_flapping")) for a in alarms):
        return (
            IncidentCause.FLAPPING,
            0.82,
            (
                {
                    "kind": "alarm",
                    "ref": str(alarms[0].get("id", "alarm")),
                    "note": "the alarm is raising and clearing repeatedly",
                },
            ),
            "A repeatedly raising and clearing alarm indicates an intermittent fault "
            "rather than a hard failure.",
        )

    # 6. Congestion: no alarm, but the complaints and the exposure say so. Lower
    #    confidence on purpose — congestion accompanies many other faults.
    if not alarms and int(complaints.get("total", 0) or 0) >= 3:
        return (
            IncidentCause.CONGESTION,
            0.70,
            (
                {
                    "kind": "complaint",
                    "ref": str(incident.get("id", "incident")),
                    "note": f"{complaints.get('total')} complaints with no alarm raised",
                },
            ),
            "Complaints without a corresponding alarm point at congestion rather than a "
            "hard fault, but this does not rule out a silent failure.",
        )

    return None


def _pick_runbook(
    cause: IncidentCause | None,
    runbooks: list[dict[str, Any]],
    *,
    preferred: str | None = None,
) -> str | None:
    """Match a cause to an approved runbook. Never invents an id.

    A model's suggestion is honoured only if it names a runbook that actually exists —
    otherwise the catalogue decides, which is the whole reason the catalogue is passed in.
    """
    catalogue = {str(book.get("id")): book for book in runbooks}
    if preferred and preferred in catalogue:
        return preferred
    if cause is None:
        return None
    for book_id, book in sorted(catalogue.items()):
        causes = book.get("causes") or book.get("triggers") or []
        if str(cause) in [str(item) for item in causes]:
            return book_id
    return None


def _verify(
    diagnosis: Diagnosis,
    *,
    alarms: list[dict[str, Any]],
    topology: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Is every piece of cited evidence something the agent was actually shown?

    This is the numeric-grounding check in its most useful form: not "does the prose read
    well" but "does this reference exist". A model that cites `ALM-9999` when no such alarm
    was provided has invented its justification, and a fabricated justification is worse
    than no diagnosis because it survives review.
    """
    known = {str(a.get("id", "")) for a in alarms}
    known |= {str(a.get("alarm_type", "")) for a in alarms}
    known |= {str(node.get("node_id", "")) for node in topology}
    known.discard("")

    for item in diagnosis.evidence:
        if item.kind in ("alarm", "topology") and item.ref not in known:
            return False, f"cited {item.kind} {item.ref!r} was not in the evidence provided"
    return True, ""


def _evidence_prompt(
    *,
    incident: dict[str, Any],
    alarms: list[dict[str, Any]],
    topology: list[dict[str, Any]],
    maintenance: list[dict[str, Any]],
    complaints: dict[str, Any],
    runbooks: list[dict[str, Any]],
    rule_hint: tuple[IncidentCause, float, tuple[dict[str, Any], ...], str] | None,
) -> str:
    """Compact evidence, with the goal restated at the tail.

    Summarised rather than dumped: a model handed 720 KPI rows answers about the rows. The
    goal goes last because that is the position a model attends to most reliably, and the
    whole point of putting the stable instructions first is that this part can change
    freely without invalidating the prompt cache.
    """
    lines = [
        "Diagnose this incident.",
        "",
        "INCIDENT",
        f"  id={incident.get('id')} region={incident.get('region')} "
        f"priority={incident.get('priority')} status={incident.get('status')}",
        f"  affected_cells={incident.get('affected_cells')} "
        f"affected_sites={incident.get('affected_sites')} "
        f"subscribers={incident.get('affected_subs')}",
        "",
        f"ALARMS ({len(alarms)})",
    ]
    lines.extend(
        f"  {a.get('id')} {a.get('alarm_type')} severity={a.get('severity')} "
        f"node={a.get('node_id')} flapping={bool(a.get('is_flapping'))}"
        for a in alarms[:12]
    )
    if not alarms:
        lines.append("  none — this fault is silent to the alarm system")

    lines += ["", f"SHARED UPSTREAM NODES ({len(topology)})"]
    lines.extend(
        f"  {node.get('node_id')} kind={node.get('kind')} "
        f"affected_cells={node.get('affected_cells')}"
        for node in topology[:8]
    )
    lines += ["", "MAINTENANCE"]
    lines.append(
        f"  {len(maintenance)} overlapping window(s)" if maintenance else "  none overlapping",
    )
    lines += [
        "",
        "COMPLAINTS",
        f"  total={complaints.get('total', 0)} categories={complaints.get('by_category', {})}",
        "",
        "APPROVED RUNBOOKS",
    ]
    lines.extend(
        f"  {book.get('id')}: {book.get('name', '')} for {book.get('causes', [])}"
        for book in runbooks[:12]
    )

    if rule_hint is not None:
        cause, confidence, _, _ = rule_hint
        lines += [
            "",
            "A deterministic rule suggests "
            f"{cause} at confidence {confidence:.2f}. Agree only if the evidence supports "
            "it; say so with low confidence if it does not.",
        ]

    lines += [
        "",
        "GOAL: name the probable cause from the enum, cite only evidence listed above by "
        "its exact reference, and recommend a runbook id from the catalogue above or none.",
    ]
    return "\n".join(lines)
