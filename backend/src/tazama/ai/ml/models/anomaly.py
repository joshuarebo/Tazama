"""KPI anomaly detection: two unsupervised detectors that fail in different ways.

Why two
-------
* **Robust z-score (median/MAD), per cell, per hour of week.** A cell's "normal" is not a
  single number: a busy urban 4G cell at 20:00 on a Friday looks nothing like the same cell
  at 04:00 on a Sunday, so the reference distribution is conditioned on the hour of the
  week. Median and MAD rather than mean and standard deviation because KPI distributions
  are skewed and an outage *is* an outlier - a mean-based z-score is dragged towards the
  very event it is supposed to flag. This detector is per-KPI, so it says *which* family
  moved, which is what an engineer actually needs.
* **Isolation Forest over the whole KPI vector.** The z-score misses the failure mode where
  every KPI is individually plausible but the *combination* is not (throughput normal while
  PRB utilisation is near zero, say). Isolation Forest is cheap, needs no labels, and
  catches exactly that. It cannot say why, so its contributors come from the per-KPI
  z-scores.

Evaluation
----------
Neither detector is trained on labels, and neither reads `ground_truth`. The seeded
scenario windows are **passed in by the caller** (the eval suite), which is what keeps the
answer out of production code - see the `ground_truth` note in
`infrastructure/db/models.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tazama.ai.ml.features import KPI_COLUMNS, require_ml
from tazama.core.logging import get_logger
from tazama.domain.value_objects import TimeWindow

_log = get_logger(__name__)

NAME = "kpi-anomaly-detection"
VERSION = "robust-z-iforest-v1"

#: 1 / Phi^-1(0.75): scales the MAD so that, for normally distributed data, it estimates
#: the standard deviation. Keeps the threshold interpretable in "sigma" terms without
#: assuming normality for the tails.
MAD_TO_SIGMA = 1.4826

#: Default flag threshold. 3.5 robust sigma is the Iglewicz-Hoaglin convention for the
#: modified z-score and keeps the false-positive rate sane across 600 cells x 720 hours.
DEFAULT_Z_THRESHOLD = 3.5

#: A reference bin needs this many samples before its MAD is trusted; below it the
#: detector falls back to a coarser bin (hour of day, then the whole cell).
MIN_BIN_SAMPLES = 8

#: Expected share of anomalies for Isolation Forest. The seed's labelled scenarios cover
#: roughly 1% of cell-hours, so 1% is the honest prior rather than sklearn's "auto".
DEFAULT_CONTAMINATION = 0.01

RANDOM_STATE = 20260301

ML_PACKAGES = ("numpy", "pandas", "sklearn")


@dataclass(frozen=True, slots=True)
class AnomalyScore:
    """One (cell, hour) verdict, with the KPI families that drove it."""

    cell_id: str
    ts: datetime
    score: float
    is_anomaly: bool
    threshold: float
    detector: str
    contributors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "ts": self.ts.isoformat(),
            "score": round(self.score, 4),
            "is_anomaly": self.is_anomaly,
            "threshold": self.threshold,
            "detector": self.detector,
            "contributors": list(self.contributors),
        }


def _median_absolute_deviation(series: Any) -> float:
    """MAD of one KPI inside one reference bin. Zero when the bin is constant."""
    return float((series - series.median()).abs().median())


def _bin_keys(frame: Any) -> tuple[Any, Any]:
    """`(hour_of_week, hour_of_day)` reference-bin keys, in local calendar terms."""
    hour_of_day = frame["hour_of_day"]
    day_of_week = frame["day_of_week"]
    return day_of_week * 24 + hour_of_day, hour_of_day


class RobustZScoreDetector:
    """Per-cell, per-hour-of-week modified z-score over each KPI family.

    Fitted on a reference window; scored on any window. The fallback chain
    (hour-of-week -> hour-of-day -> whole cell) exists because 30 days of hourly data gives
    only four samples per hour-of-week bin, and a MAD computed from four points is as
    likely to be zero as it is to be useful.
    """

    name = NAME
    algorithm = "robust_zscore"
    version = VERSION
    detector = "robust_zscore"

    def __init__(
        self,
        *,
        kpis: Sequence[str] = KPI_COLUMNS,
        threshold: float = DEFAULT_Z_THRESHOLD,
        min_bin_samples: int = MIN_BIN_SAMPLES,
    ) -> None:
        require_ml("numpy", "pandas")
        self.kpis = tuple(kpis)
        self.threshold = threshold
        self.min_bin_samples = min_bin_samples
        self._reference: dict[str, Any] = {}

    def params(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "min_bin_samples": self.min_bin_samples,
            "kpis": list(self.kpis),
            "bins": "cell x hour_of_week, falling back to hour_of_day then cell",
        }

    def fit(self, frame: Any) -> RobustZScoreDetector:
        require_ml("numpy", "pandas")
        hour_of_week, hour_of_day = _bin_keys(frame)
        work = frame[["cell_id", *self.kpis]].copy()
        work["_how"] = hour_of_week
        work["_hod"] = hour_of_day
        levels = (
            ("how", ["cell_id", "_how"]),
            ("hod", ["cell_id", "_hod"]),
            ("cell", ["cell_id"]),
        )
        reference: dict[str, Any] = {}
        for level, keys in levels:
            grouped = work.groupby(keys, sort=False, observed=True)[list(self.kpis)]
            reference[level] = {
                "median": grouped.median(),
                "mad": grouped.agg(_median_absolute_deviation),
                "count": grouped.count(),
            }
        self._reference = reference
        return self

    def score_frame(self, frame: Any) -> Any:
        """Per-row, per-KPI robust z-scores as a DataFrame of the same length."""
        require_ml("numpy", "pandas")
        import numpy as np
        import pandas as pd  # type: ignore[import-untyped]

        if not self._reference:
            raise RuntimeError("RobustZScoreDetector.fit must be called before scoring")
        hour_of_week, hour_of_day = _bin_keys(frame)
        index = frame.index
        scores = pd.DataFrame(np.nan, index=index, columns=list(self.kpis))
        keys = {
            "how": pd.MultiIndex.from_arrays([frame["cell_id"], hour_of_week]),
            "hod": pd.MultiIndex.from_arrays([frame["cell_id"], hour_of_day]),
            "cell": pd.Index(frame["cell_id"]),
        }
        for level in ("how", "hod", "cell"):
            reference = self._reference[level]
            median = reference["median"].reindex(keys[level])
            mad = reference["mad"].reindex(keys[level])
            count = reference["count"].reindex(keys[level])
            for kpi in self.kpis:
                centre = median[kpi].to_numpy(dtype="float64")
                spread = mad[kpi].to_numpy(dtype="float64") * MAD_TO_SIGMA
                samples = count[kpi].to_numpy(dtype="float64")
                values = frame[kpi].to_numpy(dtype="float64")
                usable = (
                    np.isnan(scores[kpi].to_numpy(dtype="float64"))
                    & ~np.isnan(values)
                    & ~np.isnan(centre)
                    & (spread > 0)
                    & (samples >= self.min_bin_samples)
                )
                candidate = np.full(len(frame), np.nan)
                np.divide(values - centre, spread, out=candidate, where=usable)
                scores.loc[usable, kpi] = candidate[usable]
        return scores

    def detect(self, frame: Any, *, threshold: float | None = None) -> tuple[AnomalyScore, ...]:
        """One `AnomalyScore` per row: the worst |z| across KPI families."""
        import numpy as np

        limit = self.threshold if threshold is None else threshold
        scores = self.score_frame(frame)
        magnitude = scores.abs()
        worst = magnitude.max(axis=1).to_numpy(dtype="float64")
        results: list[AnomalyScore] = []
        kpis = list(self.kpis)
        matrix = magnitude.to_numpy(dtype="float64")
        cell_ids = frame["cell_id"].tolist()
        stamps = frame["ts"].tolist()
        for position in range(len(frame)):
            value = worst[position]
            flagged = bool(not np.isnan(value) and value >= limit)
            row = matrix[position]
            contributors = tuple(
                kpis[i]
                for i in np.argsort(-np.nan_to_num(row, nan=-1.0))
                if not np.isnan(row[i]) and row[i] >= limit
            )
            results.append(
                AnomalyScore(
                    cell_id=cell_ids[position],
                    ts=stamps[position].to_pydatetime(),
                    score=0.0 if np.isnan(value) else float(value),
                    is_anomaly=flagged,
                    threshold=limit,
                    detector=self.detector,
                    contributors=contributors,
                ),
            )
        return tuple(results)


class IsolationForestDetector:
    """Isolation Forest over the KPI vector, for combinations no single KPI reveals.

    Contributors are borrowed from a companion robust z-score detector, because an
    Isolation Forest path length is not an explanation an engineer can act on.
    """

    name = NAME
    algorithm = "isolation_forest"
    version = VERSION
    detector = "isolation_forest"

    def __init__(
        self,
        *,
        kpis: Sequence[str] = KPI_COLUMNS,
        contamination: float = DEFAULT_CONTAMINATION,
        n_estimators: int = 200,
        random_state: int = RANDOM_STATE,
        n_threads: int = 1,
    ) -> None:
        require_ml("numpy", "pandas", "sklearn")
        self.kpis = tuple(kpis)
        self.contamination = contamination
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.n_threads = n_threads
        self._model: Any = None
        self._medians: Any = None

    def params(self) -> dict[str, Any]:
        return {
            "contamination": self.contamination,
            "n_estimators": self.n_estimators,
            "random_state": self.random_state,
            "kpis": list(self.kpis),
        }

    def _matrix(self, frame: Any) -> Any:
        values = frame[list(self.kpis)].astype("float64")
        # Median imputation from the *reference* window only: a missing KPI must not shift
        # the decision boundary of the window being scored.
        return values.fillna(self._medians)

    def fit(self, frame: Any) -> IsolationForestDetector:
        require_ml("numpy", "pandas", "sklearn")
        from sklearn.ensemble import IsolationForest

        self._medians = frame[list(self.kpis)].astype("float64").median()
        model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=self.n_threads,
            bootstrap=False,
        )
        model.fit(self._matrix(frame))
        self._model = model
        return self

    def detect(
        self,
        frame: Any,
        *,
        companion: RobustZScoreDetector | None = None,
    ) -> tuple[AnomalyScore, ...]:
        if self._model is None:
            raise RuntimeError("IsolationForestDetector.fit must be called before scoring")
        matrix = self._matrix(frame)
        # `score_samples` is higher for normal points; negate so "larger is stranger",
        # matching the z-score detector's polarity.
        raw = -self._model.score_samples(matrix)
        flags = self._model.predict(matrix) == -1
        threshold = float(raw[flags].min()) if flags.any() else float("inf")
        contributors: tuple[tuple[str, ...], ...] = ((),) * len(frame)
        if companion is not None:
            companion_scores = companion.detect(frame)
            contributors = tuple(s.contributors for s in companion_scores)
        cell_ids = frame["cell_id"].tolist()
        stamps = frame["ts"].tolist()
        return tuple(
            AnomalyScore(
                cell_id=cell_ids[position],
                ts=stamps[position].to_pydatetime(),
                score=float(raw[position]),
                is_anomaly=bool(flags[position]),
                threshold=threshold,
                detector=self.detector,
                contributors=contributors[position],
            )
            for position in range(len(frame))
        )


def labels_for_windows(
    frame: Any,
    windows: Mapping[str, TimeWindow],
    *,
    cell_ids: Mapping[str, str] | None = None,
) -> Any:
    """Boolean labels for rows inside caller-supplied scenario windows.

    `windows` maps a scenario id to its window; `cell_ids` maps the same scenario id to the
    cell it affected (omit an entry to label every cell in that window). The labels come
    from the caller precisely so that no production code reads `ground_truth`.
    """
    require_ml("numpy", "pandas")
    import numpy as np

    labels = np.zeros(len(frame), dtype=bool)
    ts = frame["ts"]
    cells = frame["cell_id"]
    for scenario, window in windows.items():
        inside = (ts >= window.start) & (ts < window.end)
        target = (cell_ids or {}).get(scenario)
        if target is not None:
            inside = inside & (cells == target)
        labels |= inside.to_numpy(dtype=bool)
    return labels


def flags(scores: Sequence[AnomalyScore]) -> Any:
    """The boolean flag vector of a detector's output, for `detection_metrics`."""
    require_ml("numpy")
    import numpy as np

    return np.asarray([s.is_anomaly for s in scores], dtype=bool)
