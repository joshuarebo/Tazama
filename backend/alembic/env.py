"""Alembic environment. The URL comes from settings, never from alembic.ini."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from tazama.core.settings import get_settings
from tazama.infrastructure.db import models  # noqa: F401 - registers every table
from tazama.infrastructure.db.base import Base
from tazama.infrastructure.db.engine import build_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001, ARG001
    """Keep migrations to our own tables (Oracle schemas carry plenty of others)."""
    if type_ == "table":
        return name in target_metadata.tables
    return True


def run_migrations_offline() -> None:
    settings = get_settings()
    url = (
        f"sqlite+pysqlite:///{settings.sqlite_target}"
        if settings.DB_BACKEND == "sqlite"
        else f"oracle+oracledb://{settings.ORACLE_USER}@{settings.ORACLE_DSN}"
    )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(get_settings())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object,
            # SQLite cannot ALTER most things; batch mode rewrites the table instead.
            render_as_batch=connection.dialect.name == "sqlite",
            poolclass=pool.NullPool,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
