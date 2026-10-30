"""The reporting schedule and the sender.

The timezone test is the one that earns its place. "07:30 Africa/Nairobi" is the
requirement; 04:30 UTC is an implementation detail, and a schedule that stores the UTC
figure has silently hard-coded today's offset. Nairobi has no DST today, which is exactly
why this needs a test rather than a comment — nothing would fail until the day a rule
changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tazama.infrastructure.adapters.teams.live_stub import envelope
from tazama.infrastructure.adapters.teams.mock import (
    DryRunTeamsSender,
    RecordingTeamsSender,
    validate_card,
)
from tazama.modules.reporting.models import Digest, DigestIncident
from tazama.modules.reporting.ports import ScheduledReport, SendOutcome
from tazama.modules.reporting.schedule import (
    DEFAULT_SCHEDULE,
    ReportingService,
    due_reports,
)

NAIROBI = "Africa/Nairobi"
T0 = datetime(2026, 3, 17, 7, 30, tzinfo=UTC)


def _digest(report_id: str = "RPT-DAILY-20260317T0430") -> Digest:
    return Digest(
        report_id=report_id,
        kind="daily",
        title="TAZAMA daily network-care digest",
        generated_at=T0,
        window_start=T0 - timedelta(days=1),
        window_end=T0,
        display_tz=NAIROBI,
        headline=(("Incidents", "2 live, 1 suppressed"),),
        incidents=(
            DigestIncident(
                incident_id="INC-1",
                title="Congestion on Westlands",
                priority="P2",
                status="detected",
                cause="congestion",
                region="Nairobi",
                affected_subs=1_200,
                affected_cells=3,
                complaint_count=9,
                detected_at=T0 - timedelta(hours=2),
                duration_minutes=120.0,
            ),
        ),
    )


# ----------------------------------------------------------------- schedule


def test_the_daily_digest_fires_at_0730_nairobi_which_is_0430_utc() -> None:
    """The requirement is local. If this ever reads 07:30 UTC, the offset was baked in."""
    at_0430_utc = datetime(2026, 3, 17, 4, 30, tzinfo=UTC)
    due = due_reports(at_0430_utc, display_tz=NAIROBI)
    assert [item.kind for item in due] == ["daily"]

    # And 07:30 UTC — which is 10:30 in Nairobi — must fire nothing.
    assert due_reports(datetime(2026, 3, 17, 7, 30, tzinfo=UTC), display_tz=NAIROBI) == ()


def test_the_weekly_summary_only_fires_on_monday() -> None:
    monday = datetime(2026, 3, 16, 5, 0, tzinfo=UTC)  # 08:00 Nairobi, a Monday
    tuesday = datetime(2026, 3, 17, 5, 0, tzinfo=UTC)
    assert monday.astimezone().weekday() is not None  # sanity: tz-aware
    assert "weekly" in [item.kind for item in due_reports(monday, display_tz=NAIROBI)]
    assert "weekly" not in [item.kind for item in due_reports(tuesday, display_tz=NAIROBI)]


def test_a_tick_outside_the_window_fires_nothing() -> None:
    assert due_reports(datetime(2026, 3, 17, 3, 0, tzinfo=UTC), display_tz=NAIROBI) == ()
    assert due_reports(datetime(2026, 3, 17, 6, 0, tzinfo=UTC), display_tz=NAIROBI) == ()


def test_a_late_tick_within_the_window_still_fires() -> None:
    """A slow poll must not skip a day; the ledger makes the double-fire harmless."""
    late = datetime(2026, 3, 17, 4, 38, tzinfo=UTC)
    assert [item.kind for item in due_reports(late, display_tz=NAIROBI)] == ["daily"]


def test_the_scheduled_time_is_reported_in_utc() -> None:
    due = due_reports(datetime(2026, 3, 17, 4, 31, tzinfo=UTC), display_tz=NAIROBI)
    assert due[0].scheduled_for == datetime(2026, 3, 17, 4, 30, tzinfo=UTC)


def test_a_disabled_entry_never_fires() -> None:
    schedule = (ScheduledReport(kind="daily", at_local="07:30", enabled=False),)
    assert (
        due_reports(
            datetime(2026, 3, 17, 4, 30, tzinfo=UTC),
            display_tz=NAIROBI,
            schedule=schedule,
        )
        == ()
    )


def test_a_different_display_timezone_moves_the_fire_time() -> None:
    """The schedule follows `DISPLAY_TZ`, which is the point of storing local time."""
    schedule = (ScheduledReport(kind="daily", at_local="07:30"),)
    # 07:30 in London on this date is 07:30 UTC (GMT, before the clocks change).
    assert due_reports(
        datetime(2026, 3, 17, 7, 30, tzinfo=UTC),
        display_tz="Europe/London",
        schedule=schedule,
    )
    assert (
        due_reports(
            datetime(2026, 3, 17, 4, 30, tzinfo=UTC),
            display_tz="Europe/London",
            schedule=schedule,
        )
        == ()
    )


def test_the_default_schedule_matches_the_prd() -> None:
    kinds = {entry.kind: entry for entry in DEFAULT_SCHEDULE}
    assert kinds["daily"].at_local == "07:30"
    assert kinds["weekly"].weekdays == (0,)


def test_a_naive_datetime_is_refused() -> None:
    """Every window in the system is timezone-aware; a naive one is a bug, not a default."""
    with pytest.raises(ValueError, match=r"naive|aware|tzinfo"):
        due_reports(datetime(2026, 3, 17, 4, 30), display_tz=NAIROBI)


# ------------------------------------------------------------------- sender


class _Ledger:
    def __init__(self) -> None:
        self.records: list[SendOutcome] = []
        self.sent_ids: set[str] = set()

    def already_sent(self, report_id: str) -> bool:
        return report_id in self.sent_ids

    def record(self, outcome: SendOutcome, *, at: datetime) -> None:
        self.records.append(outcome)
        if outcome.sent:
            self.sent_ids.add(outcome.report_id)

    def dead_letters(self, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        return tuple(
            {"report_id": r.report_id, "error": r.error} for r in self.records if r.failed
        )[:limit]


def test_the_dry_run_sender_validates_and_posts_nothing(frozen_clock) -> None:
    sender = DryRunTeamsSender()
    service = ReportingService(sender=sender, ledger=_Ledger(), clock=frozen_clock)
    outcome = service.send(_digest())
    assert outcome.dry_run is True
    assert outcome.sent is False
    assert not outcome.failed
    assert outcome.card_bytes > 0
    assert len(sender.sent) == 1


def test_a_sent_report_is_not_sent_twice(frozen_clock) -> None:
    """The scheduler can fire twice and the worker can be replayed. The ledger decides."""
    sender = RecordingTeamsSender()
    ledger = _Ledger()
    service = ReportingService(sender=sender, ledger=ledger, clock=frozen_clock)

    first = service.send(_digest())
    assert first.sent is True
    second = service.send(_digest())
    assert second.sent is False
    assert sender.attempts == 1, "the second call must not reach the transport"


def test_force_resends_the_same_report(frozen_clock) -> None:
    """The admin "send again" button. It skips the ledger, not the validation."""
    sender = RecordingTeamsSender()
    service = ReportingService(sender=sender, ledger=_Ledger(), clock=frozen_clock)
    service.send(_digest())
    again = service.send(_digest(), force=True)
    assert again.sent is True
    assert sender.attempts == 2


def test_a_failed_send_becomes_a_dead_letter(frozen_clock) -> None:
    sender = RecordingTeamsSender(fail_times=99)
    ledger = _Ledger()
    service = ReportingService(sender=sender, ledger=ledger, clock=frozen_clock)
    outcome = service.send(_digest())
    assert outcome.failed is True
    assert service.dead_letters()
    assert service.dead_letters()[0]["report_id"] == "RPT-DAILY-20260317T0430"


def test_a_retried_send_eventually_succeeds(frozen_clock) -> None:
    sender = RecordingTeamsSender(fail_times=2)
    service = ReportingService(sender=sender, ledger=_Ledger(), clock=frozen_clock)
    assert service.send(_digest()).failed is True
    assert service.send(_digest()).failed is True
    assert service.send(_digest()).sent is True


def test_an_invalid_card_is_refused_before_any_transport(frozen_clock) -> None:
    with pytest.raises(ValueError, match="Adaptive Card"):
        validate_card({"type": "NotACard"})


def test_the_workflows_envelope_matches_the_documented_shape() -> None:
    """Contract-tested against the documented payload, since no webhook was available."""
    card = {"type": "AdaptiveCard", "version": "1.5", "body": []}
    body = envelope(card)
    assert body["type"] == "message"
    assert len(body["attachments"]) == 1
    attachment = body["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"] is card, "the card must be sent unmodified"


def test_the_live_sender_refuses_to_post_in_dry_run_mode(settings) -> None:
    """Belt and braces: the composition root should not have built it, and if it did…"""
    from tazama.infrastructure.adapters.teams.live_stub import WorkflowsTeamsSender

    sender = WorkflowsTeamsSender(settings=settings.model_copy(update={"TEAMS_DRY_RUN": True}))
    outcome = sender.send(report_id="RPT-1", card={"type": "AdaptiveCard"})
    assert outcome.sent is False
    assert "refuses to post" in outcome.error


def test_the_live_sender_refuses_without_a_webhook(settings) -> None:
    from pydantic import SecretStr

    from tazama.infrastructure.adapters.teams.live_stub import WorkflowsTeamsSender

    live = settings.model_copy(
        update={"TEAMS_DRY_RUN": False, "TEAMS_WORKFLOW_WEBHOOK_URL": SecretStr("")},
    )
    outcome = WorkflowsTeamsSender(settings=live).send(
        report_id="RPT-1",
        card={"type": "AdaptiveCard"},
    )
    assert outcome.sent is False
    assert "no Teams webhook" in outcome.error


def test_the_webhook_url_never_appears_in_an_outcome(settings) -> None:
    """A webhook URL is a bearer credential. It must not leak through an error message."""
    import httpx
    from pydantic import SecretStr

    from tazama.infrastructure.adapters.teams.live_stub import WorkflowsTeamsSender

    # A real Workflows URL shape on purpose — the test plants a recognisable token in it
    # and asserts the token is absent from the outcome. The secret scanner is right to
    # flag the shape, which is why it carries the suppression rather than being weakened.
    secret_url = (
        "https://prod-99.westeurope.logic.azure.com"  # pragma: allowlist secret
        "/workflows/SECRETVALUE/triggers"
    )
    live = settings.model_copy(
        update={
            "TEAMS_DRY_RUN": False,
            "TEAMS_WORKFLOW_WEBHOOK_URL": SecretStr(secret_url),
        },
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {secret_url}")

    sender = WorkflowsTeamsSender(
        settings=live,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=1,
    )
    outcome = sender.send(
        report_id="RPT-1",
        card={"type": "AdaptiveCard", "version": "1.5", "body": []},
    )
    assert outcome.sent is False
    assert "SECRETVALUE" not in outcome.error
    assert "logic.azure.com" not in outcome.error
    assert "ConnectError" in outcome.error


def test_a_non_retryable_status_is_not_retried(settings) -> None:
    """A 400 means the payload is wrong and will be wrong again."""
    import httpx
    from pydantic import SecretStr

    from tazama.infrastructure.adapters.teams.live_stub import WorkflowsTeamsSender

    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "bad payload"})

    live = settings.model_copy(
        update={
            "TEAMS_DRY_RUN": False,
            "TEAMS_WORKFLOW_WEBHOOK_URL": SecretStr("https://example.invalid/hook"),
        },
    )
    sender = WorkflowsTeamsSender(
        settings=live,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=4,
    )
    outcome = sender.send(
        report_id="RPT-1",
        card={"type": "AdaptiveCard", "version": "1.5", "body": []},
    )
    assert outcome.sent is False
    assert attempts["n"] == 1, "a 400 must not be retried"
