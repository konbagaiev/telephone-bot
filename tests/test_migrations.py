"""Migrations run from an empty database and reverse cleanly.

Guards the failure where a migration passes locally against a hand-built schema
and then fails on the VPS from empty (ADR-015/ADR-016). Uses its own database so
it cannot disturb the schema the other tests run against.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from tests.conftest import ROOT, _ensure_database_exists, _test_url

EXPECTED_TABLES = {"persons", "assignments", "calls", "answers"}


@pytest.fixture
def migration_url():
    url = _test_url().rsplit("/", 1)[0] + "/telbot_migrations_test"
    _ensure_database_exists(url)
    yield url


def _alembic(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    os.environ["DATABASE_URL"] = url
    return cfg


def test_upgrade_from_empty_then_downgrade(migration_url, engine):
    cfg = _alembic(migration_url)
    target = create_engine(migration_url, future=True)

    try:
        command.downgrade(cfg, "base")

        with target.connect() as conn:
            assert EXPECTED_TABLES & set(inspect(conn).get_table_names()) == set()

        command.upgrade(cfg, "head")

        with target.connect() as conn:
            assert EXPECTED_TABLES <= set(inspect(conn).get_table_names())

        command.downgrade(cfg, "base")

        with target.connect() as conn:
            remaining = set(inspect(conn).get_table_names()) - {"alembic_version"}
            assert remaining == set()
            # The enum types must go too, or a re-upgrade fails with "type
            # already exists" on a database that was downgraded.
            leftover_types = conn.execute(
                text(
                    "select typname from pg_type where typname in "
                    "('assignment_status', 'call_disposition', 'call_end_reason')"
                )
            ).scalars().all()
            assert leftover_types == []

        command.upgrade(cfg, "head")  # a second upgrade must still work
    finally:
        target.dispose()
        os.environ["DATABASE_URL"] = _test_url()
