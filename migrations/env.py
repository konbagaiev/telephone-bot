"""Alembic environment.

The database URL always comes from DATABASE_URL, never from alembic.ini —
credentials do not belong in the repository (ADR-015).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.db import database_url, metadata

config = context.config
# Prefer a URL a caller set explicitly on the config (e.g. the test suite);
# fall back to DATABASE_URL for production, which sets nothing here (ADR-015).
url = config.get_main_option("sqlalchemy.url") or database_url()
config.set_main_option("sqlalchemy.url", url)

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
