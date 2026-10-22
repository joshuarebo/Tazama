"""The policy file is a control surface, so the loader is strict about it.

A silently-accepted bad policy is worse than a crash: it changes behaviour nobody asked
for. These tests pin down what is refused and why.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
import yaml

from tazama.core.errors import ValidationFailed
from tazama.core.policy import QuietHours, load_policies

MINIMAL: dict[str, object] = {
    "version": 3,
    "updated": "2026-10-21",
    "owner": "platform",
    "approvals": {
        "notification_send": {
            "required": True,
            "roles": ["manager", "admin"],
            "auto_approve": False,
        },
    },
    "confidence": {"site_identification_min": 0.6},
    "correlation": {"spatial_radius_km": 3.0, "time_window_minutes": 45},
    "prioritisation": {
        "weights": {"affected_subscribers": 0.5, "duration_minutes": 0.5},
        "p1_threshold": 0.8,
        "p2_threshold": 0.6,
        "p3_threshold": 0.35,
    },
    "notification": {"quiet_hours": {"start": "21:00", "end": "06:30"}},
    "verification": {"quiet_period_minutes": 30},
    "ai": {"diagnosis_agent": {"max_steps": 6, "tool_permission_tiers": {"kpi_query": "read"}}},
}


def _write(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "policies.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _mutate(**changes: object) -> dict[str, object]:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    for dotted, value in changes.items():
        target = document
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[part]  # type: ignore[index]
        target[parts[-1]] = value  # type: ignore[index]
    return document


# -------------------------------------------------------- the real policy


def test_committed_policy_file_loads(repo_root: Path) -> None:
    policies = load_policies(repo_root / "config" / "policies.yaml")
    assert policies.version >= 1
    assert policies.rule_version == f"policies-v{policies.version}"
    assert policies.owner


def test_committed_policy_keeps_humans_in_the_loop(repo_root: Path) -> None:
    """PRD section 12: these four actions may never be automatic."""
    policies = load_policies(repo_root / "config" / "policies.yaml")
    for action in (
        "notification_send",
        "recovery_execute",
        "incident_close",
        "report_send_now",
    ):
        rule = policies.approval_for(action)
        assert rule.required is True, action
        assert rule.auto_approve is False, action
        assert rule.roles, action


def test_committed_policy_forbids_dangerous_tools(repo_root: Path) -> None:
    budget = load_policies(repo_root / "config" / "policies.yaml").ai.diagnosis_agent
    for tool in ("runbook_execute", "send_message", "network_command"):
        assert budget.tier(tool) == "never", tool
    for tool in ("kpi_query", "alarm_query", "topology_lookup"):
        assert budget.tier(tool) == "read", tool


def test_unknown_tool_defaults_to_never(repo_root: Path) -> None:
    """A tool must be granted, not merely not forbidden."""
    budget = load_policies(repo_root / "config" / "policies.yaml").ai.diagnosis_agent
    assert budget.tier("delete_everything") == "never"


# -------------------------------------------------------------- structure


def test_minimal_policy_loads(tmp_path: Path) -> None:
    policies = load_policies(_write(tmp_path, MINIMAL))
    assert policies.version == 3
    assert policies.correlation.min_complaints_for_incident == 3  # documented default


def test_missing_file_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="not found"):
        load_policies("nope/policies.yaml")


def test_invalid_yaml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "policies.yaml"
    path.write_text("approvals: [unclosed", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="valid YAML"):
        load_policies(path)


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    document = _mutate(**{"prioritisation.weights": {"affected_subscribers": 0.5}})
    with pytest.raises(ValidationFailed, match=r"sum to 1\.0"):
        load_policies(_write(tmp_path, document))


def test_priority_thresholds_must_decrease(tmp_path: Path) -> None:
    document = _mutate(**{"prioritisation.p2_threshold": 0.9})
    with pytest.raises(ValidationFailed, match="decrease"):
        load_policies(_write(tmp_path, document))


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_confidence_must_be_a_ratio(tmp_path: Path, value: float) -> None:
    document = _mutate(**{"confidence.site_identification_min": value})
    with pytest.raises(ValidationFailed, match=r"between 0 and 1"):
        load_policies(_write(tmp_path, document))


def test_correlation_window_must_be_positive(tmp_path: Path) -> None:
    document = _mutate(**{"correlation.time_window_minutes": 0})
    with pytest.raises(ValidationFailed, match="positive"):
        load_policies(_write(tmp_path, document))


def test_approval_requiring_nobody_is_refused(tmp_path: Path) -> None:
    document = _mutate(**{"approvals.notification_send.roles": []})
    with pytest.raises(ValidationFailed, match="names no roles"):
        load_policies(_write(tmp_path, document))


def test_approval_cannot_be_required_and_automatic(tmp_path: Path) -> None:
    document = _mutate(**{"approvals.notification_send.auto_approve": True})
    with pytest.raises(ValidationFailed, match="both required and auto-approved"):
        load_policies(_write(tmp_path, document))


def test_free_text_customer_messages_cannot_be_enabled(tmp_path: Path) -> None:
    document = _mutate(**{"notification.templates_only": False})
    with pytest.raises(ValidationFailed, match="templates_only"):
        load_policies(_write(tmp_path, document))


def test_opt_out_cannot_be_disabled(tmp_path: Path) -> None:
    document = _mutate(**{"notification.respect_opt_out": False})
    with pytest.raises(ValidationFailed, match="respect_opt_out"):
        load_policies(_write(tmp_path, document))


def test_numeric_grounding_cannot_be_disabled(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["ai"]["reporter"] = {"numeric_grounding_required": False}  # type: ignore[index]
    with pytest.raises(ValidationFailed, match="numeric_grounding"):
        load_policies(_write(tmp_path, document))


def test_unknown_permission_tier_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["ai"]["diagnosis_agent"]["tool_permission_tiers"]["kpi_query"] = "write"  # type: ignore[index]
    with pytest.raises(ValidationFailed, match="read, propose or never"):
        load_policies(_write(tmp_path, document))


def test_bad_quiet_hours_are_refused(tmp_path: Path) -> None:
    document = _mutate(**{"notification.quiet_hours": {"start": "late", "end": "06:30"}})
    with pytest.raises(ValidationFailed, match="HH:MM"):
        load_policies(_write(tmp_path, document))


def test_undeclared_action_requires_approval(tmp_path: Path) -> None:
    """Fail closed: an action nobody wrote a rule for needs a human."""
    rule = load_policies(_write(tmp_path, MINIMAL)).approval_for("launch_missiles")
    assert rule.required is True
    assert rule.auto_approve is False
    assert rule.roles == ("admin",)


def test_two_approver_tiers_are_parsed(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["approvals"]["recovery_execute"] = {  # type: ignore[index]
        "required": True,
        "roles": ["manager", "admin"],
        "risk_tiers_requiring_two_approvers": ["high"],
    }
    rule = load_policies(_write(tmp_path, document)).approval_for("recovery_execute")
    assert rule.requires_two_approvers("high")
    assert not rule.requires_two_approvers("low")


# ------------------------------------------------------------ quiet hours


@pytest.mark.parametrize(
    ("moment", "quiet"),
    [
        (time(20, 59), False),
        (time(21, 0), True),
        (time(23, 59), True),
        (time(0, 30), True),
        (time(6, 29), True),
        (time(6, 30), False),
        (time(12, 0), False),
    ],
)
def test_quiet_hours_wrap_midnight(moment: time, quiet: bool) -> None:
    window = QuietHours(start=time(21, 0), end=time(6, 30))
    assert window.covers(moment) is quiet


def test_quiet_hours_within_one_day() -> None:
    """A non-wrapping window must also work, in case the policy changes."""
    window = QuietHours(start=time(1, 0), end=time(5, 0))
    assert window.covers(time(2, 0)) is True
    assert window.covers(time(23, 0)) is False


def test_priority_bands(repo_root: Path) -> None:
    policies = load_policies(repo_root / "config" / "policies.yaml")
    assert policies.priority_for(0.95) == "P1"
    assert policies.priority_for(0.70) == "P2"
    assert policies.priority_for(0.40) == "P3"
    assert policies.priority_for(0.05) == "P4"
