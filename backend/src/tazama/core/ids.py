"""Identifier generation and correlation-id plumbing.

Public ids are ULID-ish (lexicographically sortable, time-prefixed) so that paging by id
is stable and so that a support engineer can read the creation time out of an id.
"""

from __future__ import annotations

import os
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime

from tazama.core.clock import Clock, ensure_utc

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # no I, L, O, U — unambiguous when read aloud

#: Per-request correlation id, attached to every log line, event and audit row.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def _b32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


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
    return f"{prefix.upper()}_{_b32(ms, 10)}_{_b32(secrets.randbits(20), 4)}"


def deterministic_id(prefix: str, *parts: str) -> str:
    """Stable id derived from its inputs — used by the seed and by idempotent upserts.

    The same inputs always produce the same id, which is what makes `make seed`
    reproducible and ingestion idempotent.
    """
    name = "|".join(parts)
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"tazama://{prefix}/{name}").int
    return f"{prefix.upper()}_{_b32(digest, 14)}"


def new_correlation_id() -> str:
    """Fresh correlation id for an inbound request or a worker tick."""
    return _b32(int.from_bytes(os.urandom(10), "big"), 16)


def get_correlation_id() -> str:
    return correlation_id.get()


def set_correlation_id(value: str) -> None:
    correlation_id.set(value)


def _require_clock() -> Clock:
    raise ValueError("new_id() needs either a clock or an explicit `at` instant")
