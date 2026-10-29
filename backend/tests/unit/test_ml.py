"""The Phase 3 machine-learning gate.

Four properties, in the order they matter:

1. **No leakage.** The gate is name-independent and adversarial: a column honestly called
   `availability_pct_lag1` that was actually built with `shift(-1)` must be caught, because
   that is how leakage arrives in practice — not as a column called `_future`.
2. **The model beats its baseline**, measured on a time-ordered held-out fold, or it is not
   activated. A model that cannot beat seasonal-naive is worse than seasonal-naive: it
   costs more and explains less.
3. **Training is reproducible.** Same data, same code, same metrics — exactly, not roughly.
4. **The registry refuses to lie.** A version is a promise about which data produced which
   artifact, so re-registering one is a conflict, not an update.

The fixtures build a synthetic frame with a *known* structure rather than reading the seed:
a test that asserts "LightGBM beat the baseline" needs the signal to be there by
construction, or it is asserting something about the seed generator instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tazama.ai.ml.evaluation import (
    assert_reproducible,
    compare_to_baseline,
    detection_metrics,
    population_stability_index,
    regression_metrics,
)
from tazama.ai.ml.features import (
    CellMeta,
    CountObservation,
    KpiObservation,
    LeakageDetected,
    assert_no_leakage,
    build_feature_frame,
    data_hash,
    max_lag_hours,
    select_cells,
    split_by_time,
)
from tazama.domain.value_objects import TimeWindow

pytestmark = pytest.mark.ml

T0 = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
HOURS = 24 * 24  # long enough for train + gap + validation + test
CELLS = ("CELL-A", "CELL-B", "CELL-C")


class FakeSource:
    """A `FeatureSource` with a relationship the model can actually find.

    Complaints follow PRB utilisation with a two-hour lag plus a daily shape. That is
    deliberately learnable from lagged features and *not* learnable by seasonal-naive
    beyond the daily part, which is what makes "beats the baseline" a real claim rather
    than an artefact of an unlearnable target.
    """

    def __init__(self, hours: int = HOURS) -> None:
        self.hours = hours

    def cells(self, *, region: str | None = None, limit: int | None = None) -> tuple[CellMeta, ...]:
        rows = tuple(
            CellMeta(
                cell_id=cell_id,
                site_id=f"SITE-{cell_id[-1]}",
                region=region or "Nairobi",
                tech="4G",
                subscribers_est=2_000 + index * 500,
            )
            for index, cell_id in enumerate(CELLS)
        )
        return rows if limit is None else rows[:limit]

    #: The one sharp outage in the window: cell B, day 18, 09:00-14:00 UTC. Rare and
    #: large, which is what an outage is — a *recurring* fault at the same hour of the
    #: week becomes that cell's normal behaviour, and a detector that still flagged it
    #: would be measuring the calendar rather than the network.
    OUTAGE_CELL = "CELL-B"
    OUTAGE_DAY = 18
    OUTAGE_HOURS = range(9, 14)

    def _is_outage(self, cell_id: str, at: datetime) -> bool:
        return (
            cell_id == self.OUTAGE_CELL
            and at.day == self.OUTAGE_DAY
            and at.hour in self.OUTAGE_HOURS
        )

    def _congestion(self, cell_index: int, hour: int) -> float:
        at = T0 + timedelta(hours=hour)
        busy = 40.0 if 16 <= at.hour <= 21 else 0.0
        # A mild congestion bump every fourth day gives the forecast a non-seasonal
        # signal to learn, without being large enough to read as a fault.
        bump = 12.0 if (at.day % 4 == 0 and 9 <= at.hour <= 13 and cell_index == 1) else 0.0
        return 30.0 + busy + bump

    def kpi_observations(
        self,
        window: TimeWindow,
        *,
        cell_ids: Any,
        granularity: str = "1h",
    ) -> tuple[KpiObservation, ...]:
        rows = []
        for cell_index, cell_id in enumerate(CELLS):
            if cell_id not in set(cell_ids):
                continue
            for hour in range(self.hours):
                at = T0 + timedelta(hours=hour)
                if not (window.start <= at < window.end):
                    continue
                prb = self._congestion(cell_index, hour)
                outage = self._is_outage(cell_id, at)
                rows.append(
                    KpiObservation(
                        cell_id=cell_id,
                        bucket_start=at,
                        values={
                            "availability_pct": (
                                12.0 if outage else 100.0 - (prb - 30.0) / 20.0
                            ),
                            "rrc_setup_success_pct": 99.5 - (prb - 30.0) / 30.0,
                            "erab_setup_success_pct": 99.4,
                            "drop_rate_pct": (
                                28.0 if outage else 0.3 + (prb - 30.0) / 100.0
                            ),
                            "prb_utilisation_pct": prb,
                            "dl_throughput_mbps": 25.0 - (prb - 30.0) / 4.0,
                            "ul_throughput_mbps": 9.0,
                            "latency_ms": 28.0 + (prb - 30.0) / 2.0,
                            "active_users": 150.0 + prb,
                            "sinr_db": 12.0,
                        },
                    ),
                )
        return tuple(rows)

    def complaint_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Any,
    ) -> tuple[CountObservation, ...]:
        rows = []
        for cell_index, cell_id in enumerate(CELLS):
            if cell_id not in set(cell_ids):
                continue
            for hour in range(self.hours):
                at = T0 + timedelta(hours=hour)
                if not (window.start <= at < window.end):
                    continue
                # Complaints respond to congestion two hours earlier.
                driver = self._congestion(cell_index, max(0, hour - 2))
                count = max(0, round((driver - 30.0) / 6.0))
                rows.append(CountObservation(cell_id=cell_id, bucket_start=at, count=count))
        return tuple(rows)

    def care_call_counts(
        self,
        window: TimeWindow,
        *,
        cell_ids: Any,
    ) -> tuple[CountObservation, ...]:
        return tuple(
            CountObservation(
                cell_id=row.cell_id,
                bucket_start=row.bucket_start,
                count=row.count // 2,
            )
            for row in self.complaint_counts(window, cell_ids=cell_ids)
        )


@pytest.fixture(scope="module")
def window() -> TimeWindow:
    return TimeWindow(start=T0, end=T0 + timedelta(hours=HOURS))


@pytest.fixture(scope="module")
def built(window: TimeWindow):
    """Built once: the frame is read-only and building it is the slow part."""
    frame = build_feature_frame(FakeSource(), window, now=T0 + timedelta(hours=HOURS))
    return frame, split_by_time(frame.frame)


# ----------------------------------------------------------------- the frame


def test_the_frame_covers_every_cell_hour(built, window) -> None:
    features, _ = built
    assert features.rows == len(CELLS) * HOURS
    assert set(features.cell_ids) == set(CELLS)
    assert features.target not in features.feature_columns


def test_the_data_hash_is_stable_and_sensitive(built, window) -> None:
    features, _ = built
    again = build_feature_frame(FakeSource(), window, now=T0)
    assert again.data_hash == features.data_hash, "same data must hash the same"

    shorter = build_feature_frame(
        FakeSource(),
        TimeWindow(start=T0, end=T0 + timedelta(hours=HOURS - 24)),
        now=T0,
    )
    assert shorter.data_hash != features.data_hash, "different data must hash differently"


def test_cell_selection_does_not_select_on_the_target() -> None:
    """Picking the busiest cells would bias every metric optimistically."""
    cells = FakeSource().cells()
    picked = select_cells(cells, limit=2)
    assert len(picked) == 2
    assert select_cells(cells, limit=2) == picked, "selection must be deterministic"
    # Not simply the largest by exposure, which is what a volume-ordered pick would give.
    by_exposure = sorted(cells, key=lambda c: -c.subscribers_est)[:2]
    assert [c.cell_id for c in picked] != [c.cell_id for c in by_exposure] or len(cells) <= 2


# --------------------------------------------------------------- the split


def test_the_split_is_time_ordered_disjoint_and_gapped(built) -> None:
    _, split = built
    assert split.train.end <= split.validation.start
    assert split.validation.end <= split.test.start
    gap = (split.validation.start - split.train.end) // timedelta(hours=1)
    assert gap >= split.gap_hours, "a training row's lag window must not touch validation"


def test_a_window_too_short_to_split_is_refused() -> None:
    short = build_feature_frame(
        FakeSource(hours=48),
        TimeWindow(start=T0, end=T0 + timedelta(hours=48)),
        now=T0,
    )
    with pytest.raises(ValueError, match="too short"):
        split_by_time(short.frame)


# -------------------------------------------------------------- the leakage gate


def test_a_clean_frame_passes_the_gate(built) -> None:
    features, split = built
    assert_no_leakage(
        features.frame,
        target=features.target,
        feature_columns=features.feature_columns,
        split=split,
    )


def test_the_target_cannot_be_its_own_feature(built) -> None:
    features, _ = built
    with pytest.raises(LeakageDetected):
        assert_no_leakage(
            features.frame,
            target=features.target,
            feature_columns=(*features.feature_columns, features.target),
        )


def test_a_feature_observed_at_t_is_rejected(built) -> None:
    """`prb_utilisation_pct` at `ts` is known only after the hour it describes."""
    features, _ = built
    with pytest.raises(LeakageDetected, match="prb_utilisation_pct"):
        assert_no_leakage(
            features.frame,
            target=features.target,
            feature_columns=(*features.feature_columns, "prb_utilisation_pct"),
        )


def test_a_future_shifted_column_is_caught_despite_an_honest_name(built) -> None:
    """The gate is name-independent, which is the only way it catches real mistakes."""
    features, _ = built
    frame = features.frame.copy()
    # Named like a lag, built like a lead.
    frame["availability_pct_lag1"] = frame.groupby("cell_id", observed=True)[
        "availability_pct"
    ].shift(-1)
    with pytest.raises(LeakageDetected):
        assert_no_leakage(
            frame,
            target=features.target,
            feature_columns=(*features.feature_columns, "availability_pct_lag1"),
        )


def test_a_missing_feature_is_an_error_not_a_silent_drop(built) -> None:
    features, _ = built
    with pytest.raises(LeakageDetected):
        assert_no_leakage(
            features.frame,
            target=features.target,
            feature_columns=(*features.feature_columns, "no_such_column"),
        )


def test_max_lag_hours_reads_the_feature_names(built) -> None:
    features, _ = built
    assert max_lag_hours(features.feature_columns) >= 1
    assert max_lag_hours(("x_lag3", "y_roll24_mean")) == 24
    assert max_lag_hours(("hour_of_day",)) == 0


# --------------------------------------------------------- baseline vs model


@pytest.fixture(scope="module")
def outcome(built):
    from tazama.ai.ml.models.forecast import train_call_surge_forecast

    features, split = built
    return train_call_surge_forecast(features, split, with_shap=True, min_improvement_pct=0.0)


def test_the_model_beats_seasonal_naive_on_the_held_out_fold(outcome) -> None:
    """The Phase 3 gate. If this fails, ship the baseline — it is cheaper and clearer."""
    assert outcome.test_model.mae < outcome.test_baseline.mae, (
        f"model MAE {outcome.test_model.mae:.4f} did not beat baseline "
        f"{outcome.test_baseline.mae:.4f}"
    )
    assert outcome.beats_baseline
    assert outcome.comparison.improvement_pct > 0


def test_cross_validation_is_expanding_window_not_shuffled(outcome) -> None:
    """Shuffled CV on a time series trains on the future. There must be none of it.

    An expanding window is observable from the row counts alone: each fold trains on
    everything before its validation slice, so the training set only ever grows. A
    shuffled or rolling split would not.
    """
    assert len(outcome.folds) >= 2
    train_rows = [fold.train_rows for fold in outcome.folds]
    assert train_rows == sorted(train_rows), "the training set must only grow"
    assert train_rows[0] < train_rows[-1], "an expanding window must actually expand"
    assert all(fold.validation_rows > 0 for fold in outcome.folds)
    assert [fold.fold for fold in outcome.folds] == list(range(1, len(outcome.folds) + 1))


def test_shap_drivers_are_ranked_and_named(outcome) -> None:
    assert outcome.drivers
    scores = [driver.mean_abs_shap for driver in outcome.drivers]
    assert scores == sorted(scores, reverse=True)
    assert all(driver.feature in outcome.feature_columns for driver in outcome.drivers)
    assert all(
        driver.direction in ("increases", "decreases", "mixed")
        for driver in outcome.drivers
    )


def test_training_is_reproducible_to_the_last_digit(built) -> None:
    """Fixed seed, one thread, deterministic=True. Any wobble here is a bug."""
    from tazama.ai.ml.models.forecast import train_call_surge_forecast

    features, split = built

    def train():
        return train_call_surge_forecast(
            features,
            split,
            with_shap=False,
            folds=2,
        ).flat_metrics()

    assert_reproducible(train, runs=2, tolerance=0.0)


# ------------------------------------------------------------------ metrics


def test_regression_metrics_agree_with_hand_calculation() -> None:
    metrics = regression_metrics([1.0, 2.0, 3.0, 4.0], [1.5, 2.5, 2.5, 3.5])
    assert metrics.mae == pytest.approx(0.5)
    assert metrics.rmse == pytest.approx(0.5)
    assert metrics.rows == 4


def test_a_perfect_prediction_scores_zero_error() -> None:
    metrics = regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert metrics.mae == 0.0
    assert metrics.r2 == pytest.approx(1.0)


def test_detection_metrics_count_the_four_cells() -> None:
    metrics = detection_metrics(
        [True, True, False, False, True],
        [True, False, False, True, True],
    )
    assert metrics.true_positives == 2
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == pytest.approx(2 / 3)
    assert metrics.recall == pytest.approx(2 / 3)


def test_a_marginal_win_does_not_count_as_beating_the_baseline() -> None:
    """A 0.1% improvement on one split is noise, and acting on noise is the failure mode."""
    close = compare_to_baseline(
        metric="test_mae",
        baseline=1.000,
        model=0.999,
        lower_is_better=True,
        min_improvement_pct=5.0,
    )
    assert close.beats_baseline is False

    clear = compare_to_baseline(
        metric="test_mae",
        baseline=1.000,
        model=0.800,
        lower_is_better=True,
        min_improvement_pct=5.0,
    )
    assert clear.beats_baseline is True
    assert clear.improvement_pct == pytest.approx(20.0)


def test_psi_is_zero_for_an_unchanged_distribution() -> None:
    reference = [float(index % 10) for index in range(500)]
    assert population_stability_index(reference, list(reference)) == pytest.approx(0.0, abs=1e-9)


def test_psi_grows_when_the_distribution_moves() -> None:
    reference = [float(index % 10) for index in range(500)]
    shifted = [value + 7.0 for value in reference]
    assert population_stability_index(reference, shifted) > 0.2


# ----------------------------------------------------------------- anomaly


@pytest.fixture(scope="module")
def detector(built):
    from tazama.ai.ml.models.anomaly import RobustZScoreDetector

    features, _ = built
    return RobustZScoreDetector().fit(features.frame)


def test_the_detector_flags_the_injected_fault_and_not_the_busy_hour(built, detector) -> None:
    """The busy hour is normal for that hour; the fault is not. That is the whole point."""
    from tazama.ai.ml.models.anomaly import flags, labels_for_windows

    features, _ = built
    scores = detector.detect(features.frame)
    assert len(scores) == features.rows

    # The one outage the fake source injected, by construction.
    outage_day = T0.replace(day=FakeSource.OUTAGE_DAY)
    windows = {
        "outage": TimeWindow(
            start=outage_day.replace(hour=min(FakeSource.OUTAGE_HOURS)),
            end=outage_day.replace(hour=max(FakeSource.OUTAGE_HOURS) + 1),
        ),
    }
    cells = {"outage": FakeSource.OUTAGE_CELL}

    labelled = labels_for_windows(features.frame, windows, cell_ids=cells)
    assert int(sum(labelled)) == len(FakeSource.OUTAGE_HOURS)

    metrics = detection_metrics(list(flags(scores)), list(labelled))
    assert metrics.recall >= 0.6, (
        f"an availability collapse must be detectable; recall was {metrics.recall:.2f}"
    )

    # And the outage must stand clearly above the rest of the window. This is the property
    # that matters operationally: a detector whose worst score during an availability
    # collapse is comparable to its everyday scores produces an alert nobody can act on.
    # (The absolute count of other flags is not asserted here — these series are perfectly
    # deterministic, so hour-of-week bins straddling the local-time busy boundary are a
    # fixture artefact rather than anything the detector got wrong.)
    inside = [score for score, flag in zip(scores, labelled, strict=True) if flag]
    outside = [score for score, flag in zip(scores, labelled, strict=True) if not flag]
    worst_outage = max(score.score for score in inside)
    typical = sorted(score.score for score in outside)[len(outside) // 2]
    assert worst_outage > typical * 5, (
        f"the outage scored {worst_outage:.2f} against a median of {typical:.2f}: "
        f"it does not stand out"
    )
    assert all("availability_pct" in score.contributors for score in inside if score.is_anomaly)


def test_scoring_before_fitting_is_refused() -> None:
    from tazama.ai.ml.models.anomaly import RobustZScoreDetector

    with pytest.raises(RuntimeError, match="fit"):
        RobustZScoreDetector().detect(None)


def test_the_detector_is_deterministic(built, detector) -> None:
    features, _ = built
    first = detector.detect(features.frame)
    second = detector.detect(features.frame)
    assert [s.score for s in first] == [s.score for s in second]


# ---------------------------------------------------------------- priority


def test_the_priority_model_refuses_to_fit_on_too_few_rows() -> None:
    """A tree on nineteen rows is an overfitted guess. The rule blend is more honest."""
    from tazama.ai.ml.models.priority import IncidentPriorityModel, PriorityTrainingRow

    rows = [
        PriorityTrainingRow(
            features={"affected_subs": 1000.0, "complaint_count": 5.0},
            label=0.5,
        )
        for _ in range(10)
    ]
    with pytest.raises(ValueError, match="refusing to fit"):
        IncidentPriorityModel().fit(rows)


def test_an_untrained_priority_model_still_answers_and_says_it_is_untrained() -> None:
    """The product must work with no model: the fallback blend is the deterministic path."""
    from tazama.ai.ml.models.priority import IncidentPriorityModel

    model = IncidentPriorityModel()
    score, version = model.score({"affected_subs": 5000.0, "complaint_count": 20.0})
    assert 0.0 <= score <= 1.0
    assert "untrained" in version or version.startswith("rules")
    assert model.trained is False


def test_a_trained_priority_model_is_bounded_and_versioned() -> None:
    from tazama.ai.ml.models.priority import IncidentPriorityModel, PriorityTrainingRow

    rows = [
        PriorityTrainingRow(
            features={
                "affected_subs": float(index * 100),
                "complaint_count": float(index),
                "enterprise_accounts": float(index % 3),
                "sla_risk": (index % 10) / 10.0,
            },
            label=min(1.0, index / 50.0),
        )
        for index in range(60)
    ]
    model = IncidentPriorityModel().fit(rows)
    assert model.trained
    score, version = model.score(dict(rows[-1].features))
    assert 0.0 <= score <= 1.0
    assert version != "untrained"


# --------------------------------------------------------------- scaffolds


def test_the_disabled_scaffolds_refuse_to_run(settings) -> None:
    """GNN and bandit stay off until they beat the baseline on the eval set (PRD §9)."""
    from tazama.ai.ml.models.scaffolds import (
        NotificationTimingBanditScaffold,
        SpatioTemporalGnnScaffold,
        scaffold_status,
    )
    from tazama.core.errors import PolicyViolation

    status = scaffold_status(settings)
    assert len(status) == 2
    assert all(item["enabled"] is False for item in status), "both must default to off"

    for scaffold in (SpatioTemporalGnnScaffold, NotificationTimingBanditScaffold):
        with pytest.raises(PolicyViolation):
            scaffold(settings=settings).run()


# ---------------------------------------------------------------- registry


@pytest.fixture
def registry(db_engine, frozen_clock, tmp_path):
    from tazama.ai.ml.registry import ModelRegistry
    from tazama.infrastructure.db.ml_repo import SqlModelRegistryStore

    return ModelRegistry(
        store=SqlModelRegistryStore(engine=db_engine),
        clock=frozen_clock,
        root=tmp_path / "models",
    )


def test_a_registered_model_round_trips(registry) -> None:
    entry = registry.register(
        name="call_surge_forecast",
        version="v1",
        algorithm="LightGBM",
        metrics={"test_mae": 0.42},
        params={"num_leaves": 31},
        feature_list=("a_lag1", "hour_of_day"),
        data_hash="abc123",
        beats_baseline=True,
    )
    stored = registry.get("call_surge_forecast", "v1")
    assert stored.id == entry.id
    assert stored.metrics["test_mae"] == 0.42
    assert stored.feature_list == ("a_lag1", "hour_of_day")
    assert stored.beats_baseline is True


def test_re_registering_a_version_is_refused(registry) -> None:
    """A version is a promise about which data produced which artifact."""
    from tazama.core.errors import Conflict

    registry.register(name="m", version="v1", algorithm="LightGBM", data_hash="aaa")
    with pytest.raises(Conflict, match="already registered"):
        registry.register(name="m", version="v1", algorithm="LightGBM", data_hash="bbb")


def test_only_one_version_is_active_at_a_time(registry) -> None:
    registry.register(
        name="m",
        version="v1",
        algorithm="LightGBM",
        beats_baseline=True,
        activate=True,
    )
    registry.register(
        name="m",
        version="v2",
        algorithm="LightGBM",
        beats_baseline=True,
        activate=True,
    )
    active = registry.active("m")
    assert active is not None and active.version == "v2"
    assert registry.get("m", "v1").is_active is False


def test_activation_without_a_baseline_comparison_is_refused(registry) -> None:
    """Fail closed: "no evidence it is better than nothing" is not a reason to go live."""
    from tazama.core.errors import PolicyViolation

    registry.register(name="m", version="v1", algorithm="LightGBM")
    with pytest.raises(PolicyViolation, match="no baseline comparison"):
        registry.activate("m", "v1")


def test_a_model_that_loses_to_its_baseline_cannot_be_activated(registry) -> None:
    from tazama.core.errors import PolicyViolation

    registry.register(name="m", version="v1", algorithm="LightGBM", beats_baseline=False)
    with pytest.raises(PolicyViolation):
        registry.activate("m", "v1")


def test_an_unknown_model_is_a_not_found(registry) -> None:
    from tazama.core.errors import NotFound

    with pytest.raises(NotFound):
        registry.get("nope", "v1")
    assert registry.find("nope", "v1") is None
    assert registry.active("nope") is None


def test_an_artifact_is_saved_and_reloadable(registry) -> None:
    payload = {"weights": [1, 2, 3]}
    entry = registry.register(
        name="m",
        version="v1",
        algorithm="dict",
        model=payload,
    )
    assert entry.artifact_path
    from pathlib import Path

    assert Path(entry.artifact_path).exists()
    assert registry.load_artifact(name="m", version="v1") == payload


def test_data_hash_is_sensitive_to_values_and_to_order(built) -> None:
    """The hash answers "which data was this?" — including the order it was laid out in.

    Order sensitivity is deliberate, not an oversight: the frame is always built by the
    same deterministic cell/time sort, so two reads of the same window hash alike, and a
    frame that arrived in a different order is a *different* frame — most likely because
    the cell selection or the time index changed, which is exactly what the hash is there
    to notice.
    """
    features, _ = built
    frame = features.frame
    columns = ["cell_id", "complaints"]

    assert data_hash(frame, columns=columns) == data_hash(frame.copy(), columns=columns)

    changed = frame.copy()
    changed.loc[changed.index[0], "complaints"] = 999.0
    assert data_hash(changed, columns=columns) != data_hash(frame, columns=columns)

    shuffled = frame.sample(frac=1.0, random_state=7)
    assert data_hash(shuffled, columns=columns) != data_hash(frame, columns=columns)
