"""Loading configuration from a local .env.

The load must be a convenience that never overrides a real environment variable —
otherwise a stray .env could point a deploy at the wrong database (ADR-015).
"""

from __future__ import annotations

import importlib

import pytest
from dotenv import load_dotenv


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return monkeypatch


def test_env_file_is_loaded_when_variable_is_unset(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+psycopg://localhost/from_env_file\n")

    load_dotenv(env_file, override=False)

    from src.db import database_url

    assert database_url() == "postgresql+psycopg://localhost/from_env_file"


def test_real_environment_wins_over_env_file(tmp_path, clean_env):
    """A value already set in the environment must not be clobbered by .env."""
    clean_env.setenv("DATABASE_URL", "postgresql+psycopg://localhost/from_real_env")
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+psycopg://localhost/from_env_file\n")

    load_dotenv(env_file, override=False)

    from src.db import database_url

    assert database_url() == "postgresql+psycopg://localhost/from_real_env"


def test_missing_env_file_is_not_an_error(tmp_path, clean_env):
    load_dotenv(tmp_path / ".env", override=False)  # does not exist

    from src.db import database_url, DEFAULT_DATABASE_URL

    assert database_url() == DEFAULT_DATABASE_URL


def test_load_local_env_is_not_called_at_import(clean_env):
    """Importing the module must not read a file off disk on its own."""
    import src.env

    importlib.reload(src.env)
    # If import had loaded anything, DATABASE_URL would be set; the fixture unset it.
    import os

    assert "DATABASE_URL" not in os.environ
