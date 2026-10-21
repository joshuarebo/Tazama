"""Logs must never carry a secret or a raw MSISDN, whatever the caller passes."""

from __future__ import annotations

import json
import logging

import pytest

from tazama.core.ids import set_correlation_id
from tazama.core.logging import JsonFormatter, configure_logging, scrub, scrub_text

# These two fixtures hold credential *shapes* on purpose: the point of the tests below is
# that the formatter scrubs them. The scanner marker keeps them out of the secret report.
_HOOK = "https://prod-12.westeurope.logic.azure.com/workflows/abc"  # pragma: allowlist secret
_BEARER = "Authorization: Bearer eyJhbGciOi.abc.def"  # pragma: allowlist secret
_DSN = "oracle://user:pass@db:1521/X"  # pragma: allowlist secret
WEBHOOK_SHAPED = _HOOK
CREDENTIAL_SHAPED = f"{_BEARER} and {_DSN}"


def _render(record_kwargs: dict[str, object], message: str = "event") -> dict[str, object]:
    formatter = JsonFormatter(service="tazama-test", mode="demo")
    record = logging.LogRecord(
        name="tazama.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in record_kwargs.items():
        setattr(record, key, value)
    return json.loads(formatter.format(record))


def test_log_line_is_json_with_required_fields() -> None:
    set_correlation_id("ABC123")
    payload = _render({"incident_id": "INC_1"})
    assert payload["level"] == "INFO"
    assert payload["msg"] == "event"
    assert payload["correlation_id"] == "ABC123"
    assert payload["service"] == "tazama-test"
    assert payload["app_mode"] == "demo"
    assert payload["incident_id"] == "INC_1"
    assert "ts" in payload


@pytest.mark.parametrize(
    "msisdn",
    ["254712345678", "+254712345678", "0712345678", "0110123456", "254110123456"],
)
def test_every_msisdn_shape_is_masked(msisdn: str) -> None:
    payload = _render({}, message=f"complaint from {msisdn} about no service")
    assert msisdn not in str(payload)
    assert "[MSISDN]" in str(payload["msg"])


def test_secret_keys_are_redacted_however_nested() -> None:
    payload = _render(
        {
            "context": {
                "oracle": {"password": "hunter2", "dsn": "host:1521/PDB"},
                "teams_webhook_url": WEBHOOK_SHAPED,
                "api_key": "sk-abcdef123456",
                "safe": "keep-me",
            },
        },
    )
    context = payload["context"]
    assert isinstance(context, dict)
    assert context["oracle"] == {"password": "***", "dsn": "***"}
    assert context["teams_webhook_url"] == "***"
    assert context["api_key"] == "***"
    assert context["safe"] == "keep-me"


def test_bearer_tokens_and_url_credentials_are_scrubbed() -> None:
    text = scrub_text(CREDENTIAL_SHAPED)
    assert "eyJhbGciOi" not in text
    assert "user:pass@" not in text
    assert "***" in text


def test_long_digit_runs_are_masked() -> None:
    assert "123456789012345" not in scrub_text("account 123456789012345 flagged")


def test_scrub_is_depth_limited() -> None:
    nested: dict[str, object] = {"k": "v"}
    for _ in range(20):
        nested = {"k": nested}
    assert "truncated" in json.dumps(scrub(nested))


def test_exception_text_is_scrubbed() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("failed for 0712345678")  # noqa: TRY301 - exercising exc logging
    except ValueError:
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=(),
            exc_info=True,
        )
        import sys

        record.exc_info = sys.exc_info()
        payload = json.loads(formatter.format(record))
    assert "0712345678" not in payload["exc"]
    assert payload["exc_type"] == "ValueError"


def test_configure_logging_installs_single_handler() -> None:
    configure_logging(level="DEBUG", service="tazama", mode="demo")
    configure_logging(level="INFO", service="tazama", mode="demo")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.INFO
