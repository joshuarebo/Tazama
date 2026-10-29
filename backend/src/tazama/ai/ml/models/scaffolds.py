"""Deep-learning and reinforcement-learning sketches. Disabled, and meant to stay that way.

PRD section 9 allows exactly two of these and fences both:

* a **spatio-temporal GNN** over the topology graph, "enabled only if it beats LightGBM on
  the eval set";
* a **contextual bandit** for notification timing, evaluated with offline policy
  evaluation - "no RL on the live network, ever".

So these are interface sketches: no torch, no training loop, no weights. Constructing one
while its flag is false raises, and the error says what has to be true first. The point is
to make the next engineer's decision explicit rather than to save them an afternoon.

What each would need before `AI_GNN_ENABLED` / `AI_BANDIT_ENABLED` could be flipped is
written in `REQUIREMENTS` on each class and repeated in the error message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tazama.core.errors import PolicyViolation
from tazama.core.settings import Settings, get_settings

#: Appended to every refusal: the rule that governs both scaffolds.
GATE = (
    "not enabled; must beat LightGBM on the eval set first "
    "(PRD section 9: deep learning only where it beats baselines)"
)


def _refuse(what: str, flag: str, requirements: Sequence[str]) -> PolicyViolation:
    steps = "; ".join(f"{i}. {r}" for i, r in enumerate(requirements, start=1))
    return PolicyViolation(
        f"{what} is {GATE}. Set {flag}=true only after: {steps}",
        flag=flag,
        scaffold=what,
    )


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """The input a spatio-temporal GNN would take: a topology graph plus node series.

    `edges` are directed `(parent, child)` pairs from `topology_edge`; `node_series` maps a
    node id to its KPI/demand history, already time-aligned by the feature builder.
    """

    node_ids: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    node_series: Mapping[str, Sequence[float]]
    horizon_hours: int = 1


class SpatioTemporalGnnScaffold:
    """Sketch of a GNN that would forecast per-cell demand using topology structure.

    The hypothesis worth testing: complaints propagate along the transmission tree, so a
    cell's neighbours under a shared aggregation node carry signal that per-cell lags
    cannot. LightGBM only sees that if someone hand-builds neighbour features; a GNN would
    learn it. That is a real hypothesis - and it is also exactly the kind of hypothesis
    that loses to a well-fed gradient-boosted tree, which is why the gate exists.
    """

    name = "spatio-temporal-gnn"
    algorithm = "gnn"
    version = "scaffold-v0"
    flag = "AI_GNN_ENABLED"

    #: Offline evidence required before this may be enabled.
    REQUIREMENTS: tuple[str, ...] = (
        "hand-built neighbour features are added to LightGBM first, and the GNN beats "
        "that stronger baseline, not the plain one",
        "the comparison runs on the time-ordered test split with the same leakage gate",
        "test MAE improves by a margin larger than the run-to-run spread of both models",
        "a torch dependency is justified in an ADR, with inference cost and a CPU budget",
        "a model card documents the graph construction and its failure modes",
    )

    def __init__(self, *, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        if not getattr(resolved, self.flag, False):
            raise _refuse(self.name, self.flag, self.REQUIREMENTS)
        raise NotImplementedError(  # pragma: no cover - unreachable while the flag is false
            "the GNN is a sketch: no implementation exists yet, only the evaluation "
            "contract in REQUIREMENTS",
        )

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Readable, flag-independent description for the AI Quality page and docs."""
        return {
            "name": cls.name,
            "status": "disabled",
            "flag": cls.flag,
            "inputs": "topology graph (topology_edge) + per-node KPI/demand history",
            "requirements": list(cls.REQUIREMENTS),
        }


@dataclass(frozen=True, slots=True)
class BanditContext:
    """One decision context for notification timing - never a customer identifier."""

    incident_id: str
    hour_of_day_local: int
    is_quiet_hours: bool
    affected_subscribers: int
    predicted_surge: float


class NotificationTimingBanditScaffold:
    """Sketch of a contextual bandit choosing *when* to propose a customer notification.

    Two hard constraints, both from the PRD, both non-negotiable:

    * **No RL on the live network, ever.** The policy may only ever be evaluated offline,
      with inverse-propensity or doubly-robust estimation over logged decisions, and the
      logging policy must have been stochastic for those estimates to mean anything.
    * **No side effects.** A bandit would rank candidate send times; a human still
      approves the notification, and quiet hours, opt-out and rate limits are enforced by
      the policy engine regardless of what it recommends.
    """

    name = "notification-timing-bandit"
    algorithm = "contextual_bandit"
    version = "scaffold-v0"
    flag = "AI_BANDIT_ENABLED"

    REQUIREMENTS: tuple[str, ...] = (
        "logged decisions exist with recorded propensities - without them, offline policy "
        "evaluation is guesswork",
        "an IPS/doubly-robust estimate beats the current fixed-schedule policy with a "
        "confidence interval that excludes no improvement",
        "a simulated-exploration safety review covers notification storms and opt-out",
        "the bandit proposes only; quiet hours, rate limits and human approval still bind",
        "no online learning path exists in the code that reaches the live network",
    )

    def __init__(self, *, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        if not getattr(resolved, self.flag, False):
            raise _refuse(self.name, self.flag, self.REQUIREMENTS)
        raise NotImplementedError(  # pragma: no cover - unreachable while the flag is false
            "the bandit is a sketch: only offline policy evaluation is permitted, and no "
            "logged propensities exist yet",
        )

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "status": "disabled",
            "flag": cls.flag,
            "inputs": "incident context + predicted surge; no subscriber identifiers",
            "evaluation": "offline policy evaluation only (IPS / doubly robust)",
            "requirements": list(cls.REQUIREMENTS),
        }


def scaffold_status(settings: Settings | None = None) -> tuple[dict[str, Any], ...]:
    """Both scaffolds and whether their flags are on - safe to call at any time."""
    resolved = settings or get_settings()
    status = []
    for scaffold in (SpatioTemporalGnnScaffold, NotificationTimingBanditScaffold):
        described = scaffold.describe()
        described["enabled"] = bool(getattr(resolved, scaffold.flag, False))
        status.append(described)
    return tuple(status)
