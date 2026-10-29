"""Call-surge forecast: LightGBM regression on the next hour's complaint rate per cell.

What this model is for: one hour of warning before a complaint surge reaches the care
centre, per cell, so an incident can be opened (and a notification proposed) before the
calls arrive.

The engineering decisions worth defending
-----------------------------------------
* **Time-based validation only.** Expanding-window folds over *unique hours*
  (`sklearn.model_selection.TimeSeriesSplit` over the sorted distinct timestamps, then
  mapped back to rows). Splitting rows directly would put the same hour from different
  cells on both sides of a fold boundary, which is a panel-data leak that looks like a
  great score. Never `KFold`, never `shuffle=True`.
* **Deterministic by configuration, not by luck.** Fixed `random_state`,
  `force_row_wise=True`, `deterministic=True` and a single thread: LightGBM's histogram
  construction is otherwise thread-order dependent and two runs disagree in the sixth
  decimal place. `assert_reproducible` holds us to exactly zero tolerance.
* **SHAP, not variance, ranks the drivers.** PRD section 9 is explicit: "Variance is not
  relevance: use supervised importance (LightGBM + SHAP) to rank drivers". So the driver
  list comes from mean absolute SHAP value with a sign taken from the value/attribution
  correlation, and PCA stays where it belongs - in exploration.
* **The baseline is evaluated on exactly the same rows.** Any other comparison is an
  accident waiting to be quoted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tazama.ai.ml.evaluation import (
    BaselineComparison,
    RegressionMetrics,
    compare_to_baseline,
    regression_metrics,
)
from tazama.ai.ml.features import (
    TARGET_COLUMN,
    FeatureFrame,
    TimeSplit,
    assert_no_leakage,
    require_ml,
)
from tazama.ai.ml.models.baseline import SeasonalNaiveForecast
from tazama.core.logging import get_logger

_log = get_logger(__name__)

NAME = "call-surge-forecast"
ALGORITHM = "lightgbm"
VERSION = "lgbm-v1"

#: Packages this model needs beyond numpy/pandas.
ML_PACKAGES = ("lightgbm", "sklearn", "shap")

#: One seed for everything stochastic in this module.
RANDOM_STATE = 20260301

#: Deliberately modest: ~29k training rows on the seed, a sparse target, and a phase gate
#: that cares about honesty rather than the last 2% of MAE. Tuning is tech debt, recorded.
DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "regression_l1",  # MAE: the metric we report and a sparse, spiky target
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 40,
    "subsample": 0.9,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": RANDOM_STATE,
    "n_jobs": 1,  # single-threaded: reproducibility beats wall-clock here
    "force_row_wise": True,
    "deterministic": True,
    "verbose": -1,
}

#: Rows sampled for SHAP. TreeExplainer is exact but quadratic-ish in rows; a few thousand
#: rows rank drivers identically and keep the whole training run inside its time budget.
SHAP_SAMPLE_ROWS = 2_000

DEFAULT_FOLDS = 4


@dataclass(frozen=True, slots=True)
class DriverImportance:
    """One feature's supervised importance, SHAP-derived."""

    feature: str
    mean_abs_shap: float
    direction: str  # "increases" | "decreases" | "mixed"
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "mean_abs_shap": round(self.mean_abs_shap, 8),
            "direction": self.direction,
            "rank": self.rank,
        }


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One expanding-window fold."""

    fold: int
    train_rows: int
    validation_rows: int
    mae: float
    rmse: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "train_rows": self.train_rows,
            "validation_rows": self.validation_rows,
            "mae": round(self.mae, 6),
            "rmse": round(self.rmse, 6),
        }


@dataclass(frozen=True)
class ForecastOutcome:
    """Everything one training run produced, ready for the registry and the model card."""

    model: Any
    baseline: Any
    validation_model: RegressionMetrics
    validation_baseline: RegressionMetrics
    test_model: RegressionMetrics
    test_baseline: RegressionMetrics
    comparison: BaselineComparison
    folds: tuple[FoldResult, ...]
    drivers: tuple[DriverImportance, ...]
    feature_columns: tuple[str, ...]
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def beats_baseline(self) -> bool:
        return self.comparison.beats_baseline

    def metrics(self) -> dict[str, Any]:
        return {
            "validation": self.validation_model.to_dict(),
            "validation_baseline": self.validation_baseline.to_dict(),
            "test": self.test_model.to_dict(),
            "test_baseline": self.test_baseline.to_dict(),
            "comparison": self.comparison.to_dict(),
            "cv_folds": [f.to_dict() for f in self.folds],
            "drivers": [d.to_dict() for d in self.drivers],
        }

    def flat_metrics(self) -> dict[str, float]:
        """The scalar metrics a reproducibility check compares run to run."""
        return {
            "validation_mae": self.validation_model.mae,
            "validation_rmse": self.validation_model.rmse,
            "validation_r2": self.validation_model.r2,
            "test_mae": self.test_model.mae,
            "test_rmse": self.test_model.rmse,
            "baseline_test_mae": self.test_baseline.mae,
            "baseline_validation_mae": self.validation_baseline.mae,
            "cv_mae": sum(f.mae for f in self.folds) / len(self.folds) if self.folds else 0.0,
        }


class CallSurgeForecast:
    """LightGBM regressor over the lagged/calendar features, wrapped thinly.

    Thin on purpose: the value is in the feature contract and the validation protocol, not
    in the estimator, and a thin wrapper is what makes the artifact easy to replace with a
    better model later.
    """

    name = NAME
    algorithm = ALGORITHM
    version = VERSION

    def __init__(
        self,
        *,
        params: Mapping[str, Any] | None = None,
        target: str = TARGET_COLUMN,
        n_threads: int = 1,
    ) -> None:
        require_ml("numpy", "pandas", "lightgbm")
        self.target = target
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        merged["n_jobs"] = n_threads
        self._params = merged
        self._model: Any = None
        self._feature_columns: tuple[str, ...] = ()

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return self._feature_columns

    def params(self) -> dict[str, Any]:
        return dict(self._params)

    @property
    def booster(self) -> Any:
        if self._model is None:
            raise RuntimeError("CallSurgeForecast.fit must be called before use")
        return self._model

    def fit(
        self,
        frame: Any,
        *,
        feature_columns: Sequence[str],
        rows: Any = None,
    ) -> CallSurgeForecast:
        require_ml("numpy", "pandas", "lightgbm")
        import lightgbm as lgb

        subset = frame if rows is None else frame.loc[rows]
        usable = subset[self.target].notna()
        x = subset.loc[usable, list(feature_columns)]
        y = subset.loc[usable, self.target]
        if len(x) == 0:
            raise ValueError("no training rows with a defined target")
        model = lgb.LGBMRegressor(**self._params)
        model.fit(x, y)
        self._model = model
        self._feature_columns = tuple(feature_columns)
        _log.info("forecast fitted", extra={"rows": len(x), "features": len(x.columns)})
        return self

    def predict(self, frame: Any, rows: Any = None) -> Any:
        subset = frame if rows is None else frame.loc[rows]
        return self.booster.predict(subset[list(self._feature_columns)])

    def cross_validate(
        self,
        frame: Any,
        *,
        feature_columns: Sequence[str],
        rows: Any = None,
        folds: int = DEFAULT_FOLDS,
    ) -> tuple[FoldResult, ...]:
        """Expanding-window folds over unique hours. The only validation we trust."""
        require_ml("numpy", "pandas", "lightgbm", "sklearn")
        import lightgbm as lgb
        import numpy as np
        from sklearn.model_selection import TimeSeriesSplit

        subset = frame if rows is None else frame.loc[rows]
        hours = np.sort(subset["ts"].unique())
        if len(hours) < folds + 1:
            raise ValueError(f"{len(hours)} distinct hours is too few for {folds} folds")
        splitter = TimeSeriesSplit(n_splits=folds)
        results: list[FoldResult] = []
        for index, (train_hours, validation_hours) in enumerate(splitter.split(hours), start=1):
            train_mask = subset["ts"].isin(hours[train_hours])
            validation_mask = subset["ts"].isin(hours[validation_hours])
            train = subset.loc[train_mask & subset[self.target].notna()]
            validation = subset.loc[validation_mask & subset[self.target].notna()]
            if len(train) == 0 or len(validation) == 0:
                continue
            model = lgb.LGBMRegressor(**self._params)
            model.fit(train[list(feature_columns)], train[self.target])
            predicted = model.predict(validation[list(feature_columns)])
            metrics = regression_metrics(validation[self.target].to_numpy(), predicted)
            results.append(
                FoldResult(
                    fold=index,
                    train_rows=len(train),
                    validation_rows=len(validation),
                    mae=metrics.mae,
                    rmse=metrics.rmse,
                ),
            )
        return tuple(results)

    def drivers(
        self,
        frame: Any,
        rows: Any = None,
        *,
        top: int = 15,
        sample_rows: int = SHAP_SAMPLE_ROWS,
    ) -> tuple[DriverImportance, ...]:
        """Rank drivers by mean absolute SHAP value, with a direction for each."""
        require_ml("numpy", "pandas", "lightgbm", "shap")
        import numpy as np
        import shap

        subset = frame if rows is None else frame.loc[rows]
        x = subset[list(self._feature_columns)]
        if len(x) > sample_rows:
            # Evenly spaced rather than random: no RNG, so the ranking is reproducible.
            x = x.iloc[:: max(1, len(x) // sample_rows)]
        explainer = shap.TreeExplainer(self.booster)
        values = np.asarray(explainer.shap_values(x))
        if values.ndim == 3:  # some versions return (rows, features, outputs)
            values = values[:, :, 0]
        mean_abs = np.abs(values).mean(axis=0)
        order = np.argsort(-mean_abs)
        ranked: list[DriverImportance] = []
        for rank, position in enumerate(order[:top], start=1):
            feature = self._feature_columns[int(position)]
            column = x[feature].to_numpy(dtype="float64")
            attribution = values[:, int(position)]
            ranked.append(
                DriverImportance(
                    feature=feature,
                    mean_abs_shap=float(mean_abs[int(position)]),
                    direction=_direction(column, attribution),
                    rank=rank,
                ),
            )
        return tuple(ranked)


def _direction(values: Any, attribution: Any) -> str:
    """ "increases"/"decreases" when the value and its attribution move together."""
    import numpy as np

    usable = ~(np.isnan(values) | np.isnan(attribution))
    if usable.sum() < 10 or np.std(values[usable]) == 0 or np.std(attribution[usable]) == 0:
        return "mixed"
    rho = float(np.corrcoef(values[usable], attribution[usable])[0, 1])
    if rho > 0.3:
        return "increases"
    if rho < -0.3:
        return "decreases"
    return "mixed"


def train_call_surge_forecast(
    features: FeatureFrame,
    split: TimeSplit,
    *,
    params: Mapping[str, Any] | None = None,
    folds: int = DEFAULT_FOLDS,
    with_shap: bool = True,
    min_improvement_pct: float = 0.0,
    n_threads: int = 1,
) -> ForecastOutcome:
    """Train the baseline and the model on the same split and return both verdicts.

    The leakage gate runs *here*, before a single tree is grown, because a model trained on
    a leaky frame is worse than no model: it is a confident wrong answer.
    """
    require_ml("numpy", "pandas", "lightgbm", "sklearn")
    frame = features.frame
    assert_no_leakage(
        frame,
        target=features.target,
        feature_columns=features.feature_columns,
        split=split,
    )

    train_mask = split.mask(frame, "train")
    validation_mask = split.mask(frame, "validation")
    test_mask = split.mask(frame, "test")

    baseline = SeasonalNaiveForecast(target=features.target).fit(frame, train_mask)
    model = CallSurgeForecast(params=params, target=features.target, n_threads=n_threads)
    model.fit(frame, feature_columns=features.feature_columns, rows=train_mask)

    def score(mask: Any) -> tuple[RegressionMetrics, RegressionMetrics]:
        actual = frame.loc[mask, features.target].to_numpy(dtype="float64")
        return (
            regression_metrics(actual, model.predict(frame, mask)),
            regression_metrics(actual, baseline.predict(frame, mask)),
        )

    validation_model, validation_baseline = score(validation_mask)
    test_model, test_baseline = score(test_mask)
    comparison = compare_to_baseline(
        metric="test_mae",
        baseline=test_baseline.mae,
        model=test_model.mae,
        lower_is_better=True,
        min_improvement_pct=min_improvement_pct,
    )
    folds_result = model.cross_validate(
        frame,
        feature_columns=features.feature_columns,
        rows=train_mask,
        folds=folds,
    )
    drivers = model.drivers(frame, validation_mask) if with_shap else ()
    _log.info("forecast trained", extra={"comparison": comparison.summary()})
    return ForecastOutcome(
        model=model,
        baseline=baseline,
        validation_model=validation_model,
        validation_baseline=validation_baseline,
        test_model=test_model,
        test_baseline=test_baseline,
        comparison=comparison,
        folds=folds_result,
        drivers=drivers,
        feature_columns=tuple(features.feature_columns),
        params=model.params(),
    )
