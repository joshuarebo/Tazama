"""Contract shared by every `SmsGateway` adapter.

One test file, parametrised over `mock` and `live_stub`, so the port's shape is proven by
two independent implementations rather than one. The contract is:

  * a send either returns a receipt carrying an id and a status, **or** refuses with a clear
    "not configured for live sending" error — never silently does nothing;
  * an invalid destination is refused as *invalid*, by both, before anything else;
  * the message handed in is never mutated;
  * no network call happens in either — asserted by making socket creation fail.

The live stub's refusal is part of the contract, not an exception to it: v1 supports
`SMS_GATEWAY_MODE=mock` only, and the boundary has to be provably closed before a
credential exists.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from tazama.core.errors import UpstreamUnavailable, ValidationFailed
from tazama.infrastructure.adapters.sms import LiveStubSmsGateway, MockSmsGateway
from tazama.modules.notification.models import SendReceipt, SmsMessage
from tazama.modules.notification.ports import SmsGateway

pytestmark = pytest.mark.contract

DESTINATION = "SUB-ABCDEFGHIJKLMNOP"

MESSAGE = SmsMessage(
    destination=DESTINATION,
    text="We know about a data fault in Westlands and are fixing it. Expected fix: 18:00.",
    template_id="known_issue",
    language="en",
    reference="NTF_0000000001",
    metadata={"incident_id": "INC_1"},
)

#: (factory, whether a send is expected to return a receipt rather than refuse).
IMPLEMENTATIONS: tuple[tuple[str, Callable[[], Any], bool], ...] = (
    ("mock", MockSmsGateway, True),
    ("live_stub", LiveStubSmsGateway, False),
)


@pytest.fixture(params=IMPLEMENTATIONS, ids=[name for name, _, _ in IMPLEMENTATIONS])
def implementation(request: pytest.FixtureRequest) -> tuple[Any, bool]:
    _, factory, returns_receipt = request.param
    return factory(), returns_receipt


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make any socket creation an immediate, loud failure."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an SMS adapter attempted to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


def _send(gateway: Any, message: SmsMessage) -> tuple[SendReceipt | None, Exception | None]:
    try:
        return gateway.send(message), None
    except Exception as exc:  # the contract deliberately covers both outcomes
        return None, exc


# ------------------------------------------------------------------ shape


def test_the_adapter_satisfies_the_port(implementation):
    gateway, _ = implementation
    assert isinstance(gateway, SmsGateway)
    assert callable(gateway.send)
    assert isinstance(gateway.mode(), str)


def test_the_mode_is_a_stable_non_empty_string(implementation):
    gateway, _ = implementation
    assert gateway.mode() == gateway.mode()
    assert gateway.mode().strip()


def test_only_the_mock_declares_the_mode_v1_supports(implementation):
    gateway, returns_receipt = implementation
    assert (gateway.mode() == "mock") is returns_receipt


# ------------------------------------------------------------------- send


def test_send_returns_a_receipt_or_refuses_clearly(implementation, no_network):
    gateway, returns_receipt = implementation
    receipt, error = _send(gateway, MESSAGE)
    if returns_receipt:
        assert error is None
        assert receipt is not None
        assert receipt.id
        assert receipt.status
        assert receipt.provider
    else:
        assert receipt is None
        assert isinstance(error, UpstreamUnavailable)
        assert "not configured for live sending" in str(error)


def test_the_same_message_twice_gives_the_same_answer_twice(implementation, no_network):
    """Idempotent in the sense the outbox needs: a redelivery cannot change the verdict.

    A receipting adapter must return the same receipt id; a refusing one must refuse
    identically. Either way the second call tells the caller what the first one did.
    """
    gateway, returns_receipt = implementation
    first_receipt, first_error = _send(gateway, MESSAGE)
    second_receipt, second_error = _send(gateway, MESSAGE)
    if returns_receipt:
        assert first_receipt is not None
        assert second_receipt is not None
        assert first_receipt.id == second_receipt.id
        assert first_receipt.status == second_receipt.status
    else:
        assert first_receipt is None and second_receipt is None
        assert type(first_error) is type(second_error)
        assert str(first_error) == str(second_error)


def test_the_message_is_never_mutated(implementation, no_network):
    gateway, _ = implementation
    before = replace(MESSAGE, metadata=dict(MESSAGE.metadata))
    _send(gateway, MESSAGE)
    assert before == MESSAGE
    assert MESSAGE.text == before.text
    assert dict(MESSAGE.metadata) == dict(before.metadata)


def test_no_network_call_happens(implementation, no_network):
    """The `no_network` fixture turns any socket into an assertion failure."""
    gateway, _ = implementation
    _, error = _send(gateway, MESSAGE)
    assert not isinstance(error, AssertionError)


# ------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "destination",
    [
        "",
        "   ",
        "254712345678",
        "+254712345678",
        "0712345678",
        "SUB-short",
        "SUB-ABCDEFGHIJKLMNO1",  # 1 is not in the base32 alphabet used by the pseudonymiser
        "sub-abcdefghijklmnop",
        "SUB-ABCDEFGHIJKLMNOP extra",
    ],
)
def test_an_invalid_destination_is_refused(implementation, no_network, destination):
    gateway, _ = implementation
    with pytest.raises(ValidationFailed):
        gateway.send(replace(MESSAGE, destination=destination))


def test_an_invalid_destination_is_refused_as_invalid_not_as_unconfigured(
    implementation, no_network
):
    """Both adapters must give the caller the same diagnosis for the same bad input."""
    gateway, _ = implementation
    _, error = _send(gateway, replace(MESSAGE, destination="254712345678"))
    assert isinstance(error, ValidationFailed)
    assert not isinstance(error, UpstreamUnavailable)


def test_empty_text_is_refused(implementation, no_network):
    gateway, _ = implementation
    with pytest.raises(ValidationFailed, match="empty"):
        gateway.send(replace(MESSAGE, text="   "))


def test_text_over_two_sms_parts_is_refused(implementation, no_network):
    gateway, _ = implementation
    with pytest.raises(ValidationFailed, match="two-part"):
        gateway.send(replace(MESSAGE, text="x" * 321))


def test_a_message_with_no_template_id_is_refused(implementation, no_network):
    """Free-text customer messages are not sendable through either adapter."""
    gateway, _ = implementation
    with pytest.raises(ValidationFailed, match="template"):
        gateway.send(replace(MESSAGE, template_id=""))


def test_validation_happens_before_anything_is_recorded(implementation, no_network):
    gateway, returns_receipt = implementation
    with pytest.raises(ValidationFailed):
        gateway.send(replace(MESSAGE, destination="0712345678"))
    recorded = gateway.sent if returns_receipt else gateway.attempted
    assert recorded == []


# ------------------------------------------------- implementation specifics


def test_the_mock_records_what_would_have_been_sent(no_network):
    gateway = MockSmsGateway()
    gateway.send(MESSAGE)
    assert len(gateway.sent) == 1
    assert gateway.texts() == (MESSAGE.text,)
    assert gateway.last() == MESSAGE


def test_the_mock_copies_the_message_so_history_cannot_be_rewritten(no_network):
    gateway = MockSmsGateway()
    mutable = dict(MESSAGE.metadata)
    message = replace(MESSAGE, metadata=mutable)
    gateway.send(message)
    mutable["incident_id"] = "INC_TAMPERED"
    assert gateway.sent[0].metadata["incident_id"] == "INC_1"


def test_the_mock_can_model_a_provider_rejection(no_network):
    gateway = MockSmsGateway(accept=False)
    receipt = gateway.send(MESSAGE)
    assert receipt.accepted is False
    assert receipt.status == "rejected"


def test_the_live_stub_records_the_refused_attempt_and_describes_itself(no_network):
    gateway = LiveStubSmsGateway()
    with pytest.raises(UpstreamUnavailable):
        gateway.send(MESSAGE)
    assert gateway.attempted == [MESSAGE]
    assert "refuses to send" in gateway.describe()


def test_the_live_stub_error_carries_the_provider_and_mode(no_network):
    gateway = LiveStubSmsGateway()
    with pytest.raises(UpstreamUnavailable) as caught:
        gateway.send(MESSAGE)
    assert caught.value.extra["mode"] == "live_stub"
    assert caught.value.extra["provider"] == "live_stub"
