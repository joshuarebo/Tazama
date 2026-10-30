"""The live Teams sender, against a Power Automate **Workflows** webhook.

Unverified, and labelled as such (`docs/tech-debt.md` #15). No webhook was available during
the build, so this is contract-tested against the documented payload shape at the HTTP
boundary and left behind `TEAMS_DRY_RUN=true`. What it does *not* do is guess: the envelope
below is the one Workflows documents for an Adaptive Card, and the retry behaviour follows
the documented 429/5xx semantics.

Why Workflows rather than an Office 365 connector: connectors are retired as of
31 March 2026, so building against them would have been building something with a known
end date.

Two rules this adapter exists to keep:

* **The webhook URL is never logged**, never put in an error message, and never returned.
  It is a bearer credential in a URL — anyone who reads it can post to the channel.
* **The card is sent unmodified.** It was PII-scanned and size-checked upstream; an adapter
  that rewrites the payload invalidates that check.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from tazama.core.logging import get_logger
from tazama.core.settings import Settings, get_settings
from tazama.infrastructure.adapters.teams.mock import validate_card
from tazama.modules.reporting.ports import SendOutcome

_log = get_logger(__name__)

MODE = "workflows"

#: The Workflows "Post card in a chat or channel" trigger takes the card inside an
#: attachments envelope. Documented shape, not inferred.
_ATTACHMENT_TYPE = "application/vnd.microsoft.card.adaptive"

#: Retry only what is worth retrying. A 400 means the payload is wrong and will be wrong
#: again; a 429 or a 5xx means the service is busy.
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


def envelope(card: dict[str, Any]) -> dict[str, Any]:
    """Wrap a card in the Workflows request body."""
    return {
        "type": "message",
        "attachments": [
            {"contentType": _ATTACHMENT_TYPE, "contentUrl": None, "content": card},
        ],
    }


@dataclass
class WorkflowsTeamsSender:
    """Posts to a Workflows webhook. Refuses to run unless explicitly enabled."""

    settings: Settings | None = None
    client: httpx.Client | None = None
    max_attempts: int = 4

    def __post_init__(self) -> None:
        self._settings = self.settings or get_settings()

    def send(self, *, report_id: str, card: dict[str, Any]) -> SendOutcome:
        if self._settings.TEAMS_DRY_RUN:
            # Belt and braces: the composition root should never have built this adapter
            # in dry-run mode, and if it did, the answer is to refuse rather than post.
            return SendOutcome(
                report_id=report_id,
                sent=False,
                dry_run=True,
                error="TEAMS_DRY_RUN is true; the live sender refuses to post",
            )

        url = self._settings.TEAMS_WORKFLOW_WEBHOOK_URL.get_secret_value()
        if not url:
            return SendOutcome(
                report_id=report_id,
                sent=False,
                error="no Teams webhook is configured",
            )

        try:
            size = validate_card(card)
        except Exception as error:
            return SendOutcome(report_id=report_id, sent=False, error=str(error))

        body = envelope(card)
        client = self.client or httpx.Client(timeout=15.0)
        last_error = ""
        status: int | None = None

        try:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    response = client.post(url, json=body)
                    status = response.status_code
                except httpx.HTTPError as error:
                    # The exception text can contain the URL, so only the type is kept.
                    last_error = f"transport error: {type(error).__name__}"
                else:
                    if 200 <= status < 300:
                        _log.info(
                            "teams_sent",
                            extra={
                                "report_id": report_id,
                                "status": status,
                                "attempt": attempt,
                                "card_bytes": size,
                            },
                        )
                        return SendOutcome(
                            report_id=report_id,
                            sent=True,
                            status_code=status,
                            attempts=attempt,
                            card_bytes=size,
                        )
                    last_error = f"Teams returned {status}"
                    if status not in _RETRYABLE:
                        break

                if attempt < self.max_attempts:
                    # Jittered backoff, so a fleet of workers does not retry in lockstep
                    # against a service that is already rate-limiting.
                    time.sleep(min(8.0, 0.5 * 2**attempt) * (0.5 + random.random()))  # noqa: S311

            _log.warning(
                "teams_send_failed",
                extra={"report_id": report_id, "status": status, "attempts": self.max_attempts},
            )
            return SendOutcome(
                report_id=report_id,
                sent=False,
                status_code=status,
                error=last_error or "send failed",
                attempts=self.max_attempts,
                card_bytes=size,
            )
        finally:
            if self.client is None:
                client.close()
