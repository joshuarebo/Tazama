"""Declarative base and naming conventions.

Oracle allows at most 30 characters for an identifier (older releases) so every generated
constraint and index name is kept short by construction, and a test asserts the limit.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Short prefixes keep generated names inside Oracle's 30-character identifier limit.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

MAX_IDENTIFIER_LEN = 30


class Base(DeclarativeBase):
    """All canonical tables inherit from here."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} {pk}>"
