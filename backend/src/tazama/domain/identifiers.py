"""Pure identifier derivation.

This lives in `domain` rather than `core` because the domain needs it: a domain event's id
is derived from the fact it describes, and `domain` may not import `core` (dependencies
point inward). `core.ids` re-exports these so the rest of the system has one import site.

The alphabet is Crockford base32 without I, L, O or U, so an id read aloud over a phone or
copied out of a log is unambiguous.
"""

from __future__ import annotations

import uuid
from typing import Final

CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def b32(value: int, length: int) -> str:
    """Encode the low `length * 5` bits of `value` in Crockford base32."""
    chars: list[str] = []
    for _ in range(length):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def deterministic_id(prefix: str, *parts: str) -> str:
    """Stable id derived from its inputs.

    The same inputs always produce the same id, which is what makes `make seed`
    reproducible, ingestion idempotent and event publication a no-op the second time.
    UUID5 gives a well-defined hash over the joined parts; the separator is a character
    that cannot appear in a part by accident.
    """
    name = "|".join(parts)
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"tazama://{prefix}/{name}").int
    return f"{prefix.upper()}_{b32(digest, 14)}"
