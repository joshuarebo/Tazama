"""What reporting needs from the outside world.

Declared in the consumer, implemented by adapters, wired in the composition root — the
same shape as every other module. The sender is the interesting one: it is the only thing
in the product that can push data out of the network, so it has two implementations that
are contract-tested against the same tests, and the dry-run one is the default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """What happened to one card. `sent=False` with no error means dry-run."""

    report_id: str
    sent: bool
    status_code: int | None = None
    dry_run: bool = False
    error: str = ""
    attempts: int = 1
    #: The rendered card, kept for the preview panel and the dead-letter row.
    card_bytes: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.error)


@runtime_checkable
class TeamsSender(Protocol):
    """Deliver one Adaptive Card.

    The card arrives already validated and PII-scanned by `digest.build_card`. A sender
    must not modify it: an adapter that rewrites the payload invalidates the check that
    was performed upstream.
    """

    def send(self, *, report_id: str, card: dict[str, Any]) -> SendOutcome: ...


@runtime_checkable
class ReportLedger(Protocol):
    """Which reports have already gone out, so a retry cannot double-post.

    Idempotency is the sender's whole correctness story: the scheduler can fire twice, the
    worker can be replayed, and an operator can press "send now" a second time.
    """

    def already_sent(self, report_id: str) -> bool: ...

    def record(self, outcome: SendOutcome, *, at: datetime) -> None: ...

    def dead_letters(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]: ...


@dataclass(frozen=True, slots=True)
class ScheduledReport:
    """One entry in the reporting schedule."""

    kind: str
    #: Local time of day in `DISPLAY_TZ`, as "HH:MM". Stored local and converted at the
    #: edge, because "07:30 Nairobi" is the requirement and 04:30 UTC is an implementation
    #: detail that breaks the day a timezone rule changes.
    at_local: str
    #: Days of the week it runs on, 0 = Monday. Empty means every day.
    weekdays: tuple[int, ...] = ()
    enabled: bool = True
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
