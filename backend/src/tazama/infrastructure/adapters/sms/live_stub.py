"""The live SMS gateway, deliberately not wired up.

It exists for three reasons:
  * the contract test runs against both implementations, so the port's shape is proven by
    two independent adapters rather than one;
  * the validation a real provider needs is written and tested now, not during the first
    live incident;
  * a misconfiguration fails with a sentence a human can act on, instead of a timeout
    against an endpoint nobody meant to call.

It never attempts a request. There is no HTTP client in this module — not an unconfigured
one, not a lazily imported one — so there is nothing to accidentally enable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tazama.core.errors import UpstreamUnavailable
from tazama.infrastructure.adapters.sms.mock import validate_message
from tazama.modules.notification.models import SendReceipt, SmsMessage

MODE = "live_stub"
PROVIDER = "live_stub"

NOT_CONFIGURED = (
    "SMS gateway is not configured for live sending: v1 supports SMS_GATEWAY_MODE=mock "
    "only. Wiring a real provider is a reviewed change (credentials, rate limits, "
    "delivery receipts and a DPIA), not a configuration flip."
)


@dataclass(slots=True)
class LiveStubSmsGateway:
    """Validates exactly as a live adapter would, then refuses to transmit."""

    #: Every message that reached the refusal, so a test can assert nothing was mutated and
    #: an operator can see what *would* have gone out had the gateway been configured.
    attempted: list[SmsMessage] = field(default_factory=list)

    def mode(self) -> str:
        return MODE

    def send(self, message: SmsMessage) -> SendReceipt:
        # Validation first: an invalid destination is refused as invalid, not as
        # unconfigured. The caller must get the same error from either adapter.
        validate_message(message)
        self.attempted.append(message)
        raise UpstreamUnavailable(NOT_CONFIGURED, provider=PROVIDER, mode=MODE)

    def describe(self) -> str:
        """One line for `scripts/doctor.py` and the readiness probe."""
        return f"sms:{MODE} — refuses to send; {len(self.attempted)} attempt(s) refused"
