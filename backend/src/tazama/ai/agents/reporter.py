"""Reporter: turn figures into prose, and never let the prose invent a figure.

This is the agent most likely to do quiet damage. A diagnosis gets reviewed by an engineer
who knows the network; a daily digest gets skimmed by a manager who takes its numbers as
read. So the single rule here is **numeric grounding**: every number that appears in the
generated prose must be one of the numbers the agent was given.

The check is mechanical, not advisory. Numbers are extracted from the output, compared
against the facts that went in, and any number that cannot be accounted for causes the
whole generated digest to be discarded in favour of the deterministic one. That trade is
deliberate: a plainly-worded digest that is correct beats a well-written one that might
not be.

Tier ceiling `read`. The Reporter cannot propose anything — the digest it produces is
rendered by `modules/reporting`, which has its own no-PII gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tazama.ai.agents.harness import AgentTrace, Budget, BudgetExhausted, ToolRegistry
from tazama.ai.llm.gateway import LlmGateway
from tazama.ai.llm.schemas import ReportDigest
from tazama.core.logging import get_logger

_log = get_logger(__name__)

AGENT = "Reporter"

#: Numbers in prose, including thousands separators, decimals and percentages.
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

#: Relative tolerance when matching a number in the prose to a number in the facts. Covers
#: honest rounding ("about 1,200" for 1,203) without letting a different figure through.
_TOLERANCE = 0.01

#: Numbers too common to be evidence of anything. Rejecting a digest because it said
#: "the top 3 regions" would make the check useless rather than strict.
_IGNORE = frozenset({0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 10.0, 12.0, 24.0, 100.0})

_SYSTEM = """\
You write short operational digests for network managers.

Rules you must follow:
- Use ONLY the figures given to you. Never estimate, extrapolate or round to a different
  number, and never introduce a figure that is not in the input.
- List every figure you used in numbers_used.
- No subscriber identifiers of any kind. Describe groups, never individuals.
- Be plain. A manager is skimming this: say what changed and what needs attention.
- Do not recommend customer contact or any action that would execute automatically.
"""


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    headline: str
    bullets: tuple[str, ...]
    method: str
    trace: AgentTrace
    rejected_numbers: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "bullets": list(self.bullets),
            "method": self.method,
            "rejected_numbers": list(self.rejected_numbers),
            "trace": self.trace.to_dict(),
        }


class Reporter:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gateway: LlmGateway | None = None,
        budget: Budget | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._budget = budget or Budget(max_steps=3, max_tool_calls=6, max_seconds=20.0)

    def write(self, *, facts: dict[str, Any], period: str = "daily") -> ReportOutcome:
        """Write a digest. Never raises: on a stop rule the plain figures are returned."""
        steps: list[str] = []
        calls: list[Any] = []
        try:
            return self._run(facts, period, steps, calls)
        except BudgetExhausted as stop:
            steps.append(f"stopped: {stop}")
            headline, bullets = _plain_digest(facts, period)
            return ReportOutcome(
                headline=headline,
                bullets=bullets,
                method="deterministic",
                trace=self._trace(steps, calls, Budget(), str(stop), degraded=True),
            )

    def _run(
        self,
        facts: dict[str, Any],
        period: str,
        steps: list[str],
        calls: list[Any],
    ) -> ReportOutcome:
        """`facts` is a flat mapping of label to number or short string, assembled by the
        caller from the database. Passing a pre-assembled mapping rather than letting the
        agent query is what makes grounding checkable: the set of legitimate numbers is
        known exactly.
        """
        budget = Budget(
            max_steps=self._budget.max_steps,
            max_tool_calls=self._budget.max_tool_calls,
            max_seconds=self._budget.max_seconds,
        )
        budget.tick_step()
        deterministic = _plain_digest(facts, period)

        if self._gateway is None or not self._gateway.configured:
            steps.append("no model; stating the figures plainly")
            return ReportOutcome(
                headline=deterministic[0],
                bullets=deterministic[1],
                method="deterministic",
                trace=self._trace(steps, calls, budget, "no model configured", degraded=True),
            )

        budget.tick_step()
        steps.append("ask the model")
        result = self._gateway.complete(
            task="report_digest",
            system=_SYSTEM,
            user=_prompt(facts, period),
            prompt_version="reporter-v1",
            fast=True,
        )

        if not isinstance(result.parsed, ReportDigest):
            steps.append("model unusable; stating the figures plainly")
            return ReportOutcome(
                headline=deterministic[0],
                bullets=deterministic[1],
                method="deterministic",
                trace=self._trace(
                    steps, calls, budget, result.note or "model degraded", degraded=True,
                ),
            )

        allowed = _allowed_numbers(facts)
        prose = " ".join([result.parsed.headline, *result.parsed.bullets])
        unaccounted = _ungrounded(prose, allowed)

        if unaccounted:
            # The whole digest goes, not just the offending sentence: a digest with one
            # invented figure has demonstrated that its figures are not trustworthy.
            steps.append(f"rejected: ungrounded figures {unaccounted}")
            _log.warning("report_ungrounded", extra={"numbers": list(unaccounted)})
            return ReportOutcome(
                headline=deterministic[0],
                bullets=deterministic[1],
                method="deterministic",
                rejected_numbers=unaccounted,
                trace=self._trace(
                    steps,
                    calls,
                    budget,
                    "generated prose contained figures that were not in the input",
                    degraded=True,
                ),
            )

        return ReportOutcome(
            headline=result.parsed.headline,
            bullets=tuple(result.parsed.bullets),
            method="llm",
            trace=self._trace(steps, calls, budget, "generated and grounded"),
        )

    def _trace(
        self,
        steps: list[str],
        calls: list[Any],
        budget: Budget,
        stopped: str,
        *,
        degraded: bool = False,
    ) -> AgentTrace:
        return AgentTrace(
            agent=AGENT,
            goal="write an operational digest",
            steps=tuple(steps),
            tool_calls=tuple(calls),
            budget=budget.summary(),
            stopped_because=stopped,
            degraded=degraded,
        )


def _allowed_numbers(facts: dict[str, Any]) -> set[float]:
    """Every number the prose may legitimately contain.

    Derived values are included where they are the obvious thing a writer would compute —
    a percentage of two given figures is grounded, not invented — but nothing else.
    """
    allowed: set[float] = set(_IGNORE)
    numeric: list[float] = []
    for value in facts.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            allowed.add(float(value))
            numeric.append(float(value))
        elif isinstance(value, str):
            allowed.update(
                float(match.group().replace(",", "")) for match in _NUMBER.finditer(value)
            )
        elif isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                    allowed.add(float(inner))
                    numeric.append(float(inner))

    # Percentages between any two given figures, rounded as a person would write them.
    for left in numeric:
        for right in numeric:
            if right:
                share = left / right * 100
                allowed.add(round(share, 1))
                allowed.add(float(round(share)))
    return allowed


def _ungrounded(prose: str, allowed: set[float]) -> tuple[float, ...]:
    """Numbers in the prose that no input figure accounts for."""
    unaccounted: list[float] = []
    for match in _NUMBER.finditer(prose):
        try:
            value = float(match.group().replace(",", ""))
        except ValueError:  # pragma: no cover - the pattern guarantees a number
            continue
        if any(
            abs(value - candidate) <= max(_TOLERANCE * abs(candidate), 0.05)
            for candidate in allowed
        ):
            continue
        unaccounted.append(value)
    return tuple(dict.fromkeys(unaccounted))


def _plain_digest(facts: dict[str, Any], period: str) -> tuple[str, tuple[str, ...]]:
    """The deterministic digest: the figures, stated.

    Not a placeholder. This is what ships whenever the model is down or ungrounded, so it
    has to be genuinely readable rather than a debug dump.
    """
    live = facts.get("live_incidents", 0)
    subs = facts.get("affected_subs", 0)
    headline = (
        f"{live} live incident(s), ~{_fmt(subs)} subscribers affected"
        if live
        else "No live incidents"
    )
    bullets = [
        f"{label.replace('_', ' ')}: {_fmt(value)}"
        for label, value in facts.items()
        if not isinstance(value, dict) and label != "live_incidents"
    ]
    bullets.append(f"Figures are for the {period} window and are stated without interpretation.")
    return headline, tuple(bullets[:8])


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _prompt(facts: dict[str, Any], period: str) -> str:
    """The figures, then the goal.

    Every number the prose is allowed to contain appears here, labelled, which is what
    makes the grounding check downstream decidable rather than a judgement call.
    """
    lines = [f"Figures for the {period} window:", ""]
    for label, value in facts.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{key}={_fmt(item)}" for key, item in value.items())
            lines.append(f"  {label}: {inner}")
        else:
            lines.append(f"  {label}: {_fmt(value)}")
    lines += [
        "",
        "GOAL: write a headline and up to five bullets for a network manager, using only "
        "the figures above. List every figure you used in numbers_used. Do not introduce "
        "any number that is not listed here.",
    ]
    return "\n".join(lines)
