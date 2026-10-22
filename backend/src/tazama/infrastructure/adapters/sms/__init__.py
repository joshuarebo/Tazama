"""SMS egress adapters for the `SmsGateway` port.

Two implementations, one contract (`backend/tests/contract/test_sms_adapter_contract.py`):

  * `MockSmsGateway` — records what *would* be sent and performs no I/O. The only mode
    supported in v1 (`SMS_GATEWAY_MODE=mock`).
  * `LiveStubSmsGateway` — the shape a real provider adapter will take, with the same
    validation, which refuses to send rather than attempting a request. It exists so the
    contract test proves the boundary is real before any credential exists.

Neither opens a socket. That is asserted, not assumed.
"""

from tazama.infrastructure.adapters.sms.live_stub import LiveStubSmsGateway
from tazama.infrastructure.adapters.sms.mock import MockSmsGateway, validate_destination

__all__ = ["LiveStubSmsGateway", "MockSmsGateway", "validate_destination"]
