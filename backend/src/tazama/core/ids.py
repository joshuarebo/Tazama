"""Identifier generation and correlation-id plumbing.

Public ids are ULID-ish (lexicographically sortable, time-prefixed) so that paging by id
is stable and so that a support engineer can read the creation time out of an id.

`deterministic_id` lives in `domain.identifiers` — the domain needs it to derive event ids,
and the domain may not import `core` — and is re-exported here so callers have one import
site regardless of layer.
"""

from __future__ import annotations

import os
import secrets
from contextvars import ContextVar
from datetime import datetime

from tazama.core.clock import Clock, ensure_utc
from tazama.domain.identifiers import CROCKFORD, b32, deterministic_id

__all__ = [
    "correlation_id",
    "deterministic_id",
    "get_correlation_id",
    "new_correlation_id",
    "new_id",
    "set_correlation_id",
]

#: Per-request correlation id, attached to every log line, event and audit row.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def new_id(prefix: str, clock: Clock | None = None, *, at: datetime | None = None) -> str:
    """Time-sortable id, e.g. `INC_01JBQ7X9K2_7F3A`.

    Args:
        prefix: short domain tag (`INC`, `CMP`, `NTF`, ...). Kept short so Oracle
            identifiers and indexes stay small.
        clock: injected clock; required unless `at` is given.
        at: explicit instant (replay/seed use this for determinism).
    """
    moment = ensure_utc(at) if at is not None else (clock or _require_clock()).now()
    ms = int(moment.timestamp() * 1000)
    return f"{prefix.upper()}_{b32(ms, 10)}_{b32(secrets.randbits(20), 4)}"


def new_correlation_id() -> str:
    """Fresh correlation id for an inbound request or a worker tick."""
    return b32(int.from_bytes(os.urandom(10), "big"), 16)


def get_correlation_id() -> str:
    return correlation_id.get()


def set_correlation_id(value: str) -> None:
    correlation_id.set(value)


def _require_clock() -> Clock:
    raise ValueError("new_id() needs either a clock or an explicit `at` instant")


#: Kept importable from here for the one or two callers that spell out the alphabet.
_CROCKFORD = CROCKFORD
