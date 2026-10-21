"""Engine and session construction for both backends (ADR 0002).

One place decides how to connect, so the rest of the system only ever asks for a session.
Oracle specifics (pooling, pre-ping, NLS, statement timeout) are applied here and in
`oracle_dialect.py`; SQLite gets the settings that make it behave predictably in tests
(foreign keys on, WAL, a shared in-memory database).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tazama.core.errors import ReadOnlyViolation, UpstreamUnavailable
from tazama.core.logging import get_logger
from tazama.core.settings import Settings, get_settings

_log = get_logger(__name__)

IN_MEMORY = ":memory:"


def _sqlite_url(settings: Settings) -> str:
    target = settings.sqlite_target
    if target == IN_MEMORY:
        return "sqlite+pysqlite://"
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _oracle_url(settings: Settings) -> str:
    # python-oracledb thin mode: no Instant Client needed. Credentials come from env only.
    user = settings.ORACLE_USER
    password = settings.ORACLE_PASSWORD.get_secret_value()
    return f"oracle+oracledb://{user}:{password}@{settings.ORACLE_DSN}"


def build_engine(settings: Settings | None = None, *, echo: bool = False) -> Engine:
    """Create an Engine for the configured backend."""
    settings = settings or get_settings()

    if settings.DB_BACKEND == "sqlite":
        url = _sqlite_url(settings)
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if url.endswith("://"):
            # A shared in-memory database: every session sees the same schema and data.
            kwargs |= {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        engine = create_engine(url, **kwargs)
        _install_sqlite_pragmas(engine)
        return engine

    oracle_kwargs: dict[str, Any] = {
        "echo": echo,
        "future": True,
        "pool_size": settings.ORACLE_POOL_MIN or 1,
        "max_overflow": max(0, settings.ORACLE_POOL_MAX - (settings.ORACLE_POOL_MIN or 1)),
        "pool_pre_ping": True,  # a stale connection after a network blip must not surface
        "pool_recycle": 1800,
        "pool_timeout": 30,
    }
    if settings.ORACLE_WALLET_DIR:
        oracle_kwargs["connect_args"] = {
            "config_dir": settings.ORACLE_WALLET_DIR,
            "wallet_location": settings.ORACLE_WALLET_DIR,
        }
    engine = create_engine(_oracle_url(settings), **oracle_kwargs)
    _install_oracle_session_setup(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def _install_oracle_session_setup(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _session_params(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # Explicit NLS: never depend on the client's locale for dates or decimals.
            cursor.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'")
            cursor.execute(
                "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SSXFF TZR'",
            )
            cursor.execute("ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'")
            cursor.execute("ALTER SESSION SET TIME_ZONE = 'UTC'")
        finally:
            cursor.close()


#: One engine per (backend, target). Kept in a plain dict rather than an lru_cache so the
#: engines can be disposed properly when a test switches backends.
_ENGINES: dict[tuple[str, str], Engine] = {}


def get_engine(settings: Settings | None = None) -> Engine:
    """Process-wide engine for the configured backend."""
    settings = settings or get_settings()
    target = settings.sqlite_target if settings.DB_BACKEND == "sqlite" else settings.ORACLE_DSN
    key = (settings.DB_BACKEND, target)
    engine = _ENGINES.get(key)
    if engine is None:
        engine = build_engine(settings)
        _ENGINES[key] = engine
    return engine


def reset_engine_cache() -> None:
    """Test hook: dispose cached engines so a new backend or path takes effect."""
    while _ENGINES:
        _, engine = _ENGINES.popitem()
        engine.dispose()


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine or get_engine(),
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


@contextmanager
def session_scope(
    engine: Engine | None = None,
    *,
    settings: Settings | None = None,
    write: bool = False,
) -> Iterator[Session]:
    """Transactional scope. `write=True` is refused when writes are not allowed.

    In live mode the app schema is still writable but source data never is; the guard
    here is the last line of defence behind the read-only database user.
    """
    settings = settings or get_settings()
    if write and not settings.source_writes_allowed:
        raise ReadOnlyViolation(
            "This deployment runs in live mode with ALLOW_LIVE_WRITES=false.",
        )
    factory = session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection(settings: Settings | None = None) -> tuple[bool, str]:
    """Synchronous health check: one trivial query, nothing cached."""
    settings = settings or get_settings()
    probe = "select 1 from dual" if settings.DB_BACKEND == "oracle" else "select 1"
    try:
        engine = get_engine(settings)
        with engine.connect() as connection:
            connection.execute(text(probe)).scalar_one()
    except Exception as exc:
        _log.warning("db_unavailable", extra={"error": str(exc)})
        return False, f"{type(exc).__name__}: {exc}"[:200]
    else:
        return True, f"connected ({engine.dialect.name})"


async def ping(settings: Settings | None = None) -> tuple[bool, str]:
    """Async wrapper used by `/readyz` and `make doctor`."""
    return await asyncio.to_thread(check_connection, settings)


def require_connection(settings: Settings | None = None) -> None:
    ok, detail = check_connection(settings)
    if not ok:
        raise UpstreamUnavailable(f"Database unavailable: {detail}")


def dialect_name(settings: Settings | None = None) -> str:
    return get_engine(settings).dialect.name


def apply_statement_timeout(connection: Connection, seconds: int) -> None:
    """Best-effort per-statement timeout for explorer queries.

    SQLite has no server-side timeout, so the explorer also enforces a wall-clock budget
    in application code; Oracle gets a real resource limit.
    """
    if connection.dialect.name == "oracle":
        connection.exec_driver_sql(f"alter session set statement_timeout = {int(seconds) * 1000}")
