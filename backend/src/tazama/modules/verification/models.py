"""DTOs for the verification module.

Closure has to be earned. Three independent checks must pass, and each one carries its own
evidence so a reopened case can be explained without re-running anything:

  * **KPI recovery** — the breached KPIs are back within `kpi_recovery_ratio` of a baseline
    computed from *before* the incident window. A baseline measured after the event is the
    degraded value, and it would make every incident look resolved.
  * **Alarms cleared** — nothing still raised on the affected elements.
  * **Quiet period** — no new complaints on those elements for `quiet_period_minutes`. A
    fix that stops the alarm but not the complaints is not a fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class CheckName(StrEnum):
    KPI_RECOVERY = "kpi_recovery"
    ALARMS_CLEARED = "alarms_cleared"
    QUIET_PERIOD = "quiet_period"


@dataclass(frozen=True, slots=True)
class KpiRecovery:
    """One KPI's recovery, polarity-aware.

    `ratio` is always "how much of the baseline has come back", in the range 0..1+, whether
    the KPI is one where higher is better (throughput) or worse (drop rate). Expressing both
    directions as one number is what lets the policy carry a single threshold.
    """

    element_id: str
    kpi: str
    baseline: float
    current: float
    ratio: float
    recovered: bool
    lower_is_worse: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "kpi": self.kpi,
            "baseline": round(self.baseline, 4),
            "current": round(self.current, 4),
            "ratio": round(self.ratio, 4),
            "recovered": self.recovered,
            "lower_is_worse": self.lower_is_worse,
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: CheckName
    passed: bool
    detail: str
    #: Skipped checks (policy switched off) pass but say so, rather than silently counting.
    applied: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": str(self.name),
            "passed": self.passed,
            "applied": self.applied,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class IncidentView:
    """The incident facts verification needs, read through `IncidentReader`."""

    id: str
    status: str
    region: str
    detected_at: datetime
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    affected_elements: tuple[str, ...] = ()
    breached_kpis: tuple[str, ...] = ()
    cause: str | None = None

    @property
    def window_start(self) -> datetime:
        """When the incident began affecting the network, as best we know."""
        return self.started_at or self.detected_at


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The whole verdict. `passed` is the conjunction — there is no partial closure."""

    incident_id: str
    checked_at: datetime
    passed: bool
    checks: tuple[CheckResult, ...]
    kpi_details: tuple[KpiRecovery, ...] = ()
    complaints_after: int = 0
    open_alarms: tuple[str, ...] = ()
    reason: str = ""
    reopened: bool = False

    def check(self, name: CheckName) -> CheckResult | None:
        return next((c for c in self.checks if c.name is name), None)

    @property
    def kpi_recovered(self) -> bool:
        result = self.check(CheckName.KPI_RECOVERY)
        return bool(result and result.passed)

    @property
    def alarms_cleared(self) -> bool:
        result = self.check(CheckName.ALARMS_CLEARED)
        return bool(result and result.passed)

    @property
    def quiet_period_ok(self) -> bool:
        result = self.check(CheckName.QUIET_PERIOD)
        return bool(result and result.passed)

    @property
    def failed_checks(self) -> tuple[CheckName, ...]:
        return tuple(c.name for c in self.checks if not c.passed)

    def to_row(self) -> dict[str, Any]:
        """Flat shape the `verification` table stores."""
        return {
            "incident_id": self.incident_id,
            "checked_at": self.checked_at,
            "kpi_recovered": self.kpi_recovered,
            "alarms_cleared": self.alarms_cleared,
            "quiet_period_ok": self.quiet_period_ok,
            "complaints_after": self.complaints_after,
            "passed": self.passed,
            "detail": {
                "reason": self.reason,
                "reopened": self.reopened,
                "checks": [c.to_dict() for c in self.checks],
                "kpis": [k.to_dict() for k in self.kpi_details],
                "open_alarms": list(self.open_alarms),
            },
        }
