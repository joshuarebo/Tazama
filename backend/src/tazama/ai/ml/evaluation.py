"""Metrics, the baseline comparison, drift and the reproducibility assertion.

Three opinions are baked in here:

* **A model is only ever reported next to its baseline.** `compare_to_baseline` returns an
  explicit `beats_baseline` verdict and the margin, and the registry refuses to activate a
  model whose verdict is false. A metric without a baseline is a number without a claim.
* **MAPE is reported only where it is defined.** The target is a sparse rate: most hours
  are genuinely zero, and dividing by zero would turn the metric into noise. The count of
  rows the MAPE was computed over is returned with it, so nobody mistakes "3.2% MAPE" for
  "3.2% MAPE over everything".
* **Drift is measured, not assumed.** `population_stability_index` compares the training
  feature distribution with a recent one using the PSI the credit-risk world has used for
  decades: < 0.1 stable, 0.1-0.25 moderate, > 0.25 a material shift.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tazama.ai.ml.features import require_ml
from tazama.core.logging import get_logger
from tazama.domain.value_objects import TimeWindow

_log = get_logger(__name__)

#: PSI bands (industry convention; documented in every model card).
PSI_STABLE = 0.10
PSI_MODERATE = 0.25

#: Below this absolute actual value a row is excluded from MAPE: the target is a rate per
#: 1,000 subscribers, so a "true" value of 0 is common and 1/0 is not a percentage error.
MAPE_FLOOR = 1e-9


@dataclass(frozen=True, slots=True)
class RegressionMetrics:
    """Point-forecast quality. `mape` is None when no row qualified."""

    mae: float
    rmse: float
    r2: float
    mape: float | None
    mape_rows: int
    rows: int
    mean_actual: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mae": round(self.mae, 6),
            "rmse": round(self.rmse, 6),
            "r2": round(self.r2, 6),
            "mape": None if self.mape is None else round(self.mape, 4),
            "mape_rows": self.mape_rows,
            "rows": self.rows,
            "mean_actual": round(self.mean_actual, 6),
        }


@dataclass(frozen=True, slots=True)
class DetectionMetrics:
    """Detector quality against labelled windows."""

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    flagged: int
    labelled: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "flagged": self.flagged,
            "labelled": self.labelled,
        }


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    """The verdict PRD section 16 makes the Phase 3 gate."""

    metric: str
    baseline: float
    model: float
    lower_is_better: bool
    beats_baseline: bool
    margin: float
    improvement_pct: float

    def summary(self) -> str:
        verdict = "beats" if self.beats_baseline else "DOES NOT BEAT"
        return (
            f"model {verdict} baseline on {self.metric}: "
            f"{self.model:.6f} vs {self.baseline:.6f} "
            f"({self.improvement_pct:+.1f}%)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": round(self.baseline, 6),
            "model": round(self.model, 6),
            "beats_baseline": self.beats_baseline,
            "margin": round(self.margin, 6),
            "improvement_pct": round(self.improvement_pct, 4),
        }


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Per-feature PSI between a reference (training) and a recent window."""

    per_feature: Mapping[str, float]
    window: TimeWindow | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def worst(self) -> tuple[str, float]:
        if not self.per_feature:
            return ("", 0.0)
        name = max(self.per_feature, key=lambda k: self.per_feature[k])
        return (name, self.per_feature[name])

    @property
    def drifted(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in self.per_feature.items() if v > PSI_MODERATE))

    @property
    def verdict(self) -> str:
        _, value = self.worst
        if value > PSI_MODERATE:
            return "material_shift"
        if value > PSI_STABLE:
            return "moderate_shift"
        return "stable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "worst_feature": self.worst[0],
            "worst_psi": round(self.worst[1], 4),
            "drifted": list(self.drifted),
            "per_feature": {k: round(v, 4) for k, v in self.per_feature.items()},
        }


def regression_metrics(actual: Any, predicted: Any) -> RegressionMetrics:
    """MAE, RMSE, R2 and MAPE-where-defined over the rows where both are present."""
    require_ml()
    import numpy as np

    y = np.asarray(actual, dtype="float64").ravel()
    yhat = np.asarray(predicted, dtype="float64").ravel()
    if y.shape != yhat.shape:
        raise ValueError(f"actual {y.shape} and predicted {yhat.shape} differ in shape")
    usable = ~(np.isnan(y) | np.isnan(yhat))
    y, yhat = y[usable], yhat[usable]
    if y.size == 0:
        raise ValueError("no comparable rows: actual and predicted are entirely missing")

    error = yhat - y
    mae = float(np.mean(np.abs(error)))
    rmse = float(np.sqrt(np.mean(error**2)))
    variance = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum(error**2)) / variance if variance > 0 else 0.0

    defined = np.abs(y) > MAPE_FLOOR
    mape = float(np.mean(np.abs(error[defined] / y[defined])) * 100.0) if defined.any() else None
    return RegressionMetrics(
        mae=mae,
        rmse=rmse,
        r2=r2,
        mape=mape,
        mape_rows=int(defined.sum()),
        rows=int(y.size),
        mean_actual=float(y.mean()),
    )


def detection_metrics(flagged: Sequence[bool], labelled: Sequence[bool]) -> DetectionMetrics:
    """Precision, recall and F1 for a boolean detector against boolean labels."""
    require_ml()
    import numpy as np

    pred = np.asarray(flagged, dtype=bool).ravel()
    truth = np.asarray(labelled, dtype=bool).ravel()
    if pred.shape != truth.shape:
        raise ValueError(f"flagged {pred.shape} and labelled {truth.shape} differ in shape")
    tp = int(np.sum(pred & truth))
    fp = int(np.sum(pred & ~truth))
    fn = int(np.sum(~pred & truth))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return DetectionMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        flagged=int(pred.sum()),
        labelled=int(truth.sum()),
    )


def compare_to_baseline(
    *,
    metric: str,
    baseline: float,
    model: float,
    lower_is_better: bool = True,
    min_improvement_pct: float = 0.0,
) -> BaselineComparison:
    """Explicit verdict and margin. `min_improvement_pct` is the bar, not a nicety.

    A model that ties the baseline does not beat it: the baseline is simpler, cheaper and
    already explainable, so the tie goes to the baseline.
    """
    margin = (baseline - model) if lower_is_better else (model - baseline)
    denominator = abs(baseline) if abs(baseline) > MAPE_FLOOR else MAPE_FLOOR
    improvement_pct = 100.0 * margin / denominator
    return BaselineComparison(
        metric=metric,
        baseline=float(baseline),
        model=float(model),
        lower_is_better=lower_is_better,
        beats_baseline=bool(margin > 0.0 and improvement_pct >= min_improvement_pct),
        margin=float(margin),
        improvement_pct=float(improvement_pct),
    )


def population_stability_index(
    reference: Any,
    recent: Any,
    *,
    bins: int = 10,
    epsilon: float = 1e-6,
) -> float:
    """PSI between two samples of one feature, using reference-quantile bins.

    Quantile bins rather than equal-width ones: a long-tailed KPI would otherwise put
    every observation in one bucket and report perfect stability.
    """
    require_ml()
    import numpy as np

    ref = np.asarray(reference, dtype="float64").ravel()
    cur = np.asarray(recent, dtype="float64").ravel()
    ref = ref[~np.isnan(ref)]
    cur = cur[~np.isnan(cur)]
    if ref.size < bins or cur.size == 0:
        return 0.0
    quantiles = np.unique(np.quantile(ref, np.linspace(0.0, 1.0, bins + 1)))
    if quantiles.size < 3:
        return 0.0
    edges = quantiles.copy()
    edges[0], edges[-1] = -np.inf, np.inf
    ref_share = np.histogram(ref, bins=edges)[0] / ref.size
    cur_share = np.histogram(cur, bins=edges)[0] / cur.size
    ref_share = np.clip(ref_share, epsilon, None)
    cur_share = np.clip(cur_share, epsilon, None)
    return float(np.sum((cur_share - ref_share) * np.log(cur_share / ref_share)))


def drift_report(
    reference_frame: Any,
    recent_frame: Any,
    *,
    feature_columns: Sequence[str],
    bins: int = 10,
) -> DriftReport:
    """PSI for every feature, train vs recent - the drift check PRD section 9 asks for."""
    require_ml()
    per_feature: dict[str, float] = {}
    skipped: list[str] = []
    for name in feature_columns:
        if name not in reference_frame.columns or name not in recent_frame.columns:
            skipped.append(name)
            continue
        per_feature[name] = population_stability_index(
            reference_frame[name].to_numpy(dtype="float64"),
            recent_frame[name].to_numpy(dtype="float64"),
            bins=bins,
        )
    notes = (f"skipped {len(skipped)} columns absent from one frame",) if skipped else ()
    return DriftReport(per_feature=per_feature, notes=notes)


def assert_reproducible(
    train_fn: Callable[[], Mapping[str, Any]],
    *,
    runs: int = 2,
    tolerance: float = 0.0,
) -> tuple[Mapping[str, Any], ...]:
    """Train `runs` times and assert every reported metric is identical.

    This is the Phase 3 gate's "reproducible training" in one function. Tolerance defaults
    to exactly zero: with fixed seeds, a single thread and `deterministic=True`, LightGBM
    is bit-for-bit repeatable, and anything else is a bug worth finding rather than a
    wobble worth tolerating.
    """
    if runs < 2:
        raise ValueError("reproducibility needs at least two runs")
    results = [dict(train_fn()) for _ in range(runs)]
    first = results[0]
    for index, other in enumerate(results[1:], start=2):
        if set(first) != set(other):
            raise AssertionError(
                f"run {index} reported different metrics: {sorted(set(first) ^ set(other))}",
            )
        for key, value in first.items():
            candidate = other[key]
            if isinstance(value, (int, float)) and isinstance(candidate, (int, float)):
                if abs(float(value) - float(candidate)) > tolerance:
                    raise AssertionError(
                        f"metric {key!r} is not reproducible: run 1 = {value!r}, "
                        f"run {index} = {candidate!r}",
                    )
            elif value != candidate:
                raise AssertionError(
                    f"metric {key!r} is not reproducible: run 1 = {value!r}, "
                    f"run {index} = {candidate!r}",
                )
    return tuple(results)
