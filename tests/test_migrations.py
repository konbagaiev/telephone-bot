"""Migrations run from an empty database and reverse cleanly.

Guards the failure where a migration passes locally against a hand-built schema
and then fails on the VPS from empty (ADR-015/ADR-016). Uses its own database so
it cannot disturb the schema the other tests run against.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from tests.conftest import ROOT, _ensure_database_exists, _test_url

EXPECTED_TABLES = {"persons", "assignments", "calls", "answers", "transcript_segments"}


@pytest.fixture
def migration_url():
    url = _test_url().rsplit("/", 1)[0] + "/vividi_migrations_test"
    _ensure_database_exists(url)
    yield url


def _alembic(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_shipped_config_leaves_url_empty_so_env_falls_back():
    """env.py resolves `sqlalchemy.url or database_url()`. Both migration tests
    now set the URL explicitly, so nothing else exercises the fallback branch
    that production relies on. Guard the documented risk: the URL shipped in
    alembic.ini must stay empty/falsy, or the `or` misfires and production
    migrations would run against an empty URL instead of DATABASE_URL.
    """
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    assert not cfg.get_main_option("sqlalchemy.url")


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
                    "('assignment_status', 'call_disposition', 'call_end_reason', "
                    "'transcript_role')"
                )
            ).scalars().all()
            assert leftover_types == []

        command.upgrade(cfg, "head")  # a second upgrade must still work
    finally:
        target.dispose()
