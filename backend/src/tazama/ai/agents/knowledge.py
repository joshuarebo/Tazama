"""KnowledgeAssistant: answer an operational question, or decline.

The design constraint that shapes everything here: **this agent must be willing to say it
does not know.** A knowledge assistant that always answers is worse than no assistant,
because an engineer under pressure acts on a confident wrong answer. So:

* Retrieval happens first and the answer is built *from* the retrieved chunks. No chunks,
  no answer — the agent declines rather than drawing on whatever the model remembers about
  mobile networks in general.
* Every answer carries citations, and a citation that names no document is a validation
  error, not a cosmetic issue.
* `sufficient_evidence=False` from the model is honoured: it becomes a decline, not a
  hedged answer.
* An answer whose claims cite documents the agent never retrieved is rejected outright.

Tier ceiling is `read`. This agent holds no tool that can propose anything, let alone act.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tazama.ai.agents.harness import AgentTrace, Budget, BudgetExhausted, ToolRegistry
from tazama.ai.agents.tools import CEILINGS, GRANTS
from tazama.ai.llm.gateway import LlmGateway
from tazama.ai.llm.schemas import KnowledgeAnswer
from tazama.core.logging import get_logger

_log = get_logger(__name__)

AGENT = "KnowledgeAssistant"

#: The honest refusal. Short, and it tells the engineer what to do next.
DECLINE = (
    "I do not have documentation that answers this. Check with the on-call engineer "
    "rather than acting on a guess."
)

_SYSTEM = """\
You answer operational questions for network engineers using ONLY the documentation
excerpts you are given.

Rules you must follow:
- Base every statement on the excerpts. If they do not answer the question, set
  sufficient_evidence to false and say what is missing.
- Cite the doc_id of every excerpt you used. Do not cite an excerpt you did not use, and
  never cite a document you were not shown.
- Prefer a short answer that is certain over a long one that is plausible.
- Do not invent runbook ids, thresholds, or commands.
- You never execute anything and never contact a customer.
"""


@dataclass(frozen=True, slots=True)
class KnowledgeOutcome:
    question: str
    answer: str
    citations: tuple[dict[str, Any], ...]
    sufficient: bool
    method: str
    trace: AgentTrace

    @property
    def declined(self) -> bool:
        return not self.sufficient

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": list(self.citations),
            "sufficient_evidence": self.sufficient,
            "declined": self.declined,
            "method": self.method,
            "trace": self.trace.to_dict(),
        }


class KnowledgeAssistant:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gateway: LlmGateway | None = None,
        budget: Budget | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._budget = budget or Budget(max_steps=3, max_tool_calls=4, max_seconds=20.0)

    def ask(self, question: str, *, limit: int = 4) -> KnowledgeOutcome:
        """Answer or decline. Never raises — a budget firing is a stop rule, not an error."""
        steps: list[str] = []
        calls: list[Any] = []
        try:
            return self._run(question, limit, steps, calls)
        except BudgetExhausted as stop:
            steps.append(f"stopped: {stop}")
            return self._decline(question, steps, calls, Budget(), str(stop))

    def _run(
        self,
        question: str,
        limit: int,
        steps: list[str],
        calls: list[Any],
    ) -> KnowledgeOutcome:
        budget = Budget(
            max_steps=self._budget.max_steps,
            max_tool_calls=self._budget.max_tool_calls,
            max_seconds=self._budget.max_seconds,
        )
        budget.tick_step()
        steps.append("retrieve")
        record = self._registry.invoke(
            "search_knowledge",
            {"query": question[:300], "limit": limit},
            allow=GRANTS[AGENT],
            max_tier=CEILINGS[AGENT],  # type: ignore[arg-type]
            budget=budget,
        )
        calls.append(record)
        chunks = list(record.result or []) if record.allowed and not record.error else []

        if not chunks:
            # No retrieval, no answer. The alternative — letting the model answer from
            # training data — is how an engineer ends up following a procedure that
            # belongs to a different operator's network.
            steps.append("nothing retrieved; declining")
            return self._decline(question, steps, calls, budget, "no documentation retrieved")

        if self._gateway is None or not self._gateway.configured:
            # The deterministic path: hand back the best excerpt verbatim with its
            # citation. Less fluent, entirely faithful.
            steps.append("no model; returning the retrieved excerpt verbatim")
            best = chunks[0]
            return KnowledgeOutcome(
                question=question,
                answer=str(best.get("text", ""))[:1_200],
                citations=tuple(
                    {"doc_id": str(c.get("doc_id", "")), "chunk": c.get("chunk")}
                    for c in chunks[:3]
                ),
                sufficient=bool(str(best.get("text", "")).strip()),
                method="retrieval-only",
                trace=self._trace(
                    question,
                    steps,
                    calls,
                    budget,
                    "no model configured",
                    degraded=True,
                ),
            )

        budget.tick_step()
        steps.append("ask the model")
        result = self._gateway.complete(
            task="knowledge_answer",
            system=_SYSTEM,
            user=_prompt(question, chunks),
            prompt_version="knowledge-v1",
        )

        if not isinstance(result.parsed, KnowledgeAnswer):
            steps.append("model unusable; returning the excerpt verbatim")
            best = chunks[0]
            return KnowledgeOutcome(
                question=question,
                answer=str(best.get("text", ""))[:1_200],
                citations=tuple({"doc_id": str(c.get("doc_id", ""))} for c in chunks[:3]),
                sufficient=bool(str(best.get("text", "")).strip()),
                method="retrieval-only",
                trace=self._trace(
                    question, steps, calls, budget, result.note or "model degraded", degraded=True,
                ),
            )

        answer = result.parsed
        retrieved_ids = {str(c.get("doc_id", "")) for c in chunks}
        cited_ids = {str(c.get("doc_id", "")) for c in answer.citations}
        invented = cited_ids - retrieved_ids
        if invented:
            # A citation to a document that was never retrieved is a fabrication wearing
            # the costume of a source, and it is *more* dangerous than an uncited answer
            # because it passes a reviewer's glance.
            steps.append(f"rejected: cited documents that were not retrieved ({sorted(invented)})")
            _log.warning("knowledge_fabricated_citation", extra={"invented": sorted(invented)})
            return self._decline(
                question, steps, calls, budget, "the answer cited documents that do not exist",
            )

        if not answer.sufficient_evidence or not answer.citations:
            steps.append("model reported insufficient evidence")
            return self._decline(question, steps, calls, budget, "insufficient evidence")

        return KnowledgeOutcome(
            question=question,
            answer=answer.answer,
            citations=tuple(answer.citations),
            sufficient=True,
            method="llm",
            trace=self._trace(question, steps, calls, budget, "answered with citations"),
        )

    def _decline(
        self,
        question: str,
        steps: list[str],
        calls: list[Any],
        budget: Budget,
        why: str,
    ) -> KnowledgeOutcome:
        return KnowledgeOutcome(
            question=question,
            answer=DECLINE,
            citations=(),
            sufficient=False,
            method="declined",
            trace=self._trace(question, steps, calls, budget, why, degraded=True),
        )

    def _trace(
        self,
        question: str,
        steps: list[str],
        calls: list[Any],
        budget: Budget,
        stopped: str,
        *,
        degraded: bool = False,
    ) -> AgentTrace:
        return AgentTrace(
            agent=AGENT,
            goal=f"answer: {question[:120]}",
            steps=tuple(steps),
            tool_calls=tuple(calls),
            budget=budget.summary(),
            stopped_because=stopped,
            degraded=degraded,
        )


def _prompt(question: str, chunks: list[dict[str, Any]]) -> str:
    """Excerpts first, question restated last.

    The excerpts are the context and the question is the goal, and the goal belongs at the
    tail where a model attends to it most reliably.
    """
    lines = ["Documentation excerpts:", ""]
    for index, chunk in enumerate(chunks, start=1):
        owner = chunk.get("owner") or "unknown owner"
        expires = chunk.get("expires_at") or "no expiry recorded"
        lines += [
            f"[{index}] doc_id={chunk.get('doc_id')} owner={owner} expires={expires}",
            str(chunk.get("text", ""))[:2_000],
            "",
        ]
    lines += [
        "GOAL: answer the following question using only the excerpts above, citing the "
        "doc_id of each one you use. If they do not answer it, set sufficient_evidence to "
        "false.",
        "",
        f"QUESTION: {question}",
    ]
    return "\n".join(lines)
