"""The feature builder: one time-indexed row per (cell, hour), and the leakage gate.

Why this module is shaped the way it is
---------------------------------------
Forecasting a complaint surge is a time-series problem, and the easiest way to produce a
model that looks excellent and is worthless is to let a feature see the future. So the
layout is deliberately rigid:

* **A row's timestamp `ts` is the hour being predicted.** The target is what actually
  happened during `[ts, ts + 1h)`. The forecast origin is `ts`, so every feature may only
  use buckets **strictly before** `ts` - a one-hour-ahead forecast.
* **Observed columns are never features.** The ten KPI families, the complaint counts and
  the care-call counts as measured *at* `ts` live in the frame (the target derives from
  one of them, and the anomaly detectors read them), but they are excluded from
  `feature_columns` by construction and `assert_no_leakage` refuses them.
* **Derived features carry their shift in their name** (`_lag1`, `_lag168`,
  `_roll24_mean`). That is not decoration: `assert_no_leakage` re-derives each one from
  the frame and rejects it if the value does not match a window that ends strictly before
  `ts`. A rolling mean that includes the current row is caught this way.
* **Calendar features are allowed** because a calendar is knowable in advance. They are
  computed in **Africa/Nairobi local time**: the seeded evening peak is 18:00-22:00 local,
  which is 15:00-19:00 UTC, so computing `is_busy_hour` from the UTC hour would mislabel
  every busy hour in the dataset.

The ML dependencies (numpy/pandas/lightgbm/shap/scikit-learn) are an optional extra, so
this module imports cleanly without them and every public entry point calls
`require_ml()` first, which raises `UpstreamUnavailable` naming `make bootstrap-ml`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from tazama.core.errors import UpstreamUnavailable
from tazama.core.logging import get_logger
from tazama.domain.value_objects import TimeWindow

try:  # The `ml` extra is optional: `tazama.ai.ml` must import without it.
    import numpy as np
    import pandas as pd  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - covered by the no-ML-deps test
    _ML_IMPORT_ERROR: ImportError | None = exc
else:
    _ML_IMPORT_ERROR = None

_log = get_logger(__name__)

#: Packages that come from the optional `ml` extra.
ML_PACKAGES: tuple[str, ...] = ("numpy", "pandas")

#: What a caller must run when the extra is missing. Named in the error on purpose.
BOOTSTRAP_HINT = "make bootstrap-ml"

#: The ten KPI families (`kpi_cell` is wide: one column per family).
KPI_COLUMNS: tuple[str, ...] = (
    "availability_pct",
    "rrc_setup_success_pct",
    "erab_setup_success_pct",
    "drop_rate_pct",
    "prb_utilisation_pct",
    "dl_throughput_mbps",
    "ul_throughput_mbps",
    "latency_ms",
    "active_users",
    "sinr_db",
)

#: Demand signals measured at `ts`. `complaints_per_1k` is the normalisation PRD section 9
#: asks for - complaints per 1,000 subscribers - which makes cells of different size
#: comparable.
DEMAND_COLUMNS: tuple[str, ...] = ("complaint_count", "complaints_per_1k", "care_call_count")

#: Everything measured *at* `ts`. None of these may ever be a feature.
OBSERVED_COLUMNS: tuple[str, ...] = KPI_COLUMNS + DEMAND_COLUMNS

#: Knowable in advance, therefore legitimate features.
CALENDAR_COLUMNS: tuple[str, ...] = ("hour_of_day", "day_of_week", "is_weekend", "is_busy_hour")

#: Time-invariant cell attributes.
STATIC_COLUMNS: tuple[str, ...] = ("subscribers_est",)

TARGET_COLUMN = "target_complaints_per_1k"

#: Lags applied to every observed column: last hour, two hours ago, same hour yesterday,
#: same hour last week. 24 and 168 are what carry the seeded daily/weekly seasonality.
DEFAULT_LAGS: tuple[int, ...] = (1, 2, 24, 168)

#: Rolling means over windows that end strictly before `ts`.
DEFAULT_DEMAND_ROLLING: tuple[int, ...] = (3, 24, 168)
DEFAULT_KPI_ROLLING: tuple[int, ...] = (24,)

#: The longest history any feature reaches back for. The train/validation gap must be at
#: least this long.
MAX_LAG_HOURS = 168

#: The seeded evening peak, in **local** time (Africa/Nairobi), half-open.
BUSY_HOUR_LOCAL: tuple[int, int] = (18, 22)

_LAG_RE = re.compile(r"^(?P<src>.+)_lag(?P<k>\d+)$")
_ROLL_RE = re.compile(r"^(?P<src>.+)_roll(?P<w>\d+)_mean$")

#: How many future shifts of an observed column a feature is compared against when
#: hunting for a future-looking feature. Three is enough to catch `shift(-1..-3)` mistakes
#: without turning the check into a cross-product.
_FUTURE_PROBE_SHIFTS = (0, 1, 2, 3)

#: Rows sampled for the (quadratic) numeric leakage probes. The checks are about the
#: *construction* of a column, so a sample proves it as well as the whole frame does.
_PROBE_ROWS = 5_000

#: Spearman correlation above which a feature is treated as a monotone transform of the
#: target, i.e. the target in disguise.
_TARGET_RHO = 0.999


class LeakageDetected(ValueError):
    """A feature, target or split would let the model see information it cannot have.

    Deliberately not a `TazamaError`: this is a programming error caught by a gate, not
    something a user can be shown and retry.
    """


def require_ml(*packages: str) -> None:
    """Refuse to continue when the optional `ml` extra is not installed.

    Called at the top of every public entry point, which is what lets the package import
    (and therefore `tazama.api`, `tazama.worker` and the rest of the test suite) work
    without numpy, pandas, lightgbm, shap or scikit-learn present.
    """
    wanted = packages or ML_PACKAGES
    missing: list[str] = []
    for name in wanted:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):  # blocked, or a broken parent package
            missing.append(name)
    if not missing and _ML_IMPORT_ERROR is None:
        return
    if _ML_IMPORT_ERROR is not None and not missing:
        missing = [_ML_IMPORT_ERROR.name or "the ML extra"]
    names = sorted(set(missing))
    raise UpstreamUnavailable(
        f"the optional ML dependencies are not installed ({', '.join(names)}); "
        f"run `{BOOTSTRAP_HINT}`",
        missing=names,
        remedy=BOOTSTRAP_HINT,
    )


# ============================================================= port + port DTOs


@dataclass(frozen=True, slots=True)
class CellMeta:
    """Time-invariant facts about a cell, used for normalisation and grouping."""

    cell_id: str
    site_id: str
    region: str
    tech: str
    subscribers_est: int


@dataclass(frozen=True, slots=True)
class KpiObservation:
    """One `kpi_cell` row, flattened. `values` maps a KPI family to its value."""

    cell_id: str
    bucket_start: datetime
    values: Mapping[str, float | None]


@dataclass(frozen=True, slots=True)
class CountObservation:
    """A per-(cell, hour) count of complaints or care calls."""

    cell_id: str
    bucket_start: datetime
    count: int


@runtime_checkable
class FeatureSource(Protocol):
    """Everything the feature builder needs from the database.

    A Protocol, satisfied structurally by `infrastructure.db.ml_repo`: `ai/ml` never
    imports infrastructure (the layered import contract forbids it), so the adapter is
    wired in by the training CLI's composition root.
    """

    def cells(self, *, region: str | None = None, limit: int | None = None) -> tuple[CellMeta, ...]:
        """Cells in a stable, deterministic order."""
        ...

    def kpi_observations(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
        granularity: str = "1h",
    ) -> tuple[KpiObservation, ...]: ...

    def complaint_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
    ) -> tuple[CountObservation, ...]: ...

    def care_call_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Sequence[str],
    ) -> tuple[CountObservation, ...]: ...


# ==================================================================== the frame


@dataclass(frozen=True)
class FeatureFrame:
    """A built feature frame plus everything needed to reproduce and audit it."""

    frame: Any  # pandas.DataFrame; loosely typed so this module imports without pandas
    feature_columns: tuple[str, ...]
    target: str
    window: TimeWindow
    cell_ids: tuple[str, ...]
    data_hash: str
    built_at: datetime
    notes: tuple[str, ...] = ()

    @property
    def rows(self) -> int:
        return len(self.frame)

    def matrix(self, rows: Any = None) -> tuple[Any, Any]:
        """`(X, y)` for the given row selection (a boolean mask or an index array)."""
        frame = self.frame if rows is None else self.frame.loc[rows]
        return frame[list(self.feature_columns)], frame[self.target]

    def describe(self) -> str:
        return (
            f"{self.rows:,} rows x {len(self.feature_columns)} features, "
            f"{len(self.cell_ids)} cells, "
            f"{self.window.start:%Y-%m-%d %H:%M} -> {self.window.end:%Y-%m-%d %H:%M} UTC, "
            f"data_hash={self.data_hash[:12]}"
        )


@dataclass(frozen=True)
class TimeSplit:
    """A time-ordered train/validation/test split with a purge gap.

    `gap_hours` is the dead zone between the end of training and the start of validation.
    It must be at least the longest lag any feature uses, so no training row's lag window
    reaches into the validation period.
    """

    train: TimeWindow
    validation: TimeWindow
    test: TimeWindow
    gap_hours: int
    rows: Mapping[str, Any] | None = field(default=None)

    @property
    def parts(self) -> tuple[tuple[str, TimeWindow], ...]:
        return (("train", self.train), ("validation", self.validation), ("test", self.test))

    def mask(self, frame: Any, part: str) -> Any:
        """Boolean mask selecting one part of a frame."""
        if self.rows is not None:
            selected = np.zeros(len(frame), dtype=bool)
            selected[np.asarray(self.rows[part], dtype=int)] = True
            return selected
        window = dict(self.parts)[part]
        ts = frame["ts"]
        return (ts >= window.start) & (ts < window.end)

    def describe(self) -> str:
        return " | ".join(
            f"{name} {w.start:%m-%d %H:%M}->{w.end:%m-%d %H:%M}" for name, w in self.parts
        )

    @classmethod
    def from_rows(
        cls,
        frame: Any,
        *,
        train_rows: Sequence[int],
        validation_rows: Sequence[int],
        test_rows: Sequence[int],
        gap_hours: int = MAX_LAG_HOURS,
    ) -> TimeSplit:
        """Build a split from explicit row selections.

        Used by tests (and by anything that thinks it can shuffle rows): the windows are
        derived from the rows' own timestamps, so a non-time-ordered assignment produces
        overlapping windows and `assert_no_leakage` rejects it.
        """
        require_ml()

        def bounds(rows: Sequence[int], name: str) -> TimeWindow:
            if len(rows) == 0:
                raise ValueError(f"{name} selection is empty")
            ts = frame["ts"].iloc[list(rows)]
            return TimeWindow(
                start=ts.min().to_pydatetime(),
                end=ts.max().to_pydatetime() + timedelta(hours=1),
            )

        return cls(
            train=bounds(train_rows, "train"),
            validation=bounds(validation_rows, "validation"),
            test=bounds(test_rows, "test"),
            gap_hours=gap_hours,
            rows={
                "train": tuple(train_rows),
                "validation": tuple(validation_rows),
                "test": tuple(test_rows),
            },
        )


# ===================================================================== assembly


def _utc_index(window: TimeWindow) -> Any:
    return pd.date_range(window.start, window.end, freq="h", inclusive="left", tz="UTC")


def observation_frame(
    *,
    cells: Sequence[CellMeta],
    kpis: Sequence[KpiObservation],
    complaints: Sequence[CountObservation],
    care_calls: Sequence[CountObservation],
    window: TimeWindow,
) -> Any:
    """A dense (cell x hour) grid of *observed* values - no features yet.

    Dense matters: a missing complaint hour is a zero, not a gap, and a lag of 24 hours
    must mean 24 hours and not "24 rows ago".
    """
    require_ml()
    hours = _utc_index(window)
    cell_ids = [c.cell_id for c in cells]
    index = pd.MultiIndex.from_product([cell_ids, hours], names=["cell_id", "ts"])
    frame = index.to_frame(index=False)

    meta = pd.DataFrame(
        {
            "cell_id": cell_ids,
            "site_id": [c.site_id for c in cells],
            "region": [c.region for c in cells],
            "tech": [c.tech for c in cells],
            "subscribers_est": [max(int(c.subscribers_est), 0) for c in cells],
        },
    )
    frame = frame.merge(meta, on="cell_id", how="left")

    if kpis:
        kpi_frame = pd.DataFrame(
            [
                {
                    "cell_id": o.cell_id,
                    "ts": o.bucket_start,
                    **{k: o.values.get(k) for k in KPI_COLUMNS},
                }
                for o in kpis
            ],
        )
        kpi_frame["ts"] = pd.to_datetime(kpi_frame["ts"], utc=True)
        frame = frame.merge(kpi_frame, on=["cell_id", "ts"], how="left")
    else:
        for column in KPI_COLUMNS:
            frame[column] = np.nan

    for name, rows in (("complaint_count", complaints), ("care_call_count", care_calls)):
        if rows:
            counts = pd.DataFrame(
                {
                    "cell_id": [r.cell_id for r in rows],
                    "ts": pd.to_datetime([r.bucket_start for r in rows], utc=True),
                    name: [float(r.count) for r in rows],
                },
            )
            frame = frame.merge(counts, on=["cell_id", "ts"], how="left")
            frame[name] = frame[name].fillna(0.0)
        else:
            frame[name] = 0.0

    for column in KPI_COLUMNS:
        frame[column] = frame[column].astype("float64")

    # Complaints per 1,000 subscribers: the comparability normalisation of PRD section 9.
    subs = frame["subscribers_est"].astype("float64")
    frame["complaints_per_1k"] = np.where(
        subs > 0,
        frame["complaint_count"] * 1000.0 / subs.where(subs > 0, 1.0),
        np.nan,
    )

    return frame.sort_values(["cell_id", "ts"], kind="stable").reset_index(drop=True)


def add_calendar_features(frame: Any, *, tz: ZoneInfo | str = "Africa/Nairobi") -> Any:
    """Hour of day, day of week, weekend and busy-hour flags, in **local** time.

    The seed's evening peak is 18:00-22:00 Africa/Nairobi, which is 15:00-19:00 UTC.
    Deriving these from the UTC hour would shift every calendar feature by three hours and
    quietly destroy the model's grip on the daily cycle.
    """
    require_ml()
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(zone)
    low, high = BUSY_HOUR_LOCAL
    frame = frame.copy()
    frame["hour_of_day"] = local.dt.hour.astype("int16")
    frame["day_of_week"] = local.dt.dayofweek.astype("int16")
    frame["is_weekend"] = (local.dt.dayofweek >= 5).astype("int8")
    frame["is_busy_hour"] = ((local.dt.hour >= low) & (local.dt.hour < high)).astype("int8")
    return frame


def add_lag_features(
    frame: Any,
    *,
    lags: Sequence[int] = DEFAULT_LAGS,
    demand_rolling: Sequence[int] = DEFAULT_DEMAND_ROLLING,
    kpi_rolling: Sequence[int] = DEFAULT_KPI_ROLLING,
) -> tuple[Any, tuple[str, ...]]:
    """Add per-cell lagged and rolling features; return `(frame, derived_column_names)`.

    Every rolling window is shifted by one hour before it is taken, so it ends strictly
    before `ts`. `min_periods=1` keeps early history usable instead of throwing away the
    first week of every cell.
    """
    require_ml()
    if any(k < 1 for k in lags):
        raise LeakageDetected(f"lags must be >= 1 hour; got {sorted(lags)}")
    frame = frame.sort_values(["cell_id", "ts"], kind="stable").reset_index(drop=True)
    grouped = frame.groupby("cell_id", sort=False, observed=True)
    derived: list[str] = []
    new: dict[str, Any] = {}

    for column in OBSERVED_COLUMNS:
        if column not in frame.columns:
            continue
        for k in lags:
            name = f"{column}_lag{k}"
            new[name] = grouped[column].shift(k)
            derived.append(name)
        windows = demand_rolling if column in DEMAND_COLUMNS else kpi_rolling
        for w in windows:
            if w < 2:
                raise ValueError(f"rolling window must span at least 2 hours; got {w}")
            name = f"{column}_roll{w}_mean"
            new[name] = grouped[column].transform(
                lambda s, w=w: s.shift(1).rolling(w, min_periods=1).mean(),
            )
            derived.append(name)

    return pd.concat([frame, pd.DataFrame(new, index=frame.index)], axis=1), tuple(derived)


def data_hash(frame: Any, *, columns: Sequence[str] | None = None) -> str:
    """A stable digest of the data a model was trained on.

    Answers "which data was this?" without storing the data. Values are rounded to six
    decimal places first so a float formatting change does not invalidate the hash.
    """
    require_ml()
    digest = hashlib.sha256()
    digest.update(f"rows={len(frame)}".encode())
    for column in sorted(columns or [c for c in frame.columns if c != "ts"]):
        if column not in frame.columns:
            continue
        series = frame[column]
        digest.update(column.encode())
        if series.dtype.kind in "fiu":
            values = series.to_numpy(dtype="float64").round(6)
            digest.update(np.ascontiguousarray(values).tobytes())
        else:
            digest.update("|".join(series.astype(str).tolist()).encode())
    if "ts" in frame.columns:
        digest.update(np.ascontiguousarray(frame["ts"].astype("int64").to_numpy()).tobytes())
    return digest.hexdigest()


def select_cells(
    cells: Sequence[CellMeta],
    *,
    limit: int | None = None,
) -> tuple[CellMeta, ...]:
    """Deterministically subsample cells.

    Hash-ordered rather than "the busiest cells": picking by complaint volume would select
    on the target and bias every metric optimistically.
    """
    ordered = sorted(cells, key=lambda c: hashlib.sha256(c.cell_id.encode()).hexdigest())
    return tuple(ordered if limit is None else ordered[:limit])


def build_feature_frame(
    source: FeatureSource,
    window: TimeWindow,
    *,
    cell_limit: int | None = None,
    region: str | None = None,
    tz: ZoneInfo | str = "Africa/Nairobi",
    lags: Sequence[int] = DEFAULT_LAGS,
    now: datetime | None = None,
) -> FeatureFrame:
    """Read the window from the port and build the full feature frame."""
    require_ml()
    cells = select_cells(source.cells(region=region), limit=cell_limit)
    if not cells:
        raise ValueError("no cells available for the requested window")
    cell_ids = [c.cell_id for c in cells]

    frame = observation_frame(
        cells=cells,
        kpis=source.kpi_observations(window, cell_ids=cell_ids),
        complaints=source.complaint_counts(window, cell_ids=cell_ids),
        care_calls=source.care_call_counts(window, cell_ids=cell_ids),
        window=window,
    )
    frame = add_calendar_features(frame, tz=tz)
    frame, derived = add_lag_features(frame, lags=lags)

    # The target is the demand observed during [ts, ts+1h). Every feature is strictly
    # older than ts, so this is a one-hour-ahead forecast with no peeking.
    frame[TARGET_COLUMN] = frame["complaints_per_1k"].astype("float64")

    feature_columns = tuple(derived) + CALENDAR_COLUMNS + STATIC_COLUMNS
    observed = [c for c in OBSERVED_COLUMNS if c in frame.columns]
    return FeatureFrame(
        frame=frame,
        feature_columns=feature_columns,
        target=TARGET_COLUMN,
        window=window,
        cell_ids=tuple(cell_ids),
        data_hash=data_hash(frame, columns=["cell_id", *observed]),
        built_at=now or datetime.now(tz=UTC),
        notes=(f"cells={len(cell_ids)}", f"lags={tuple(lags)}"),
    )


def split_by_time(
    frame: Any,
    *,
    validation_hours: int = 72,
    test_hours: int = 72,
    gap_hours: int = MAX_LAG_HOURS,
) -> TimeSplit:
    """Carve the frame into train -> gap -> validation -> test, in time order.

    Taken from the end of the window backwards so the test set is always the most recent
    data, which is the only split that answers "would this have worked last week?".
    Validation and test are adjacent: both are out-of-sample, and the gap exists to stop a
    *training* row's lag window from touching validation.
    """
    require_ml()
    first = frame["ts"].min().to_pydatetime()
    last = frame["ts"].max().to_pydatetime() + timedelta(hours=1)
    test_start = last - timedelta(hours=test_hours)
    validation_start = test_start - timedelta(hours=validation_hours)
    train_end = validation_start - timedelta(hours=gap_hours)
    if train_end <= first:
        raise ValueError(
            f"window of {(last - first) // timedelta(hours=1)}h is too short for "
            f"validation={validation_hours}h + test={test_hours}h + gap={gap_hours}h",
        )
    return TimeSplit(
        train=TimeWindow(start=first, end=train_end),
        validation=TimeWindow(start=validation_start, end=test_start),
        test=TimeWindow(start=test_start, end=last),
        gap_hours=gap_hours,
    )


# =============================================================== the leakage gate


def max_lag_hours(feature_columns: Sequence[str]) -> int:
    """The furthest back any named feature reaches, in hours."""
    furthest = 0
    for name in feature_columns:
        lag = _LAG_RE.match(name)
        if lag:
            furthest = max(furthest, int(lag.group("k")))
            continue
        roll = _ROLL_RE.match(name)
        if roll:
            # A window of w hours ending at ts-1h reaches back w hours.
            furthest = max(furthest, int(roll.group("w")))
    return furthest


def _sample(frame: Any) -> Any:
    """A subset of **whole cells**, never a stride over rows.

    Striding would break the very relationship being checked: row `t` and row `t-1` must
    both survive for `shift(1)` to mean "one hour earlier".
    """
    if len(frame) <= _PROBE_ROWS:
        return frame
    cells = frame["cell_id"].drop_duplicates().tolist()
    per_cell = max(1, len(frame) // max(1, len(cells)))
    keep = max(1, _PROBE_ROWS // per_cell)
    return frame[frame["cell_id"].isin(set(cells[:keep]))]


def _matches(left: Any, right: Any) -> bool:
    """True when two series agree on **every** row they both define.

    Exact (to floating-point tolerance) rather than "mostly": the target is a sparse rate,
    so a lagged copy of it agrees with the unlagged column on ~95% of rows simply because
    both are zero. A partial-agreement rule turns every sparse lag into a false alarm,
    while real leakage - a column built with `shift(-1)`, or an unshifted rolling mean - is
    an exact reproduction and is still caught.
    """
    both = left.notna() & right.notna()
    if int(both.sum()) < 20:
        return False
    if left[both].nunique() < 2:
        return False
    return bool(
        np.isclose(
            left[both].to_numpy(dtype="float64"),
            right[both].to_numpy(dtype="float64"),
        ).all(),
    )


def _check_future_looking(frame: Any, feature_columns: Sequence[str], target: str) -> None:
    """Reject a feature whose values are an observed column at `ts` or later.

    Name-independent on purpose: a column called `availability_pct_lag1` that was actually
    built with `shift(-1)` is caught here, and so is anything called `_next` or `_lead`.
    """
    sample = _sample(frame)
    grouped = sample.groupby("cell_id", sort=False, observed=True)
    sources = [c for c in (*OBSERVED_COLUMNS, target) if c in sample.columns]
    shifted: dict[tuple[str, int], Any] = {
        (src, k): (grouped[src].shift(-k) if k else sample[src])
        for src in sources
        for k in _FUTURE_PROBE_SHIFTS
    }
    for name in feature_columns:
        values = sample[name]
        if values.dtype.kind not in "fiub":
            continue
        for (src, k), future in shifted.items():
            if _matches(values, future):
                when = "at t" if k == 0 else f"at t+{k}h"
                raise LeakageDetected(
                    f"feature {name!r} reproduces {src!r} {when}: a feature at t may only "
                    f"use information from strictly before t",
                )


def _check_derived_alignment(frame: Any, feature_columns: Sequence[str]) -> None:
    """Re-derive every `_lagN` / `_rollW_mean` feature and insist the values match.

    This is the check that catches a rolling mean including the current row: the shifted
    and unshifted windows are both computed and the mismatch is named precisely.
    """
    sample = _sample(frame)
    grouped = sample.groupby("cell_id", sort=False, observed=True)
    for name in feature_columns:
        lag = _LAG_RE.match(name)
        if lag:
            src, k = lag.group("src"), int(lag.group("k"))
            if k < 1:
                raise LeakageDetected(
                    f"feature {name!r} has a lag of {k}h: that is the value at t",
                )
            if src in sample.columns and not _matches(sample[name], grouped[src].shift(k)):
                raise LeakageDetected(
                    f"feature {name!r} does not match {src!r} shifted by {k}h; it was not "
                    f"built from history strictly before t",
                )
            continue
        roll = _ROLL_RE.match(name)
        if not roll:
            continue
        src, w = roll.group("src"), int(roll.group("w"))
        if src not in sample.columns:
            continue
        correct = grouped[src].transform(
            lambda s, w=w: s.shift(1).rolling(w, min_periods=1).mean(),
        )
        if _matches(sample[name], correct):
            continue
        including = grouped[src].transform(lambda s, w=w: s.rolling(w, min_periods=1).mean())
        if _matches(sample[name], including):
            raise LeakageDetected(
                f"feature {name!r} is a rolling mean that includes the current row: the "
                f"window must be shifted so it ends strictly before t",
            )
        raise LeakageDetected(
            f"feature {name!r} does not match a {w}h rolling mean of {src!r} ending "
            f"strictly before t",
        )


def _check_target_disguise(frame: Any, feature_columns: Sequence[str], target: str) -> None:
    """Reject a feature that is the target under a monotone transform.

    Spearman rather than Pearson, so `target * 10`, `log1p(target)` and any rank-preserving
    rescale are caught, not just an exact copy.
    """
    sample = _sample(frame)
    y = sample[target]
    if int(y.notna().sum()) < 20 or y.nunique() < 3:
        return
    for name in feature_columns:
        values = sample[name]
        if values.dtype.kind not in "fiu" or values.nunique() < 3:
            continue
        rho = values.corr(y, method="spearman")
        if rho is not None and not pd.isna(rho) and abs(float(rho)) >= _TARGET_RHO:
            raise LeakageDetected(
                f"feature {name!r} has Spearman rho={float(rho):.4f} against the target "
                f"{target!r}: it is the target in disguise",
            )


def _check_split(frame: Any, split: TimeSplit, feature_columns: Sequence[str]) -> None:
    order = split.parts
    for (earlier_name, earlier), (later_name, later) in pairwise(order):
        if earlier.end > later.start:
            raise LeakageDetected(
                f"split is not time-ordered: {earlier_name} ends at {earlier.end.isoformat()} "
                f"but {later_name} starts at {later.start.isoformat()}",
            )
    required = max_lag_hours(feature_columns)
    actual = int((split.validation.start - split.train.end) // timedelta(hours=1))
    if actual < required:
        raise LeakageDetected(
            f"train/validation gap is {actual}h but features reach back {required}h: a "
            f"training row's lag window would overlap the validation period",
        )
    if split.rows is None:
        return
    seen: dict[int, str] = {}
    for name, rows in split.rows.items():
        for row in np.asarray(rows, dtype=int).tolist():
            if row in seen:
                raise LeakageDetected(
                    f"row {row} appears in both {seen[row]} and {name}: splits must be disjoint",
                )
            seen[row] = name
    for name, window in order:
        part = frame["ts"].iloc[list(np.asarray(split.rows[name], dtype=int))]
        outside = int(((part < window.start) | (part >= window.end)).sum())
        if outside:
            raise LeakageDetected(
                f"{outside} rows assigned to {name} fall outside its own time window: "
                f"splits must be time-ordered, never shuffled",
            )


def assert_no_leakage(
    frame: Any,
    *,
    target: str,
    feature_columns: Sequence[str],
    split: TimeSplit | None = None,
) -> None:
    """The gate. Raise `LeakageDetected` with a precise message, or return silently.

    Checked, in order:
      1. the target is not itself a feature;
      2. every feature exists in the frame;
      3. no feature is a value observed *at* `ts` (the ten KPIs, the counts);
      4. every `_lagN` / `_rollW_mean` feature really is shifted strictly before `ts`;
      5. no feature reproduces an observed column at `ts` or later (future-looking);
      6. no feature is a monotone transform of the target;
      7. the split is time-ordered, disjoint, and separated by at least the maximum lag.
    """
    require_ml()
    features = list(feature_columns)
    if target in features:
        raise LeakageDetected(f"the target {target!r} is itself a feature")
    missing = [c for c in features if c not in frame.columns]
    if missing:
        raise LeakageDetected(f"feature columns absent from the frame: {sorted(missing)}")
    contemporaneous = [c for c in features if c in OBSERVED_COLUMNS]
    if contemporaneous:
        raise LeakageDetected(
            f"features {sorted(contemporaneous)} are values observed at t; only lagged or "
            f"calendar information is available when the forecast is made",
        )
    _check_derived_alignment(frame, features)
    _check_future_looking(frame, features, target)
    _check_target_disguise(frame, features, target)
    if split is not None:
        _check_split(frame, split, features)
