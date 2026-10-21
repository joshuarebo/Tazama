"""Persistence adapters. Oracle specifics stay inside this package (ADR 0002).

Public surface used by the rest of the system:
    ping / check_connection   health probes
    get_engine / session_scope transactional access
    Base / models             the canonical schema
"""

from tazama.infrastructure.db import models  # noqa: F401 - registers every table on Base
from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.engine import (
    build_engine,
    check_connection,
    dialect_name,
    get_engine,
    ping,
    require_connection,
    reset_engine_cache,
    session_factory,
    session_scope,
)

__all__ = [
    "Base",
    "build_engine",
    "check_connection",
    "dialect_name",
    "get_engine",
    "ping",
    "require_connection",
    "reset_engine_cache",
    "session_factory",
    "session_scope",
]
