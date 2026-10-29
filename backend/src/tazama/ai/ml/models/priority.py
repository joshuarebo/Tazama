"""Incident priority scoring - a scaffold with a real estimator behind it.

Read this before using it
-------------------------
**There is almost no genuine training signal for this model on synthetic data.** The seed
has no human priority decisions, no "was this actually urgent?" outcome, and no record of
an engineer disagreeing with the rule. So the only labels available are the ones the
deterministic rule in `modules/prioritisation` already produces, which means a model
trained here *distils an existing rule* and cannot be better than it - only faster, less
explainable and more brittle. Any accuracy number you see from it is a measure of how well
it copied the rule, not of how well it prioritises incidents.

It exists anyway, for two reasons, and both are honest:
  1. the integration surface is needed now, so that when real priority outcomes (SLA
     breaches, escalations, reopened cases) start accumulating, the model can be trained on
     them without touching `modules/prioritisation`;
  2. `score()` satisfies the small protocol prioritisation will blend against, so the
     wiring can be tested end to end while the model stays a clearly-labelled scaffold.

Until real outcome labels exist, the untrained instance returns a documented, transparent
linear blend of the normalised impact factors and a version that says so
(`priority-untrained-v0`). The rule stays the decision-maker: AI recommends, rules decide,
humans approve (CLAUDE.md section 6.9).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tazama.ai.ml.features import require_ml
from tazama.core.logging import get_logger

_log = get_logger(__name__)

NAME = "incident-priority-score"
ALGORITHM = "gradient_boosting"
TRAINED_VERSION = "priority-gbr-v1"
UNTRAINED_VERSION = "priority-untrained-v0"

ML_PACKAGES = ("numpy", "sklearn")

RANDOM_STATE = 20260301

#: The impact features `modules/prioritisation` already computes. Order is part of the
#: contract: the model is fitted on this order and scores on it.
FEATURE_ORDER: tuple[str, ...] = (
    "affected_subscribers",
    "enterprise_accounts",
    "duration_minutes",
    "sla_risk",
    "complaint_count",
)

#: Saturation points used to normalise each feature into [0, 1]. Copied deliberately from
#: `modules/prioritisation` rather than imported: `ai/ml` must not import a module's
#: internals, and a silent change there should show up as a test failure here.
SATURATION: Mapping[str, float] = {
    "affected_subscribers": 50_000.0,
    "enterprise_accounts": 25.0,
    "duration_minutes": 240.0,
    "sla_risk": 1.0,
    "complaint_count": 200.0,
}

#: The transparent fallback blend. Weights sum to 1 and mirror the rule's emphasis: scale
#: of customer impact first, then enterprise/SLA exposure, then duration and volume.
FALLBACK_WEIGHTS: Mapping[str, float] = {
    "affected_subscribers": 0.35,
    "enterprise_accounts": 0.20,
    "duration_minutes": 0.15,
    "sla_risk": 0.20,
    "complaint_count": 0.10,
}


@dataclass(frozen=True, slots=True)
class PriorityTrainingRow:
    """One training example: impact features plus an observed priority in [0, 1]."""

    features: Mapping[str, float]
    label: float


def normalise(features: Mapping[str, float]) -> list[float]:
    """Clamp each impact feature into [0, 1] against its saturation point."""
    values: list[float] = []
    for name in FEATURE_ORDER:
        raw = float(features.get(name, 0.0) or 0.0)
        ceiling = SATURATION[name]
        values.append(min(max(raw / ceiling, 0.0), 1.0) if ceiling > 0 else 0.0)
    return values


class IncidentPriorityModel:
    """Gradient-boosted regressor over the impact features; honest when untrained.

    Satisfies the protocol prioritisation will blend against:

        def score(self, features: dict[str, float]) -> tuple[float, str]: ...

    returning `(score in [0, 1], model version)`. The version is what lets an audit record
    say which thing produced the number - including when that thing was the fallback
    blend.
    """

    name = NAME
    algorithm = ALGORITHM

    def __init__(
        self,
        *,
        random_state: int = RANDOM_STATE,
        n_estimators: int = 200,
        max_depth: int = 3,
        learning_rate: float = 0.05,
    ) -> None:
        self.random_state = random_state
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self._model: Any = None
        self._rows = 0

    @property
    def trained(self) -> bool:
        return self._model is not None

    @property
    def version(self) -> str:
        return TRAINED_VERSION if self.trained else UNTRAINED_VERSION

    def params(self) -> dict[str, Any]:
        return {
            "random_state": self.random_state,
            "n_estimators": self.n_estimators,
            "max_depth": self.max_depth,
            "learning_rate": self.learning_rate,
            "features": list(FEATURE_ORDER),
            "training_rows": self._rows,
            "trained": self.trained,
            "caveat": "synthetic labels distil the existing rule; no real outcome signal",
        }

    def fit(self, rows: Sequence[PriorityTrainingRow]) -> IncidentPriorityModel:
        """Fit on caller-supplied labels. See the module docstring on what they mean."""
        require_ml("numpy", "sklearn")
        import numpy as np
        from sklearn.ensemble import GradientBoostingRegressor

        if len(rows) < 20:
            raise ValueError(
                f"refusing to fit a priority model on {len(rows)} rows; the fallback blend "
                f"is more honest than an overfitted tree",
            )
        x = np.asarray([normalise(r.features) for r in rows], dtype="float64")
        y = np.clip(np.asarray([r.label for r in rows], dtype="float64"), 0.0, 1.0)
        model = GradientBoostingRegressor(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            random_state=self.random_state,
        )
        model.fit(x, y)
        self._model = model
        self._rows = len(rows)
        _log.info("priority model fitted", extra={"rows": len(rows)})
        return self

    def score(self, features: dict[str, float]) -> tuple[float, str]:
        """`(score, version)` - the protocol `modules/prioritisation` will blend against."""
        normalised = normalise(features)
        if self._model is None:
            blend = sum(
                FALLBACK_WEIGHTS[name] * value
                for name, value in zip(FEATURE_ORDER, normalised, strict=True)
            )
            return (round(min(max(blend, 0.0), 1.0), 6), UNTRAINED_VERSION)
        import numpy as np

        predicted = float(self._model.predict(np.asarray([normalised], dtype="float64"))[0])
        return (round(min(max(predicted, 0.0), 1.0), 6), TRAINED_VERSION)

    def score_many(self, rows: Sequence[Mapping[str, float]]) -> tuple[tuple[float, str], ...]:
        return tuple(self.score(dict(row)) for row in rows)
