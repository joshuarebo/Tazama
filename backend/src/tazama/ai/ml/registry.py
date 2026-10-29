"""The model registry: one row per trained model, one artifact per row, one active version.

PRD section 9 asks for "a simple file/DB model registry, model cards in docs/model-cards,
reproducible training scripts, drift checks". This is the registry half. An entry records
everything needed to answer the three questions that matter six months later:

  * *what is this?* - algorithm, params, feature list, model card;
  * *what was it trained on?* - `data_hash`, which digests the exact observation values
    and time window the frame was built from;
  * *is it allowed to be used?* - `beats_baseline` plus `is_active`, and the service
    refuses to activate a model whose verdict is false.

Security note on artifacts (read this before adding a loader anywhere else)
--------------------------------------------------------------------------
Artifacts are Python pickles. **Unpickling is arbitrary code execution.** That is
acceptable here for exactly one reason: we only ever unpickle a file this system wrote,
from the directory this system owns (`core.paths.artifacts_dir() / "models"`).
`load_artifact` resolves the path and refuses anything outside that directory, so a
registry row whose `artifact_path` has been tampered with cannot be used to run code. No
artifact path may ever come from user input, an HTTP request, an upload or another
operator's machine. If a model ever has to cross a trust boundary, it needs a format that
is data rather than code (ONNX, or the LightGBM text dump) - not a wider pickle loader.
"""

from __future__ import annotations

import pickle
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tazama.core.clock import Clock, SystemClock, ensure_utc
from tazama.core.errors import Conflict, NotFound, PolicyViolation
from tazama.core.logging import get_logger
from tazama.core.paths import artifacts_dir

_log = get_logger(__name__)

#: Subdirectory of `artifacts_dir()` that holds model artifacts. The only directory
#: `load_artifact` will read from.
MODELS_SUBDIR = "models"

ARTIFACT_SUFFIX = ".pkl"


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One registry row. Immutable: a new training run is a new version, never an edit."""

    name: str
    version: str
    algorithm: str
    trained_at: datetime
    metrics: Mapping[str, Any] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    feature_list: tuple[str, ...] = ()
    data_hash: str | None = None
    artifact_path: str | None = None
    card_path: str | None = None
    beats_baseline: bool | None = None
    is_active: bool = False

    @property
    def id(self) -> str:
        """Stable identity: the registry's unique key is (name, version)."""
        return f"{self.name}:{self.version}"

    def describe(self) -> str:
        verdict = {True: "beats baseline", False: "does NOT beat baseline", None: "no baseline"}[
            self.beats_baseline
        ]
        active = " [ACTIVE]" if self.is_active else ""
        return (
            f"{self.name} {self.version} ({self.algorithm}) {verdict}{active} "
            f"data_hash={(self.data_hash or '-')[:12]}"
        )


@dataclass(frozen=True, slots=True)
class EvalRunRecord:
    """One `eval_run` row: what was evaluated, against what thresholds, and the verdict."""

    suite: str
    started_at: datetime
    finished_at: datetime | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    passed: bool | None = None
    llm_mode: str = "none"
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """One `prediction` row - a stored forecast, kept so accuracy can be scored later."""

    model_name: str
    model_version: str
    entity_type: str
    entity_id: str
    target: str
    value: float
    created_at: datetime
    predicted_for: datetime | None = None
    horizon_minutes: int | None = None
    lower: float | None = None
    upper: float | None = None
    features: Mapping[str, Any] | None = None


@runtime_checkable
class ModelRegistryStore(Protocol):
    """Persistence for registry rows (the `model_registry` table).

    Declared here and implemented by `infrastructure.db.ml_repo`: `ai/ml` must not import
    infrastructure, so the adapter is injected by the training CLI's composition root.
    """

    def upsert(self, entry: ModelEntry) -> ModelEntry: ...

    def get(self, name: str, version: str) -> ModelEntry | None: ...

    def active(self, name: str) -> ModelEntry | None: ...

    def set_active(self, name: str, version: str) -> ModelEntry:
        """Activate one version and deactivate every other version of the same name.

        Must be atomic: "only one active version per name" is an invariant, not a hope.
        """
        ...

    def list_all(self, *, name: str | None = None) -> tuple[ModelEntry, ...]: ...


@runtime_checkable
class EvalRunSink(Protocol):
    """Append-only record of evaluation runs (the `eval_run` table)."""

    def record(self, run: EvalRunRecord) -> str: ...


@runtime_checkable
class PredictionSink(Protocol):
    """Writes stored forecasts (the `prediction` table)."""

    def write(self, predictions: Sequence[PredictionRecord]) -> int: ...


class ModelRegistry:
    """Registry service: database row plus on-disk artifact, kept consistent.

    Keyword-only construction, injected clock, injected store - the same conventions as
    every other service in the tree.
    """

    def __init__(
        self,
        *,
        store: ModelRegistryStore,
        clock: Clock | None = None,
        root: Path | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._root = (root or artifacts_dir() / MODELS_SUBDIR).resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ queries

    def get(self, name: str, version: str) -> ModelEntry:
        entry = self._store.get(name, version)
        if entry is None:
            raise NotFound(f"no registry entry for model {name!r} version {version!r}")
        return entry

    def find(self, name: str, version: str) -> ModelEntry | None:
        return self._store.get(name, version)

    def active(self, name: str) -> ModelEntry | None:
        """The one version of `name` that is live, if any."""
        return self._store.active(name)

    def list_models(self, *, name: str | None = None) -> tuple[ModelEntry, ...]:
        return self._store.list_all(name=name)

    # -------------------------------------------------------------- registration

    def register(
        self,
        *,
        name: str,
        version: str,
        algorithm: str,
        metrics: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        feature_list: Sequence[str] = (),
        data_hash: str | None = None,
        card_path: str | None = None,
        beats_baseline: bool | None = None,
        model: Any = None,
        trained_at: datetime | None = None,
        activate: bool = False,
    ) -> ModelEntry:
        """Record a trained model, optionally saving its artifact and activating it.

        Re-registering the same `(name, version)` is refused: a version is a promise about
        which data and which code produced the artifact, and silently overwriting it would
        break every `data_hash` already recorded against it.
        """
        if self._store.get(name, version) is not None:
            raise Conflict(
                f"model {name!r} version {version!r} is already registered; "
                f"a new training run needs a new version",
            )
        artifact_path: str | None = None
        if model is not None:
            artifact_path = str(self.save_artifact(model, name=name, version=version))
        entry = ModelEntry(
            name=name,
            version=version,
            algorithm=algorithm,
            trained_at=ensure_utc(trained_at or self._clock.now()),
            metrics=dict(metrics or {}),
            params=dict(params or {}),
            feature_list=tuple(feature_list),
            data_hash=data_hash,
            artifact_path=artifact_path,
            card_path=card_path,
            beats_baseline=beats_baseline,
            is_active=False,
        )
        stored = self._store.upsert(entry)
        _log.info(
            "model registered",
            extra={"model": stored.id, "beats_baseline": stored.beats_baseline},
        )
        if activate:
            return self.activate(name, version)
        return stored

    def activate(self, name: str, version: str) -> ModelEntry:
        """Make one version the active one, refusing anything that lost to the baseline.

        This is the hard edge of "classical first" (CLAUDE.md section 7): a model that does
        not beat the seasonal-naive baseline is kept for the record and never served.
        """
        entry = self.get(name, version)
        if entry.beats_baseline is False:
            raise PolicyViolation(
                f"refusing to activate {entry.id}: it does not beat the baseline "
                f"({entry.metrics.get('comparison', 'see metrics')}). Keep the baseline.",
                model=entry.id,
            )
        if entry.beats_baseline is None:
            raise PolicyViolation(
                f"refusing to activate {entry.id}: no baseline comparison was recorded, "
                f"so there is no evidence it is better than doing nothing",
                model=entry.id,
            )
        activated = self._store.set_active(name, version)
        _log.info("model activated", extra={"model": activated.id})
        return activated

    # ----------------------------------------------------------------- artifacts

    def artifact_path(self, *, name: str, version: str) -> Path:
        """Where an artifact for `(name, version)` lives. Both parts are sanitised.

        `name` and `version` come from our own code, but path construction from any string
        is the one place a traversal bug hides, so the components are validated rather
        than trusted.
        """
        for part, label in ((name, "name"), (version, "version")):
            if not part or any(c in part for c in "/\\:") or part in {".", ".."}:
                raise ValueError(f"unsafe model {label}: {part!r}")
        return self._root / name / f"{version}{ARTIFACT_SUFFIX}"

    def save_artifact(self, model: Any, *, name: str, version: str) -> Path:
        """Pickle a model we trained ourselves into the artifacts directory."""
        path = self.artifact_path(name=name, version=version)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)
        return path

    def load_artifact(self, *, name: str, version: str) -> Any:
        """Load an artifact, refusing any path outside our own artifacts directory.

        Unpickling runs code (see the module docstring). The containment check below is the
        control that makes it safe: a registry row cannot point the loader at
        `/tmp/evil.pkl`, and nothing else in the system is allowed to call `pickle.load`.
        """
        entry = self.get(name, version)
        if not entry.artifact_path:
            raise NotFound(f"model {entry.id} has no stored artifact")
        path = Path(entry.artifact_path).resolve()
        if not path.is_relative_to(self._root):
            raise PolicyViolation(
                f"refusing to load {entry.id}: artifact {path} is outside the trusted "
                f"artifacts directory {self._root}",
                model=entry.id,
            )
        if not path.exists():
            raise NotFound(f"artifact for {entry.id} is missing from disk: {path}")
        with path.open("rb") as handle:
            return pickle.load(handle)  # noqa: S301 - trusted path only; see the docstring

    # ------------------------------------------------------------------- helpers

    def with_metrics(self, entry: ModelEntry, metrics: Mapping[str, Any]) -> ModelEntry:
        """Replace an entry's metrics in place (used when evaluation follows training)."""
        return self._store.upsert(replace(entry, metrics=dict(metrics)))
