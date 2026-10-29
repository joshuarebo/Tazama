"""The agent harness: containment first, capability second.

An agent is a loop that calls tools. The dangerous part is not the reasoning — it is the
tools, and specifically what happens when a model asks for one it should not have, or asks
for a legitimate one with arguments it invented. So the harness is built around four
non-negotiables (PRD §11, CLAUDE.md §7):

1. **Permission tiers.** Every tool is declared `read`, `propose` or `never`. `read` runs.
   `propose` produces a proposal a human approves. `never` is registered precisely so that
   a model asking for it is *recorded as having asked*, which is a signal worth having.
   An unregistered tool is `never` by default: unknown means forbidden, not allowed.
2. **Typed arguments.** Each tool carries a Pydantic schema. Arguments are validated before
   the handler is reached, so a handler never defends itself against a hallucinated field.
3. **Budgets.** Steps, tool calls, wall-clock. A loop that cannot terminate is a loop that
   takes the worker with it, so the stop rule is enforced by the harness rather than
   trusted to the model.
4. **An external checker.** The loop stops when a *deterministic* function says the goal is
   met — never when the model says so. A model asked "are you done?" will say yes.

The tool access decision never reads the complaint text. That is the whole structural
defence against prompt injection: no string a customer can write changes which tools exist
or which tier they are in.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from tazama.core.errors import PolicyViolation, ValidationFailed
from tazama.core.logging import get_logger

_log = get_logger(__name__)

Tier = Literal["read", "propose", "never"]

#: Tiers in increasing order of consequence, so a comparison is explicit rather than
#: alphabetical.
_TIER_RANK: dict[Tier, int] = {"read": 0, "propose": 1, "never": 2}


class ToolDenied(PolicyViolation):
    """A tool was requested that this agent may not use."""


class BudgetExhausted(RuntimeError):
    """The loop hit its step, call or time ceiling. Not an error — a stop rule firing."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool: what it is called, what it accepts, what tier it sits in."""

    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[BaseModel], Any]
    tier: Tier = "read"
    timeout_s: float = 10.0

    def openai_schema(self) -> dict[str, Any]:
        """The shape an OpenAI-compatible endpoint expects in `tools`."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A recorded call: what was asked, what was allowed, what came back."""

    name: str
    arguments: dict[str, Any]
    tier: Tier
    allowed: bool
    result: Any = None
    error: str = ""
    duration_ms: float = 0.0
    proposal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "allowed": self.allowed,
            "proposal": self.proposal,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            # Arguments, not results: a result can be large, and the trace is for
            # answering "what did it ask for", not for caching data.
            "arguments": self.arguments,
        }


@dataclass
class Budget:
    """The stop rule, enforced by the harness.

    Wall-clock is in here alongside step and call counts because the failure that actually
    hurts is not "too many steps", it is one step that never returns.
    """

    max_steps: int = 6
    max_tool_calls: int = 12
    max_seconds: float = 30.0
    steps: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def tick_step(self) -> None:
        if self.steps >= self.max_steps:
            raise BudgetExhausted(f"step budget exhausted ({self.steps}/{self.max_steps})")
        self.elapsed_check()
        self.steps += 1

    def tick_tool(self) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExhausted(
                f"tool-call budget exhausted ({self.tool_calls}/{self.max_tool_calls})",
            )
        self.elapsed_check()
        self.tool_calls += 1

    def elapsed_check(self) -> None:
        if self.elapsed > self.max_seconds:
            raise BudgetExhausted(f"time budget exhausted ({self.elapsed:.1f}s)")

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def summary(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "elapsed_s": round(self.elapsed, 2),
            "max_seconds": self.max_seconds,
        }


class ToolRegistry:
    """Everything an agent might call, and the tiers it is allowed to call them in."""

    def __init__(self, tools: Sequence[ToolSpec] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def schemas(self, *, allow: Sequence[str]) -> list[dict[str, Any]]:
        """Only the allowlisted tools are even described to the model.

        A tool the model never hears about is one it cannot ask for by name. That is not
        the security boundary — `invoke` is — but there is no reason to advertise.
        """
        allowed = set(allow)
        return [
            tool.openai_schema()
            for name, tool in sorted(self._tools.items())
            if name in allowed and tool.tier != "never"
        ]

    def tier_of(self, name: str) -> Tier:
        """Unregistered means `never`: unknown is forbidden, not permitted."""
        tool = self._tools.get(name)
        return tool.tier if tool else "never"

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        *,
        allow: Sequence[str],
        max_tier: Tier = "read",
        budget: Budget | None = None,
    ) -> ToolCall:
        """Run one tool call, or record precisely why it was refused.

        Refusals are returned rather than raised. The model has to be *told* that it was
        refused so it can continue sensibly, and the refusal has to be in the trace so a
        reviewer can see what was attempted. Raising would lose both.
        """
        parsed = _arguments(arguments)
        tool = self._tools.get(name)
        tier = self.tier_of(name)

        if tool is None:
            _log.warning("tool_unknown", extra={"tool": name})
            return ToolCall(
                name=name,
                arguments=parsed,
                tier="never",
                allowed=False,
                error=f"no tool named {name!r} exists",
            )
        if name not in set(allow):
            _log.warning("tool_not_granted", extra={"tool": name})
            return ToolCall(
                name=name,
                arguments=parsed,
                tier=tier,
                allowed=False,
                error=f"tool {name!r} is not granted to this agent",
            )
        if _TIER_RANK[tier] > _TIER_RANK[max_tier]:
            _log.warning("tool_tier_denied", extra={"tool": name, "tier": tier})
            return ToolCall(
                name=name,
                arguments=parsed,
                tier=tier,
                allowed=False,
                error=(
                    f"tool {name!r} is tier {tier!r}, above this agent's {max_tier!r} "
                    f"ceiling"
                ),
            )

        try:
            validated = tool.args.model_validate(parsed)
        except ValidationError as error:
            # A hallucinated argument is caught here, before the handler, so no handler
            # has to defend itself against one.
            return ToolCall(
                name=name,
                arguments=parsed,
                tier=tier,
                allowed=False,
                error=f"invalid arguments: {_first_error(error)}",
            )

        if budget is not None:
            budget.tick_tool()

        started = time.perf_counter()
        try:
            result = tool.handler(validated)
        except Exception as error:  # noqa: BLE001 - a tool failing must not kill the loop
            _log.warning("tool_failed", extra={"tool": name, "error": type(error).__name__})
            return ToolCall(
                name=name,
                arguments=parsed,
                tier=tier,
                allowed=True,
                error=f"{type(error).__name__}: {error}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        return ToolCall(
            name=name,
            arguments=parsed,
            tier=tier,
            allowed=True,
            result=result,
            duration_ms=(time.perf_counter() - started) * 1000,
            # A `propose` tool never acts. Its output is a proposal, and the flag is what
            # the UI and the audit row key off to say "awaiting a human".
            proposal=tier == "propose",
        )


@dataclass(frozen=True, slots=True)
class AgentTrace:
    """What one agent run did, in enough detail to argue about months later."""

    agent: str
    goal: str
    steps: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    budget: dict[str, Any]
    stopped_because: str
    degraded: bool = False
    confidence: float | None = None
    escalated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "goal": self.goal,
            "steps": list(self.steps),
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "budget": self.budget,
            "stopped_because": self.stopped_because,
            "degraded": self.degraded,
            "confidence": self.confidence,
            "escalated": self.escalated,
        }

    @property
    def called(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.tool_calls if call.allowed)

    @property
    def refused(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.tool_calls if not call.allowed)


def _arguments(raw: dict[str, Any] | str) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string from the wire. Non-objects are refused."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
    return f"{location}: {first.get('msg', 'invalid')}"


def require_confidence(
    value: float | None,
    *,
    floor: float,
    what: str,
) -> None:
    """Fail closed below the floor.

    Used by the cheap-decision gate: when the fast model is not sure, the answer is
    escalation, not a guess. `None` counts as unsure, because "no confidence reported" is
    not evidence of confidence.
    """
    if value is None or value < floor:
        raise ValidationFailed(
            f"{what} confidence {value if value is not None else 'unknown'} is below the "
            f"{floor:.2f} floor; escalating to a human",
        )
