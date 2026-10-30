"""When reports go out, and the service that sends them exactly once.

The timezone handling is the part worth reading. The requirement is "07:30 Africa/Nairobi",
and the obvious implementation — store 04:30 UTC — is wrong in a way that only shows up
years later: it hard-codes today's offset into the schedule, so the day a timezone rule
changes the digest silently arrives an hour out. So the local time is stored and converted
at the moment of comparison, which is the same rule the rest of the product follows
(store UTC, convert at the edge) applied in the other direction.

Idempotency is the other half. The scheduler can fire twice, the worker can be replayed,
and an operator can press "send now" after an automatic send — so the ledger, not the
caller, decides whether a report has already gone out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tazama.core.clock import Clock, SystemClock, ensure_utc
from tazama.core.logging import get_logger
from tazama.modules.reporting.digest import build_card
from tazama.modules.reporting.models import Digest
from tazama.modules.reporting.ports import ReportLedger, ScheduledReport, SendOutcome, TeamsSender

_log = get_logger(__name__)

#: The default schedule (PRD §13). Local times in `DISPLAY_TZ`.
DEFAULT_SCHEDULE: tuple[ScheduledReport, ...] = (
    ScheduledReport(
        kind="daily",
        at_local="07:30",
        note="Daily digest, before the morning shift handover.",
    ),
    ScheduledReport(
        kind="weekly",
        at_local="08:00",
        weekdays=(0,),
        note="Weekly summary, Monday morning.",
    ),
)

#: How close to the scheduled minute a tick has to be to fire. Wider than the tick
#: interval so a slow poll cannot skip a day, and the ledger makes a double-fire harmless.
WINDOW = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class DueReport:
    kind: str
    scheduled_for: datetime
    entry: ScheduledReport


def due_reports(
    now_utc: datetime,
    *,
    display_tz: str,
    schedule: tuple[ScheduledReport, ...] = DEFAULT_SCHEDULE,
    window: timedelta = WINDOW,
) -> tuple[DueReport, ...]:
    """Which reports are due at `now_utc`.

    Pure: no clock, no database. The scheduler calls it with a time, and so do the tests —
    which is the only way "07:30 Nairobi is 04:30 UTC" gets checked rather than assumed.
    """
    now = ensure_utc(now_utc)
    zone = ZoneInfo(display_tz)
    local = now.astimezone(zone)

    due: list[DueReport] = []
    for entry in schedule:
        if not entry.enabled:
            continue
        if entry.weekdays and local.weekday() not in entry.weekdays:
            continue
        hour, _, minute = entry.at_local.partition(":")
        target_local = local.replace(
            hour=int(hour),
            minute=int(minute),
            second=0,
            microsecond=0,
        )
        # Compared in UTC, because a local-time subtraction across a DST boundary is
        # meaningless even where — as in Nairobi — there is currently no DST to cross.
        target = target_local.astimezone(UTC)
        if timedelta(0) <= now - target < window:
            due.append(DueReport(kind=entry.kind, scheduled_for=target, entry=entry))
    return tuple(due)


class ReportingService:
    """Build a digest, send it once, and record what happened."""

    def __init__(
        self,
        *,
        sender: TeamsSender,
        ledger: ReportLedger,
        clock: Clock | None = None,
        console_url: str | None = None,
    ) -> None:
        self._sender = sender
        self._ledger = ledger
        self._clock = clock or SystemClock()
        self._console_url = console_url

    def send(self, digest: Digest, *, force: bool = False) -> SendOutcome:
        """Send one digest, unless it has already gone out.

        `force` exists for the admin "send again" button and is audited by the caller. It
        does not bypass validation — only the ledger check.
        """
        if not force and self._ledger.already_sent(digest.report_id):
            _log.info("report_already_sent", extra={"report_id": digest.report_id})
            return SendOutcome(
                report_id=digest.report_id,
                sent=False,
                error="",
                dry_run=False,
                attempts=0,
            )

        card = build_card(digest, console_url=self._console_url)
        outcome = self._sender.send(report_id=digest.report_id, card=card)
        self._ledger.record(outcome, at=self._clock.now())

        if outcome.failed:
            _log.warning(
                "report_send_failed",
                extra={"report_id": digest.report_id, "error": outcome.error},
            )
        return outcome

    def dead_letters(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        """Reports that could not be delivered, for the admin page and the runbook."""
        return self._ledger.dead_letters(limit=limit)
