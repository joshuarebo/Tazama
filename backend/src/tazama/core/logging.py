"""Structured JSON logging with correlation ids and hard redaction.

Two rules that are tested, not merely intended:
  * Every log line is one JSON object with `ts`, `level`, `msg`, `correlation_id`, `module`.
  * Secrets and raw subscriber identifiers never reach a log line, whatever the caller does.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from tazama.core.ids import get_correlation_id

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}

#: Keys whose values are replaced wholesale, however deeply nested.
SECRET_KEY_PATTERN = re.compile(
    r"(pass|passwd|password|secret|token|api_?key|authorization|cookie|salt|webhook|dsn|credential)",
    re.IGNORECASE,
)

#: Kenyan MSISDN shapes (+2547xx, 2541xx, 07xx, 01xx) and long digit runs.
MSISDN_PATTERN = re.compile(r"(?<!\d)(?:\+?254|0)(?:7|1)\d{8}(?!\d)")
LONG_DIGITS_PATTERN = re.compile(r"(?<!\d)\d{12,}(?!\d)")
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
URL_CREDENTIALS_PATTERN = re.compile(r"://[^/\s:@]+:[^/\s@]+@")

REDACTED = "***"
MASKED_MSISDN = "[MSISDN]"


def scrub_text(text: str) -> str:
    """Remove identifiers and credential shapes from free text."""
    text = BEARER_PATTERN.sub("bearer " + REDACTED, text)
    text = URL_CREDENTIALS_PATTERN.sub("://" + REDACTED + "@", text)
    text = MSISDN_PATTERN.sub(MASKED_MSISDN, text)
    return LONG_DIGITS_PATTERN.sub(MASKED_MSISDN, text)


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret keys and identifier-shaped values."""
    if _depth > 8:
        return "<truncated>"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            key_s = str(key)
            out[key_s] = REDACTED if SECRET_KEY_PATTERN.search(key_s) else scrub(val, _depth + 1)
        return out
    if isinstance(value, list | tuple | set):
        return [scrub(v, _depth + 1) for v in value]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return scrub_text(str(value))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the fields our dashboards and runbooks expect."""

    def __init__(self, service: str = "tazama", mode: str = "demo") -> None:
        super().__init__()
        self.service = service
        self.mode = mode

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "msg": scrub_text(record.getMessage()),
            "logger": record.name,
            "module": record.module,
            "service": self.service,
            "app_mode": self.mode,
            "correlation_id": get_correlation_id(),
        }
        for key, val in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = scrub(val)
        if record.exc_info:
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")
            payload["exc"] = scrub_text(self.formatException(record.exc_info))[-4000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", service: str = "tazama", mode: str = "demo") -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter(service=service, mode=mode))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn duplicates access logs through its own handlers; let the root own output.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
