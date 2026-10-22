"""Loader for `config/policies.yaml` (PRD section 12: human-in-the-loop policy).

Every threshold a reviewer might argue about lives in that file, not in code, and every
decision records the policy version it used — so an audit row can be re-read years later
and still mean something.

The loader is strict on structure and forgiving on absence: a missing optional section
falls back to the documented default rather than crashing a worker at 03:00, but an
unparseable file or an out-of-range threshold fails fast at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from tazama.core.errors import ValidationFailed
from tazama.core.paths import config_dir

POLICY_FILENAME = "policies.yaml"


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    action: str
    required: bool
    roles: tuple[str, ...]
    auto_approve: bool
    reason: str = ""
    two_approver_risk_tiers: tuple[str, ...] = ()

    def requires_two_approvers(self, risk_tier: str) -> bool:
        return risk_tier in self.two_approver_risk_tiers


@dataclass(frozen=True, slots=True)
class ConfidenceThresholds:
    site_identification_min: float = 0.60
    diagnosis_min: float = 0.55
    classification_min: float = 0.50
    notification_min: float = 0.70


@dataclass(frozen=True, slots=True)
class CorrelationPolicy:
    spatial_radius_km: float = 3.0
    time_window_minutes: int = 45
    min_complaints_for_incident: int = 3
    min_complaints_per_1k_subs: float = 0.8
    topology_shared_upstream: bool = True
    alarm_storm_collapse_minutes: int = 10
    flapping_min_transitions: int = 4
    maintenance_suppression: bool = True
    silent_sufferer_kpi_threshold: float = 0.75


@dataclass(frozen=True, slots=True)
class PrioritisationPolicy:
    weights: dict[str, float] = field(default_factory=dict)
    p1_threshold: float = 0.80
    p2_threshold: float = 0.60
    p3_threshold: float = 0.35


@dataclass(frozen=True, slots=True)
class QuietHours:
    start: time
    end: time
    tz: str = "Africa/Nairobi"

    def covers(self, moment: time) -> bool:
        """Quiet hours normally wrap midnight, so the comparison has two shapes."""
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    quiet_hours: QuietHours
    max_per_subscriber_per_day: int = 2
    max_per_incident: int = 1
    dedupe_window_hours: int = 12
    respect_opt_out: bool = True
    templates_only: bool = True
    max_per_minute: int = 200
    storm_breach_action: str = "pause_and_alert"


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    quiet_period_minutes: int = 30
    kpi_recovery_ratio: float = 0.95
    require_alarm_clear: bool = True
    reopen_on_failure: bool = True


@dataclass(frozen=True, slots=True)
class AgentBudget:
    max_steps: int = 6
    max_tokens: int = 12_000
    max_wall_clock_s: int = 45
    tool_permission_tiers: dict[str, str] = field(default_factory=dict)

    def tier(self, tool: str) -> str:
        """Unknown tools are `never`: a tool must be granted, not merely not forbidden."""
        return self.tool_permission_tiers.get(tool, "never")


@dataclass(frozen=True, slots=True)
class AiPolicy:
    diagnosis_agent: AgentBudget = field(default_factory=AgentBudget)
    knowledge_require_citations: bool = True
    knowledge_refuse_without_evidence: bool = True
    knowledge_exclude_expired: bool = True
    knowledge_max_steps: int = 3
    reporter_numeric_grounding: bool = True
    rag_grader_min_score: float = 0.6
    rag_max_context_chunks: int = 8


@dataclass(frozen=True, slots=True)
class Policies:
    """The whole policy file, validated. `version` is recorded on every decision."""

    version: int
    updated: str
    owner: str
    approvals: dict[str, ApprovalRule]
    confidence: ConfidenceThresholds
    correlation: CorrelationPolicy
    prioritisation: PrioritisationPolicy
    notification: NotificationPolicy
    verification: VerificationPolicy
    ai: AiPolicy

    @property
    def rule_version(self) -> str:
        """Stamp stored alongside every rules-based decision."""
        return f"policies-v{self.version}"

    def approval_for(self, action: str) -> ApprovalRule:
        rule = self.approvals.get(action)
        if rule is None:
            # Fail closed: an action with no declared rule needs a human, not a default yes.
            return ApprovalRule(
                action=action,
                required=True,
                roles=("admin",),
                auto_approve=False,
                reason="no policy declared for this action; approval required by default",
            )
        return rule

    def priority_for(self, score: float) -> str:
        if score >= self.prioritisation.p1_threshold:
            return "P1"
        if score >= self.prioritisation.p2_threshold:
            return "P2"
        if score >= self.prioritisation.p3_threshold:
            return "P3"
        return "P4"


def _parse_time(value: Any, field_name: str) -> time:
    text = str(value)
    try:
        hour, _, minute = text.partition(":")
        return time(hour=int(hour), minute=int(minute or 0))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{field_name} must look like HH:MM, got {text!r}") from exc


def _ratio(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{field_name} must be a number, got {value!r}") from exc
    if not 0.0 <= number <= 1.0:
        raise ValidationFailed(f"{field_name} must be between 0 and 1, got {number}")
    return number


def load_policies(path: Path | str | None = None) -> Policies:
    """Read and validate the policy file."""
    file_path = Path(path) if path else config_dir() / POLICY_FILENAME
    if not file_path.exists():
        raise ValidationFailed(f"policy file not found: {file_path}")
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"{file_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed(f"{file_path} must contain a mapping at the top level")

    approvals: dict[str, ApprovalRule] = {}
    for action, spec in (raw.get("approvals") or {}).items():
        if not isinstance(spec, dict):
            raise ValidationFailed(f"approvals.{action} must be a mapping")
        approvals[str(action)] = ApprovalRule(
            action=str(action),
            required=bool(spec.get("required", True)),
            roles=tuple(str(r) for r in (spec.get("roles") or ())),
            auto_approve=bool(spec.get("auto_approve", False)),
            reason=str(spec.get("reason", "")),
            two_approver_risk_tiers=tuple(
                str(t) for t in (spec.get("risk_tiers_requiring_two_approvers") or ())
            ),
        )
    for action, rule in approvals.items():
        if rule.required and not rule.roles:
            raise ValidationFailed(
                f"approvals.{action} requires approval but names no roles that may give it",
            )
        if rule.auto_approve and rule.required:
            raise ValidationFailed(
                f"approvals.{action} cannot be both required and auto-approved",
            )

    confidence_raw = raw.get("confidence") or {}
    confidence = ConfidenceThresholds(
        site_identification_min=_ratio(
            confidence_raw.get("site_identification_min"),
            "confidence.site_identification_min",
            0.60,
        ),
        diagnosis_min=_ratio(confidence_raw.get("diagnosis_min"), "confidence.diagnosis_min", 0.55),
        classification_min=_ratio(
            confidence_raw.get("classification_min"),
            "confidence.classification_min",
            0.50,
        ),
        notification_min=_ratio(
            confidence_raw.get("notification_min"),
            "confidence.notification_min",
            0.70,
        ),
    )

    correlation_raw = raw.get("correlation") or {}
    correlation = CorrelationPolicy(
        spatial_radius_km=float(correlation_raw.get("spatial_radius_km", 3.0)),
        time_window_minutes=int(correlation_raw.get("time_window_minutes", 45)),
        min_complaints_for_incident=int(correlation_raw.get("min_complaints_for_incident", 3)),
        min_complaints_per_1k_subs=float(correlation_raw.get("min_complaints_per_1k_subs", 0.8)),
        topology_shared_upstream=bool(correlation_raw.get("topology_shared_upstream", True)),
        alarm_storm_collapse_minutes=int(correlation_raw.get("alarm_storm_collapse_minutes", 10)),
        flapping_min_transitions=int(correlation_raw.get("flapping_min_transitions", 4)),
        maintenance_suppression=bool(correlation_raw.get("maintenance_suppression", True)),
        silent_sufferer_kpi_threshold=_ratio(
            correlation_raw.get("silent_sufferer_kpi_threshold"),
            "correlation.silent_sufferer_kpi_threshold",
            0.75,
        ),
    )
    if correlation.spatial_radius_km <= 0 or correlation.time_window_minutes <= 0:
        raise ValidationFailed("correlation radius and window must be positive")

    prioritisation_raw = raw.get("prioritisation") or {}
    weights = {str(k): float(v) for k, v in (prioritisation_raw.get("weights") or {}).items()}
    total = sum(weights.values())
    if weights and abs(total - 1.0) > 0.01:
        raise ValidationFailed(
            f"prioritisation.weights must sum to 1.0, got {total:.3f} — "
            "otherwise the score is not comparable across incidents",
        )
    prioritisation = PrioritisationPolicy(
        weights=weights,
        p1_threshold=_ratio(prioritisation_raw.get("p1_threshold"), "p1_threshold", 0.80),
        p2_threshold=_ratio(prioritisation_raw.get("p2_threshold"), "p2_threshold", 0.60),
        p3_threshold=_ratio(prioritisation_raw.get("p3_threshold"), "p3_threshold", 0.35),
    )
    if not prioritisation.p1_threshold > prioritisation.p2_threshold > prioritisation.p3_threshold:
        raise ValidationFailed("priority thresholds must decrease from P1 to P3")

    notification_raw = raw.get("notification") or {}
    quiet_raw = notification_raw.get("quiet_hours") or {}
    notification = NotificationPolicy(
        quiet_hours=QuietHours(
            start=_parse_time(quiet_raw.get("start", "21:00"), "quiet_hours.start"),
            end=_parse_time(quiet_raw.get("end", "06:30"), "quiet_hours.end"),
            tz=str(quiet_raw.get("tz", "Africa/Nairobi")),
        ),
        max_per_subscriber_per_day=int(notification_raw.get("max_per_subscriber_per_day", 2)),
        max_per_incident=int(notification_raw.get("max_per_incident", 1)),
        dedupe_window_hours=int(notification_raw.get("dedupe_window_hours", 12)),
        respect_opt_out=bool(notification_raw.get("respect_opt_out", True)),
        templates_only=bool(notification_raw.get("templates_only", True)),
        max_per_minute=int(
            (notification_raw.get("storm_guard") or {}).get("max_notifications_per_minute", 200)
        ),
        storm_breach_action=str(
            (notification_raw.get("storm_guard") or {}).get("breach_action", "pause_and_alert")
        ),
    )
    if not notification.templates_only:
        raise ValidationFailed(
            "notification.templates_only=false is refused: the LLM fills slots in approved "
            "templates and never writes free text to a customer",
        )
    if not notification.respect_opt_out:
        raise ValidationFailed("notification.respect_opt_out=false is refused")

    verification_raw = raw.get("verification") or {}
    verification = VerificationPolicy(
        quiet_period_minutes=int(verification_raw.get("quiet_period_minutes", 30)),
        kpi_recovery_ratio=_ratio(
            verification_raw.get("kpi_recovery_ratio"),
            "verification.kpi_recovery_ratio",
            0.95,
        ),
        require_alarm_clear=bool(verification_raw.get("require_alarm_clear", True)),
        reopen_on_failure=bool(verification_raw.get("reopen_on_failure", True)),
    )

    ai_raw = raw.get("ai") or {}
    diagnosis_raw = ai_raw.get("diagnosis_agent") or {}
    knowledge_raw = ai_raw.get("knowledge_assistant") or {}
    reporter_raw = ai_raw.get("reporter") or {}
    rag_raw = ai_raw.get("rag") or {}
    tiers = {str(k): str(v) for k, v in (diagnosis_raw.get("tool_permission_tiers") or {}).items()}
    for tool, tier in tiers.items():
        if tier not in ("read", "propose", "never"):
            raise ValidationFailed(
                f"ai.diagnosis_agent.tool_permission_tiers.{tool} must be read, propose or never",
            )
    ai = AiPolicy(
        diagnosis_agent=AgentBudget(
            max_steps=int(diagnosis_raw.get("max_steps", 6)),
            max_tokens=int(diagnosis_raw.get("max_tokens", 12_000)),
            max_wall_clock_s=int(diagnosis_raw.get("max_wall_clock_s", 45)),
            tool_permission_tiers=tiers,
        ),
        knowledge_require_citations=bool(knowledge_raw.get("require_citations", True)),
        knowledge_refuse_without_evidence=bool(knowledge_raw.get("refuse_without_evidence", True)),
        knowledge_exclude_expired=bool(knowledge_raw.get("exclude_expired_docs", True)),
        knowledge_max_steps=int(knowledge_raw.get("max_steps", 3)),
        reporter_numeric_grounding=bool(reporter_raw.get("numeric_grounding_required", True)),
        rag_grader_min_score=_ratio(
            rag_raw.get("grader_min_score"), "ai.rag.grader_min_score", 0.6
        ),
        rag_max_context_chunks=int(rag_raw.get("max_context_chunks", 8)),
    )
    if not ai.reporter_numeric_grounding:
        raise ValidationFailed(
            "ai.reporter.numeric_grounding_required=false is refused: every number in a "
            "generated report must exist in the source data",
        )

    return Policies(
        version=int(raw.get("version", 0)),
        updated=str(raw.get("updated", "")),
        owner=str(raw.get("owner", "")),
        approvals=approvals,
        confidence=confidence,
        correlation=correlation,
        prioritisation=prioritisation,
        notification=notification,
        verification=verification,
        ai=ai,
    )


@lru_cache(maxsize=1)
def get_policies() -> Policies:
    """Process-wide policies, loaded once."""
    return load_policies()


def reset_policy_cache() -> None:
    get_policies.cache_clear()
