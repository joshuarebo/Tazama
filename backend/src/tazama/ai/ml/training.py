"""`make train` — the Phase 3 training run, end to end and reproducible.

This is the composition root for machine learning. `ai/ml` is forbidden from importing
infrastructure (the layered import contract), so the adapters are wired here and nowhere
else, exactly as `api/deps.py` does for the request path.

What one run does, in order:

1. Read the window from the database through `SqlFeatureSource` and build the feature
   frame. The window ends at the **latest hour of data**, not at `now`: a seeded database
   sits still, and anchoring on the wall clock would train on an empty tail.
2. Split train → gap → validation → test, strictly in time order.
3. Run the leakage gate. It raises before a single tree is grown, because a model trained
   on a leaky frame is not a weak model, it is a confident wrong answer.
4. Fit the seasonal-naive baseline and the LightGBM forecast on the same split, and
   compare them on the test fold.
5. Fit the KPI anomaly detector and score it against the seeded incident windows, which
   are the only ground truth that exists.
6. Register every model with its metrics, params, feature list and data hash, and write a
   model card beside it. **A model that does not beat its baseline is still registered and
   still gets a card — it is simply not activated.** Hiding a failed run is how a team
   ends up believing a model works.

The exit code is the gate: non-zero when the forecast fails to beat the baseline, so
`make train` and CI agree on what "done" means.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from tazama.ai.ml.evaluation import detection_metrics
from tazama.ai.ml.features import FeatureFrame, TimeSplit, build_feature_frame, split_by_time
from tazama.ai.ml.models.anomaly import RobustZScoreDetector, flags, labels_for_windows
from tazama.ai.ml.models.forecast import ForecastOutcome, train_call_surge_forecast
from tazama.ai.ml.models.scaffolds import scaffold_status
from tazama.ai.ml.registry import EvalRunRecord, ModelRegistry
from tazama.core.clock import Clock, SystemClock
from tazama.core.logging import configure_logging, get_logger
from tazama.core.paths import repo_root
from tazama.core.settings import Settings, get_settings

_log = get_logger(__name__)

#: How much history one run trains on. Thirty days of hourly buckets is 720 points per
#: cell: enough for an hour-of-week profile, and all the seed produces.
DEFAULT_DAYS = 30

#: How many cells to train on. The full network is ~600 cells x 720 hours, which is more
#: than a demo laptop should spend on `make train`; the subsample is hash-ordered, never
#: volume-ordered, so it does not select on the target.
DEFAULT_CELL_LIMIT = 60

#: The forecast must beat seasonal-naive by this much on test MAE to be activated. Not
#: zero: a 0.1% win on one split is noise, and activating on noise is how a model that
#: does nothing ends up in the loop.
MIN_IMPROVEMENT_PCT = 5.0

#: The detector must catch at least this share of the seeded incident windows. A missed
#: degradation is a customer-visible outage, so recall is the floor that matters.
ANOMALY_MIN_RECALL = 0.50

#: And it must not flag more than this share of cell-hours.
#:
#: Precision deliberately is **not** gated on. The labels are incident windows, and an
#: incident is raised only where correlation had enough evidence to raise one — so a
#: genuine degradation that produced no incident counts as a false positive, and measured
#: precision lands near 0.005 however good the detector is. Gating on a number that cannot
#: be achieved teaches a team to disable the gate. The operational question a flag rate
#: actually answers is "will this drown the NOC?", which is what is enforced here;
#: precision is still recorded, on the card and in `eval_run`, as a number to watch.
ANOMALY_MAX_FLAG_RATE = 0.05

CARDS_DIR = "docs/model-cards"


@dataclass(frozen=True, slots=True)
class TrainingReport:
    """What one run produced. Returned so tests can assert on it without parsing stdout."""

    window_start: datetime
    window_end: datetime
    rows: int
    cells: int
    data_hash: str
    forecast: ForecastOutcome
    anomaly_metrics: dict[str, Any]
    registered: tuple[str, ...]
    cards: tuple[str, ...]
    passed: bool

    def render(self) -> str:
        comparison = self.forecast.comparison
        lines = [
            "training run",
            f"  window      {self.window_start:%Y-%m-%d %H:%M} -> "
            f"{self.window_end:%Y-%m-%d %H:%M} UTC",
            f"  frame       {self.rows:,} rows, {self.cells} cells, "
            f"data_hash={self.data_hash[:12]}",
            "",
            "  call-surge forecast (lower MAE is better)",
            f"    baseline  test MAE {self.forecast.test_baseline.mae:.4f}",
            f"    model     test MAE {self.forecast.test_model.mae:.4f}",
            f"    verdict   {comparison.summary()}",
            "",
            "  KPI anomaly detection vs seeded incident windows",
            f"    recall {self.anomaly_metrics.get('recall', 0.0):.3f}"
            f"  flag rate {self.anomaly_metrics.get('flag_rate', 0.0):.2%}"
            f"  (precision {self.anomaly_metrics.get('precision', 0.0):.4f}, reported "
            f"not gated)",
            "",
            f"  registered  {', '.join(self.registered) or 'nothing'}",
            f"  cards       {', '.join(self.cards) or 'none'}",
            "",
            f"  GATE        {'PASS' if self.passed else 'FAIL'}",
        ]
        if self.forecast.drivers:
            lines.append("")
            lines.append("  top drivers (SHAP, mean |contribution|)")
            for driver in self.forecast.drivers[:8]:
                lines.append(
                    f"    {driver.feature:<34} {driver.mean_abs_shap:.3e}  "
                    f"{driver.direction}",
                )
        return "\n".join(lines)


def _latest_hour(engine: Any) -> datetime | None:
    """The last whole hour of KPI data, or None when the database is empty."""
    from sqlalchemy import func, select

    from tazama.infrastructure.db.engine import session_factory
    from tazama.infrastructure.db.models import KpiCell

    with session_factory(engine)() as session:
        latest = session.execute(select(func.max(KpiCell.bucket_start))).scalar_one_or_none()
    if latest is None:
        return None
    latest = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
    return latest.replace(minute=0, second=0, microsecond=0)


def build_frame(
    *,
    settings: Settings,
    engine: Any,
    days: int = DEFAULT_DAYS,
    cell_limit: int | None = DEFAULT_CELL_LIMIT,
    region: str | None = None,
    clock: Clock | None = None,
) -> tuple[FeatureFrame, TimeSplit]:
    """The frame and its split, anchored on the data rather than the wall clock."""
    from tazama.domain.value_objects import TimeWindow
    from tazama.infrastructure.db.ml_repo import SqlFeatureSource

    end = _latest_hour(engine)
    if end is None:
        raise SystemExit("no KPI data to train on — run `make seed` first")
    window = TimeWindow(start=end - timedelta(days=days), end=end)

    source = SqlFeatureSource(engine=engine)
    features = build_feature_frame(
        source,
        window,
        cell_limit=cell_limit,
        region=region,
        tz=settings.DISPLAY_TZ,
        now=(clock or SystemClock()).now(),
    )
    return features, split_by_time(features.frame)


def _train_anomaly(features: FeatureFrame, engine: Any) -> tuple[RobustZScoreDetector, dict]:
    """Fit the detector and score it against the seeded incident windows."""
    from sqlalchemy import select

    from tazama.infrastructure.db.engine import session_factory
    from tazama.infrastructure.db.models import Incident, IncidentLink

    detector = RobustZScoreDetector().fit(features.frame)
    scores = detector.detect(features.frame)

    # Ground truth: the cell/time windows the seed actually degraded. Nothing else in the
    # system knows when a fault was real.
    with session_factory(engine)() as session:
        rows = session.execute(
            select(
                IncidentLink.entity_id,
                Incident.started_at,
                Incident.detected_at,
                Incident.resolved_at,
            )
            .join(Incident, Incident.id == IncidentLink.incident_id)
            .where(IncidentLink.entity_type == "cell"),
        ).all()

    from tazama.domain.value_objects import TimeWindow

    windows: dict[str, TimeWindow] = {}
    cells: dict[str, str] = {}
    for index, (cell_id, started_at, detected_at, resolved_at) in enumerate(rows):
        start = started_at or detected_at
        if start is None:
            continue
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        end = resolved_at or (start + timedelta(hours=6))
        end = end if end.tzinfo else end.replace(tzinfo=UTC)
        if end <= start:
            continue
        key = f"inc-{index}"
        windows[key] = TimeWindow(start=start, end=end)
        cells[key] = str(cell_id)

    labelled = labels_for_windows(features.frame, windows, cell_ids=cells)
    metrics = detection_metrics(list(flags(scores)), list(labelled))
    return detector, metrics.to_dict()


def _write_card(
    *,
    name: str,
    version: str,
    algorithm: str,
    purpose: str,
    metrics: dict[str, Any],
    params: dict[str, Any],
    features: FeatureFrame,
    limitations: list[str],
    beats_baseline: bool | None,
    root: Path,
) -> str:
    """One model card per registered model, written where the PRD says they live.

    The limitations section is not boilerplate: on synthetic data every metric here is
    optimistic, and a card that does not say so is worse than no card.
    """
    directory = root / CARDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    verdict = {
        True: "Beats its baseline on the held-out test fold.",
        False: "**Does not beat its baseline.** Not activated; the baseline stays in use.",
        None: "No baseline comparison applies to this model.",
    }[beats_baseline]

    path.write_text(
        "\n".join(
            [
                f"# Model card — {name}",
                "",
                f"- **Version** `{version}`",
                f"- **Algorithm** {algorithm}",
                f"- **Trained on** {features.window.start:%Y-%m-%d %H:%M} to "
                f"{features.window.end:%Y-%m-%d %H:%M} UTC "
                f"({features.rows:,} rows, {len(features.cell_ids)} cells)",
                f"- **Data hash** `{features.data_hash}`",
                f"- **Verdict** {verdict}",
                "",
                "## Purpose",
                "",
                purpose,
                "",
                "## Metrics",
                "",
                "```json",
                json.dumps(metrics, indent=2, default=str),
                "```",
                "",
                "## Parameters",
                "",
                "```json",
                json.dumps(params, indent=2, default=str),
                "```",
                "",
                "## Features",
                "",
                "".join(f"- `{column}`\n" for column in features.feature_columns),
                "## Limitations",
                "",
                *[f"- {item}" for item in limitations],
                "",
                "## Ethical and operational notes",
                "",
                "- The model informs a human. It executes nothing: no runbook, no customer",
                "  message, no network change follows from its output (PRD §9, §11).",
                "- No subscriber identifier reaches training. Features are cell-level",
                "  aggregates; complaint counts are normalised by `cell.subscribers_est`.",
                "- Retraining is a new version, never an edit. The registry refuses to",
                "  overwrite a version, because the data hash recorded against it is a",
                "  promise about which data produced which artifact.",
                "",
                "_Generated by `make train`. Do not edit by hand._",
                "",
            ],
        ),
        encoding="utf-8",
    )
    return str(path.relative_to(root))


def run(
    *,
    settings: Settings | None = None,
    days: int = DEFAULT_DAYS,
    cell_limit: int | None = DEFAULT_CELL_LIMIT,
    region: str | None = None,
    version: str | None = None,
    clock: Clock | None = None,
    with_shap: bool = True,
    activate: bool = True,
    write_cards: bool = True,
) -> TrainingReport:
    """Train, evaluate, register and report. Raises nothing on a failed gate — see `passed`."""
    resolved = settings or get_settings()
    the_clock = clock or SystemClock()

    from tazama.infrastructure.db.engine import get_engine
    from tazama.infrastructure.db.ml_repo import SqlEvalRunSink, SqlModelRegistryStore

    engine = get_engine(resolved)
    features, split = build_frame(
        settings=resolved,
        engine=engine,
        days=days,
        cell_limit=cell_limit,
        region=region,
        clock=the_clock,
    )
    _log.info("frame built", extra={"frame": features.describe()})

    forecast = train_call_surge_forecast(
        features,
        split,
        with_shap=with_shap,
        min_improvement_pct=MIN_IMPROVEMENT_PCT,
    )
    detector, anomaly_metrics = _train_anomaly(features, engine)

    # The version is derived from the data and from the gate it was judged against, not
    # from the clock: the same data under the same thresholds must produce the same
    # version, or "which model saw which rows" is unanswerable. Including the thresholds
    # matters because a verdict is part of what a version records — when a floor moves, the
    # same data honestly yields a *different* conclusion, and that belongs in a new version
    # rather than silently contradicting the row already on file.
    gate_fingerprint = sha256(
        json.dumps(
            {
                "forecast_min_improvement_pct": MIN_IMPROVEMENT_PCT,
                "anomaly_min_recall": ANOMALY_MIN_RECALL,
                "anomaly_max_flag_rate": ANOMALY_MAX_FLAG_RATE,
            },
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    resolved_version = version or f"v{features.data_hash[:8]}-{gate_fingerprint[:4]}"
    root = repo_root()
    registry = ModelRegistry(
        store=SqlModelRegistryStore(engine=engine),
        clock=the_clock,
        root=root / "artifacts" / "models",
    )

    registered: list[str] = []
    cards: list[str] = []

    flag_rate = anomaly_metrics.get("flag_rate", 0.0)
    anomaly_passed = (
        anomaly_metrics.get("recall", 0.0) >= ANOMALY_MIN_RECALL
        and flag_rate <= ANOMALY_MAX_FLAG_RATE
    )

    for name, algorithm, model, metrics, params, purpose, limitations, beats in (
        (
            "call_surge_forecast",
            "LightGBM regressor",
            forecast.model,
            forecast.metrics(),
            dict(forecast.params),
            "One-hour-ahead forecast of complaints per 1,000 subscribers per cell, so care "
            "staffing and proactive notification can be prepared before the calls arrive.",
            [
                "Trained on synthetic data. Every metric here is optimistic: the seed's "
                "relationships are the ones the generator put there, and a real network is "
                "noisier and less consistent.",
                "Validated on a single time-ordered split plus expanding-window CV. No "
                "shuffled cross-validation is used anywhere, and none should be.",
                "Features are cell-level. A cell whose `subscribers_est` is stale will be "
                "forecast badly and nothing downstream can detect that from the forecast.",
                "A one-hour horizon only. Nothing here supports a longer forecast.",
            ],
            forecast.beats_baseline,
        ),
        (
            "kpi_anomaly_detector",
            "Robust z-score over hour-of-week baselines (MAD)",
            detector,
            {
                "detection": anomaly_metrics,
                "thresholds": {
                    "min_recall": ANOMALY_MIN_RECALL,
                    "max_flag_rate": ANOMALY_MAX_FLAG_RATE,
                },
            },
            detector.params(),
            "Flags per-cell KPI behaviour that departs from that cell's own hour-of-week "
            "norm, giving correlation a network-first entry point that does not wait for a "
            "complaint.",
            [
                "Measured against the seeded incident windows, which are the only ground "
                "truth available; precision is understated because a real degradation "
                "outside a labelled window counts as a false positive.",
                "A robust z-score is unconditional: it says 'unusual', never 'bad'. A "
                "planned maintenance window looks exactly like a fault to it, which is why "
                "suppression lives in correlation and not here.",
                "Needs roughly two weeks of history per cell before the hour-of-week "
                "baseline is meaningful; it falls back to coarser bins below that.",
            ],
            anomaly_passed,
        ),
    ):
        # The version is derived from the data, so re-running `make train` on an unchanged
        # database legitimately produces the same version — and the registry rightly
        # refuses to overwrite one. A gate that crashes on its second run is not a gate, so
        # an identical run is recognised and reported instead of registered twice. A
        # *different* data hash under the same version would be a real conflict and still
        # raises, because that is the promise the version makes.
        existing = registry.find(name, resolved_version)
        if existing is not None and existing.data_hash == features.data_hash:
            registered.append(f"{existing.describe()} (already registered, unchanged)")
            if write_cards:
                cards.append(
                    _write_card(
                        name=name,
                        version=resolved_version,
                        algorithm=algorithm,
                        purpose=purpose,
                        metrics=metrics,
                        params=params,
                        features=features,
                        limitations=limitations,
                        beats_baseline=beats,
                        root=root,
                    ),
                )
            continue

        entry = registry.register(
            name=name,
            version=resolved_version,
            algorithm=algorithm,
            metrics=metrics,
            params=params,
            feature_list=features.feature_columns,
            data_hash=features.data_hash,
            beats_baseline=beats,
            model=model,
            # Only a model that beat its baseline goes live. A failed run is still
            # recorded, with its card, so the failure is visible rather than lost.
            activate=activate and bool(beats),
        )
        registered.append(entry.describe())
        if write_cards:
            cards.append(
                _write_card(
                    name=name,
                    version=resolved_version,
                    algorithm=algorithm,
                    purpose=purpose,
                    metrics=metrics,
                    params=params,
                    features=features,
                    limitations=limitations,
                    beats_baseline=beats,
                    root=root,
                ),
            )

    passed = bool(forecast.beats_baseline) and anomaly_passed
    started = the_clock.now()
    SqlEvalRunSink(engine=engine, clock=the_clock).record(
        EvalRunRecord(
            suite="ml",
            started_at=started,
            finished_at=the_clock.now(),
            metrics={
                "forecast": forecast.flat_metrics(),
                "anomaly": anomaly_metrics,
                "data_hash": features.data_hash,
            },
            thresholds={
                "forecast_min_improvement_pct": MIN_IMPROVEMENT_PCT,
                "anomaly_min_recall": ANOMALY_MIN_RECALL,
                "anomaly_max_flag_rate": ANOMALY_MAX_FLAG_RATE,
            },
            passed=passed,
            llm_mode="none",
            notes=f"version={resolved_version} cells={len(features.cell_ids)}",
        ),
    )

    return TrainingReport(
        window_start=features.window.start,
        window_end=features.window.end,
        rows=features.rows,
        cells=len(features.cell_ids),
        data_hash=features.data_hash,
        forecast=forecast,
        anomaly_metrics=anomaly_metrics,
        registered=tuple(registered),
        cards=tuple(cards),
        passed=passed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the Phase 3 models.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--cells", type=int, default=DEFAULT_CELL_LIMIT)
    parser.add_argument("--region", default=None)
    parser.add_argument("--version", default=None, help="override the derived version")
    parser.add_argument("--no-shap", action="store_true", help="skip SHAP (faster)")
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(
        level=settings.LOG_LEVEL,
        service="tazama-train",
        mode=settings.APP_MODE,
    )

    report = run(
        settings=settings,
        days=args.days,
        cell_limit=args.cells,
        region=args.region,
        version=args.version,
        with_shap=not args.no_shap,
        activate=not args.no_activate,
    )
    print(report.render())  # noqa: T201 - this is a CLI

    for scaffold in scaffold_status(settings):
        state = "enabled" if scaffold["enabled"] else "disabled"
        print(f"  scaffold    {scaffold['name']}: {state}")  # noqa: T201

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
