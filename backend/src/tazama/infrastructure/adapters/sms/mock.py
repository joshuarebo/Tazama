"""The v1 SMS gateway: records what would be sent, sends nothing.

This is not a test double that happens to ship — it is the product's only SMS path in v1.
`SMS_GATEWAY_MODE=mock` is the only supported value, the notification service refuses any
other mode, and the pilot measures calls avoided from what this adapter recorded.

Validation is deliberately as strict as a live adapter's would be, so switching to a real
provider cannot be the first time a bad destination is noticed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tazama.core.errors import ValidationFailed
from tazama.core.ids import deterministic_id
from tazama.modules.notification.models import SendReceipt, SmsMessage

MODE = "mock"
PROVIDER = "mock"

#: A destination is a pseudonym token (`SUB-` + 16 base32 characters), never a number.
#: An MSISDN reaching this boundary is a pseudonymisation failure upstream, so it is
#: refused loudly rather than quietly delivered.
_TOKEN = re.compile(r"^SUB-[A-Z2-7]{16}$")

#: Longest message any template can produce: two SMS parts.
MAX_TEXT_CHARS = 320


def validate_destination(destination: str) -> str:
    """Refuse anything that is not a pseudonym token. Shared by both adapters."""
    candidate = destination.strip()
    if not candidate:
        raise ValidationFailed("SMS destination is required.")
    if not _TOKEN.fullmatch(candidate):
        raise ValidationFailed(
            "SMS destination must be a subscriber pseudonym token (SUB-...); a raw "
            "identifier must never reach the gateway.",
        )
    return candidate


def validate_message(message: SmsMessage) -> None:
    """Checks every adapter applies before a send is even attempted."""
    validate_destination(message.destination)
    if not message.text.strip():
        raise ValidationFailed("SMS text is empty; there is nothing to send.")
    if len(message.text) > MAX_TEXT_CHARS:
        raise ValidationFailed(
            f"SMS text is {len(message.text)} characters, over the {MAX_TEXT_CHARS}-character "
            f"two-part limit.",
        )
    if not message.template_id.strip():
        raise ValidationFailed(
            "SMS has no template id; free-text customer messages are not sendable.",
        )


@dataclass(slots=True)
class MockSmsGateway:
    """Records sends in memory. No sockets, no files, no clock of its own."""

    #: Set to simulate a provider rejection without changing the call shape.
    accept: bool = True
    sent: list[SmsMessage] = field(default_factory=list)

    def mode(self) -> str:
        return MODE

    def send(self, message: SmsMessage) -> SendReceipt:
        validate_message(message)
        # Store a copy, so a later mutation of the caller's object cannot rewrite history.
        recorded = replace(message, metadata=dict(message.metadata))
        self.sent.append(recorded)
        receipt_id = deterministic_id("SMS", message.reference, message.destination)
        return SendReceipt(
            id=receipt_id,
            status="accepted" if self.accept else "rejected",
            provider=PROVIDER,
            accepted=self.accept,
            detail="recorded by the mock gateway; nothing was transmitted",
        )

    def last(self) -> SmsMessage | None:
        return self.sent[-1] if self.sent else None

    def texts(self) -> tuple[str, ...]:
        return tuple(m.text for m in self.sent)

    def reset(self) -> None:
        self.sent.clear()
