"""Column types that behave identically on SQLite and Oracle.

Oracle pitfalls handled here once, so no module has to think about them (CLAUDE.md §5):
  * **Timestamps** are stored in UTC and come back timezone-aware. SQLite forgets the
    offset; Oracle returns its own tzinfo. `UtcDateTime` normalises both directions and
    refuses a naive value rather than guessing a zone.
  * **Empty string is NULL on Oracle.** `NonEmptyStr` makes that explicit instead of
    letting `''` silently become NULL on one backend and not the other.
  * **JSON** is stored as text (CLOB on Oracle) with an `IS JSON` check added by the
    migration, and is always round-tripped through a validating codec.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Dialect, String, Text, TypeDecorator
from sqlalchemy.types import DateTime


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps on every backend."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime refused: TAZAMA stores UTC and requires aware datetimes",
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite drops the offset; everything we write is UTC, so re-attach it.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JsonText(TypeDecorator[Any]):
    """JSON held as text, so the same DDL works on SQLite and on Oracle CLOB.

    Decoding failures surface as a clear error rather than a silent `None`: a corrupt
    evidence or payload column is a bug worth stopping for.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, dict | list):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"column does not contain valid JSON: {exc}") from exc


class NonEmptyStr(TypeDecorator[str]):
    """A string column where `''` is rejected, because Oracle would store it as NULL."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value == "":
            raise ValueError(
                "empty string refused: Oracle stores '' as NULL, so use None explicitly",
            )
        return value


#: Vector columns are declared as JSON text in the portable schema. The Oracle adapter
#: swaps in a native VECTOR column when the capability probe reports support (ADR 0005).
Embedding = JsonText
