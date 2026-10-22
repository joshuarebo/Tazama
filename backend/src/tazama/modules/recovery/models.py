"""DTOs and the runbook catalogue loader for the recovery module.

The defining property of this module is what it *cannot* do: nothing here, and no port in
`ports.py`, runs a command against the network. A recovery is a proposal with an approval
trail and a **simulated** outcome. The policy file's `never` tier (`runbook_execute`,
`network_command`, `send_message`) is the same rule stated for the agent layer.

The catalogue itself is a reviewed YAML file (`config/runbooks.yaml`) loaded with
`yaml.safe_load`, in the same spirit as `config/policies.yaml`: the steps an engineer will
follow are a document, not code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from tazama.core.errors import ValidationFailed
from tazama.core.paths import config_dir
from tazama.domain.value_objects import IncidentCause

RUNBOOK_FILENAME = "runbooks.yaml"

#: Tool names the policy pins to the `never` tier. Mirrored here so a test in this module
#: can assert the module honours them without reaching into another module.
NEVER_TIER_TOOLS: Final[tuple[str, ...]] = ("runbook_execute", "network_command", "send_message")


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecoveryStatus(StrEnum):
    PROPOSED = "proposed"
    #: High-risk runbooks need two approvers; the first one lands here.
    AWAITING_SECOND_APPROVAL = "awaiting_second_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Simulated, and that is as far as v1 goes. There is no `executed`.
    SIMULATED = "simulated"


@dataclass(frozen=True, slots=True)
class RunbookStep:
    """One ordered step. `tier` is the permission tier an automated caller would need."""

    n: int
    action: str
    tier: str = "propose"


@dataclass(frozen=True, slots=True)
class KpiExpectation:
    """What the runbook is expected to do to a KPI, as a modelled claim."""

    kpi: str
    direction: str  # improve | reduce
    target: float | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class Runbook:
    id: str
    title: str
    cause: str
    risk_tier: RiskTier
    steps: tuple[RunbookStep, ...]
    rollback: tuple[RunbookStep, ...]
    expected_effect: str
    requires_approval: bool
    owner: str
    estimated_minutes: int = 30
    expected_kpis: tuple[KpiExpectation, ...] = ()

    @property
    def is_high_risk(self) -> bool:
        return self.risk_tier is RiskTier.HIGH


@dataclass(frozen=True, slots=True)
class SimulatedOutcome:
    """A modelled result. Stored on `recovery_action.simulated_outcome`.

    `simulation` and `executed` are written on every record, so a row read years later
    cannot be mistaken for evidence that something was done to the network.
    """

    runbook_id: str
    expected_kpis: tuple[KpiExpectation, ...]
    affected_elements: tuple[str, ...]
    estimated_minutes: int
    rollback_plan: tuple[RunbookStep, ...]
    expected_effect: str
    simulated_at: datetime
    confidence: float = 0.5
    caveats: tuple[str, ...] = ()
    simulation: bool = True
    executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulation": True,
            "executed": False,
            "disclaimer": (
                "SIMULATION ONLY — modelled from the runbook's declared expected effect. "
                "Nothing was executed against the network."
            ),
            "runbook_id": self.runbook_id,
            "simulated_at": self.simulated_at.isoformat(),
            "estimated_minutes": self.estimated_minutes,
            "expected_effect": self.expected_effect,
            "confidence": self.confidence,
            "expected_kpis": [
                {
                    "kpi": k.kpi,
                    "direction": k.direction,
                    "target": k.target,
                    "note": k.note,
                }
                for k in self.expected_kpis
            ],
            "affected_elements": list(self.affected_elements),
            "rollback_plan": [{"n": s.n, "action": s.action} for s in self.rollback_plan],
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True, slots=True)
class IncidentBrief:
    """The incident facts a recovery needs. Passed in; never fetched from another module."""

    id: str
    region: str
    status: str
    cause: str | None = None
    affected_elements: tuple[str, ...] = ()
    affected_subs: int = 0
    priority: str = "P3"


@dataclass(frozen=True, slots=True)
class RecoveryProposal:
    """A proposed (never executed) recovery and its approval trail."""

    id: str
    incident_id: str
    runbook_id: str
    status: RecoveryStatus
    risk_tier: RiskTier
    proposed_by: str
    proposed_at: datetime
    rationale: str = ""
    requires_approval: bool = True
    required_approvals: int = 1
    approvals: tuple[str, ...] = ()
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    rollback_of: str | None = None
    simulated_outcome: Mapping[str, Any] | None = None
    affected_elements: tuple[str, ...] = ()

    @property
    def approved_by(self) -> str | None:
        """Last approver, which is what the `recovery_action` row stores."""
        return self.approvals[-1] if self.approvals else None

    @property
    def is_rollback(self) -> bool:
        return self.rollback_of is not None

    def with_approval(self, *, approver: str, at: datetime) -> RecoveryProposal:
        approvals = (*self.approvals, approver)
        complete = len(approvals) >= self.required_approvals
        return replace(
            self,
            approvals=approvals,
            status=RecoveryStatus.APPROVED if complete else RecoveryStatus.AWAITING_SECOND_APPROVAL,
            approved_at=at if complete else self.approved_at,
        )

    def rejected(self, *, reason: str) -> RecoveryProposal:
        return replace(self, status=RecoveryStatus.REJECTED, rejected_reason=reason)

    def with_simulation(self, outcome: SimulatedOutcome) -> RecoveryProposal:
        return replace(self, simulated_outcome=outcome.to_dict())

    def to_row(self) -> dict[str, Any]:
        """Flat shape the `recovery_action` table stores."""
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "runbook_id": self.runbook_id,
            "status": str(self.status),
            "risk_tier": str(self.risk_tier),
            "simulated_outcome": dict(self.simulated_outcome) if self.simulated_outcome else None,
            "proposed_by": self.proposed_by,
            "proposed_at": self.proposed_at,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "rejected_reason": self.rejected_reason,
            "rollback_of": self.rollback_of,
        }


# --------------------------------------------------------- catalogue loading


def runbook_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else config_dir() / RUNBOOK_FILENAME


def load_runbooks(path: Path | str | None = None) -> dict[str, Runbook]:
    """Read and validate `config/runbooks.yaml`."""
    file_path = runbook_path(path)
    if not file_path.exists():
        raise ValidationFailed(f"runbook catalogue not found: {file_path}")
    try:
        raw: Any = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"{file_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed(f"{file_path} must contain a mapping at the top level")
    entries = raw.get("runbooks")
    if not isinstance(entries, list) or not entries:
        raise ValidationFailed(f"{file_path}: `runbooks` must be a non-empty list")

    catalogue: dict[str, Runbook] = {}
    valid_causes = {str(cause) for cause in IncidentCause}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValidationFailed(f"runbooks[{index}] must be a mapping")
        runbook = _build_runbook(entry, index=index, valid_causes=valid_causes)
        if runbook.id in catalogue:
            raise ValidationFailed(f"duplicate runbook id {runbook.id!r}")
        catalogue[runbook.id] = runbook
    return catalogue


def _build_runbook(entry: Mapping[str, Any], *, index: int, valid_causes: set[str]) -> Runbook:
    runbook_id = str(entry.get("id", "")).strip()
    if not runbook_id:
        raise ValidationFailed(f"runbooks[{index}] has no id")

    cause = str(entry.get("cause", "")).strip()
    if cause not in valid_causes:
        raise ValidationFailed(
            f"runbook {runbook_id}: cause {cause!r} is not an IncidentCause "
            f"({', '.join(sorted(valid_causes))})",
        )

    try:
        risk_tier = RiskTier(str(entry.get("risk_tier", "")).strip())
    except ValueError as exc:
        raise ValidationFailed(
            f"runbook {runbook_id}: risk_tier must be low, medium or high",
        ) from exc

    steps = _steps(entry.get("steps"), runbook_id=runbook_id, label="steps")
    if not steps:
        raise ValidationFailed(f"runbook {runbook_id}: steps must not be empty")
    rollback = _steps(entry.get("rollback"), runbook_id=runbook_id, label="rollback")
    if not rollback:
        raise ValidationFailed(
            f"runbook {runbook_id}: rollback must not be empty — a change with no way back "
            f"is not a runbook",
        )

    expected_effect = str(entry.get("expected_effect", "")).strip()
    if not expected_effect:
        raise ValidationFailed(
            f"runbook {runbook_id}: expected_effect is required — verification checks it",
        )

    requires_approval = bool(entry.get("requires_approval", True))
    if risk_tier is RiskTier.HIGH and not requires_approval:
        raise ValidationFailed(
            f"runbook {runbook_id}: a high-risk runbook always requires approval",
        )

    owner = str(entry.get("owner", "")).strip()
    if not owner:
        raise ValidationFailed(f"runbook {runbook_id}: owner is required")

    expected_kpis = tuple(
        KpiExpectation(
            kpi=str(item.get("kpi", "")),
            direction=str(item.get("direction", "improve")),
            target=None if item.get("target") is None else float(item["target"]),
            note=str(item.get("note", "")),
        )
        for item in (entry.get("expected_kpis") or ())
        if isinstance(item, dict)
    )
    for expectation in expected_kpis:
        if expectation.direction not in ("improve", "reduce"):
            raise ValidationFailed(
                f"runbook {runbook_id}: expected_kpis direction must be improve or reduce",
            )

    return Runbook(
        id=runbook_id,
        title=str(entry.get("title", "")).strip() or runbook_id,
        cause=cause,
        risk_tier=risk_tier,
        steps=steps,
        rollback=rollback,
        expected_effect=expected_effect,
        requires_approval=requires_approval,
        owner=owner,
        estimated_minutes=int(entry.get("estimated_minutes", 30)),
        expected_kpis=expected_kpis,
    )


def _steps(raw: Any, *, runbook_id: str, label: str) -> tuple[RunbookStep, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValidationFailed(f"runbook {runbook_id}: {label} must be a list")
    steps: list[RunbookStep] = []
    for position, item in enumerate(raw, start=1):
        if isinstance(item, str):
            steps.append(RunbookStep(n=position, action=item.strip()))
            continue
        if not isinstance(item, dict):
            raise ValidationFailed(
                f"runbook {runbook_id}: {label}[{position}] must be a string or a mapping",
            )
        action = str(item.get("action", "")).strip()
        if not action:
            raise ValidationFailed(f"runbook {runbook_id}: {label}[{position}] has no action")
        tier = str(item.get("tier", "propose"))
        if tier not in ("read", "propose"):
            # `never` steps would be unperformable, and there is no `execute` tier at all.
            raise ValidationFailed(
                f"runbook {runbook_id}: {label}[{position}] tier must be read or propose",
            )
        steps.append(RunbookStep(n=int(item.get("n", position)), action=action, tier=tier))
    return tuple(steps)


class FileRunbookRepository:
    """`RunbookRepository` backed by the YAML catalogue. Loaded once, then read-only."""

    __slots__ = ("_runbooks",)

    def __init__(self, path: Path | str | None = None) -> None:
        self._runbooks = load_runbooks(path)

    def get(self, runbook_id: str) -> Runbook | None:
        return self._runbooks.get(runbook_id)

    def for_cause(self, cause: str) -> tuple[Runbook, ...]:
        return tuple(r for r in self.all() if r.cause == cause)

    def all(self) -> tuple[Runbook, ...]:
        return tuple(self._runbooks[key] for key in sorted(self._runbooks))


@dataclass(frozen=True, slots=True)
class RecommendationInputs:
    """What the recommendation saw, recorded verbatim on the decision row."""

    incident_id: str
    cause: str
    diagnosis_hint: str | None
    candidates: tuple[str, ...]
    chosen: str
    reason: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "cause": self.cause,
            "diagnosis_hint": self.diagnosis_hint,
            "candidates": list(self.candidates),
            "chosen": self.chosen,
            "reason": self.reason,
            **dict(self.extra),
        }


def ordered_steps(steps: Sequence[RunbookStep]) -> tuple[RunbookStep, ...]:
    """Steps in their declared order. A runbook read out of order is a different runbook."""
    return tuple(sorted(steps, key=lambda step: step.n))
