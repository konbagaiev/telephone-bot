"""Test fixtures.

Tests run against a real Postgres, not SQLite: a substitute engine diverges from
production exactly where storage bugs live (ADR-016). The schema is built by
running the migrations, so every test exercises them and the migration cannot
drift from `db.metadata` unnoticed.

Needs a local Postgres. Override the target with TEST_DATABASE_URL.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from src.config import load_config

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEST_URL = "postgresql+psycopg://localhost/telbot_test"


def _test_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)


def _ensure_database_exists(url: str) -> None:
    name = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        exists = conn.execute(
            text("select 1 from pg_database where datname = :name"), {"name": name}
        ).scalar()
        if not exists:
            conn.execute(text(f'create database "{name}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine():
    url = _test_url()
    _ensure_database_exists(url)

    alembic_cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)

    command.downgrade(alembic_cfg, "base")  # start from empty even after a failed run
    command.upgrade(alembic_cfg, "head")

    eng = create_engine(url, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    """A connection whose work is rolled back, so tests cannot see each other."""
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def example_config():
    return load_config(ROOT / "data" / "example")
