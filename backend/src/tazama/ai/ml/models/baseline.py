"""The seasonal-naive baseline: the thing every model has to beat.

PRD section 9 orders the work "classical first, deep learning only where it beats
baselines", and CLAUDE.md section 7 says "baseline before model". This is that baseline,
and it is not a straw man: the seed carries real daily and weekly seasonality, so "the
same hour last week" is a genuinely strong predictor of complaint volume. If LightGBM
cannot beat it, the honest conclusion is that LightGBM is not worth deploying - not that
the baseline needs weakening.

The rule, in order:
  1. the value observed at `t - 168h` (same hour, same weekday, last week);
  2. failing that, `t - 24h` (same hour yesterday);
  3. failing that, the mean of the **training** window.

Step 3 is why this model has a `fit`: the fallback mean is learned from training rows
only, so a prediction for a validation hour never contains a validation value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tazama.ai.ml.features import TARGET_COLUMN, require_ml

#: One week of hourly buckets. Same hour, same weekday.
SEASON_HOURS = 168

#: One day of hourly buckets, used when a week of history is missing.
FALLBACK_HOURS = 24

VERSION = "seasonal-naive-168h-v1"
NAME = "call-surge-baseline"


@dataclass(frozen=True, slots=True)
class BaselineFit:
    """What the baseline learned: a single number, and where it came from."""

    fallback_mean: float
    train_rows: int
    season_hours: int
    fallback_hours: int


class SeasonalNaiveForecast:
    """Seasonal-naive one-hour-ahead forecast of the complaint rate per cell.

    Stateless apart from the fallback mean, deterministic by construction, and cheap
    enough to re-run on every batch - which is exactly what a baseline should be.
    """

    name = NAME
    algorithm = "seasonal_naive"
    version = VERSION

    def __init__(
        self,
        *,
        target: str = TARGET_COLUMN,
        season_hours: int = SEASON_HOURS,
        fallback_hours: int = FALLBACK_HOURS,
    ) -> None:
        if season_hours < 1 or fallback_hours < 1:
            raise ValueError("seasonal lags must be at least one hour")
        self.target = target
        self.season_hours = season_hours
        self.fallback_hours = fallback_hours
        self._fit: BaselineFit | None = None

    @property
    def fitted(self) -> BaselineFit:
        if self._fit is None:
            raise RuntimeError("SeasonalNaiveForecast.fit must be called before predict")
        return self._fit

    def params(self) -> dict[str, Any]:
        return {
            "season_hours": self.season_hours,
            "fallback_hours": self.fallback_hours,
            "target": self.target,
        }

    def fit(self, frame: Any, rows: Any = None) -> SeasonalNaiveForecast:
        """Learn the fallback mean from the given (training) rows only."""
        require_ml()
        subset = frame if rows is None else frame.loc[rows]
        values = subset[self.target].astype("float64")
        mean = float(values.mean()) if int(values.notna().sum()) else 0.0
        self._fit = BaselineFit(
            # An all-NaN training slice would otherwise poison every fallback prediction.
            fallback_mean=0.0 if math.isnan(mean) else mean,
            train_rows=len(subset),
            season_hours=self.season_hours,
            fallback_hours=self.fallback_hours,
        )
        return self

    def predict(self, frame: Any, rows: Any = None) -> Any:
        """One prediction per row, as a numpy array aligned with the selection.

        The lag columns are used when the feature builder already produced them, and
        derived by shifting the target otherwise. Both are strictly historical: a shift of
        +168 hours can only look backwards.
        """
        require_ml()
        import numpy as np

        fitted = self.fitted
        full = self._lag_series(frame)
        season, fallback = full
        if rows is not None:
            mask = rows
            season = season.loc[mask] if hasattr(mask, "loc") else season[mask]
            fallback = fallback.loc[mask] if hasattr(mask, "loc") else fallback[mask]
        values = season.to_numpy(dtype="float64").copy()
        day = fallback.to_numpy(dtype="float64")
        gap = np.isnan(values)
        values[gap] = day[gap]
        values[np.isnan(values)] = fitted.fallback_mean
        return values

    def _lag_series(self, frame: Any) -> tuple[Any, Any]:
        source = self._source_column(frame)
        season_name = f"{source}_lag{self.season_hours}"
        fallback_name = f"{source}_lag{self.fallback_hours}"
        if season_name in frame.columns and fallback_name in frame.columns:
            return frame[season_name], frame[fallback_name]
        grouped = frame.groupby("cell_id", sort=False, observed=True)[source]
        return grouped.shift(self.season_hours), grouped.shift(self.fallback_hours)

    def _source_column(self, frame: Any) -> str:
        """Which observed column the seasonal lag is taken from.

        The target is a copy of an observed demand column, so its lags are the natural
        source; when the frame carries the observed column too, prefer it, because that is
        the column the lag features were built from.
        """
        if "complaints_per_1k" in frame.columns:
            return "complaints_per_1k"
        return self.target


def seasonal_naive_predictions(
    frame: Any,
    *,
    target: str = TARGET_COLUMN,
    train_rows: Any = None,
    predict_rows: Any = None,
    season_hours: int = SEASON_HOURS,
) -> Any:
    """Convenience wrapper: fit on `train_rows`, predict `predict_rows`."""
    model = SeasonalNaiveForecast(target=target, season_hours=season_hours)
    model.fit(frame, train_rows)
    return model.predict(frame, predict_rows)


def baseline_feature_columns(target: str = TARGET_COLUMN) -> Sequence[str]:
    """The only columns the baseline reads - recorded in the registry entry."""
    return (
        f"complaints_per_1k_lag{SEASON_HOURS}",
        f"complaints_per_1k_lag{FALLBACK_HOURS}",
        target,
    )
