"""The dry-run Teams sender: renders, validates, records, posts nothing.

This is the default and it is not a test double. `TEAMS_DRY_RUN=true` ships, and the
preview panel in the console renders exactly what this adapter would have posted. Nothing
in the product requires a webhook to work.

Validation here is as strict as the live adapter's — the same PII scan, the same size
guard — so switching to a real webhook cannot be the first time a bad card is noticed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tazama.core.logging import get_logger
from tazama.modules.reporting.digest import MAX_CARD_BYTES, assert_no_pii
from tazama.modules.reporting.ports import SendOutcome

_log = get_logger(__name__)

MODE = "dry-run"


def validate_card(card: dict[str, Any]) -> int:
    """Checks every sender applies. Returns the payload size in bytes.

    Shared between the dry-run and live adapters on purpose: a card that the dry run
    accepted and the live sender rejected would make the preview a lie.
    """
    if not isinstance(card, dict) or card.get("type") != "AdaptiveCard":
        raise ValueError("payload is not an Adaptive Card")
    assert_no_pii(card)
    size = len(json.dumps(card))
    if size > MAX_CARD_BYTES:
        raise ValueError(f"card is {size} bytes, over the {MAX_CARD_BYTES} limit")
    return size


@dataclass
class DryRunTeamsSender:
    """Validates and records. The default in every environment until a webhook exists."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, *, report_id: str, card: dict[str, Any]) -> SendOutcome:
        try:
            size = validate_card(card)
        except Exception as error:
            _log.error("teams_card_invalid", extra={"report_id": report_id})
            return SendOutcome(report_id=report_id, sent=False, dry_run=True, error=str(error))

        self.sent.append({"report_id": report_id, "card": card})
        # The webhook URL is never logged, in either adapter. There is nothing to log here
        # because there is no URL — which is the point of the default.
        _log.info("teams_dry_run", extra={"report_id": report_id, "card_bytes": size})
        return SendOutcome(report_id=report_id, sent=False, dry_run=True, card_bytes=size)


@dataclass
class RecordingTeamsSender(DryRunTeamsSender):
    """A dry-run sender that can be told to fail, for the retry and dead-letter tests."""

    fail_times: int = 0
    attempts: int = 0

    def send(self, *, report_id: str, card: dict[str, Any]) -> SendOutcome:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            return SendOutcome(
                report_id=report_id,
                sent=False,
                error="simulated transport failure",
                attempts=self.attempts,
            )
        outcome = super().send(report_id=report_id, card=card)
        return SendOutcome(
            report_id=outcome.report_id,
            sent=True,
            status_code=200,
            dry_run=False,
            attempts=self.attempts,
            card_bytes=outcome.card_bytes,
        )
