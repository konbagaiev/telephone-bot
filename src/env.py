"""Local configuration via a git-ignored .env file.

A convenience for development only. In production, configuration arrives through
real environment variables (ADR-015); this must never override them.

Call `load_local_env()` once, at the edge — when the application or CLI starts —
never from inside a library function. A library that reads a file off disk as an
import side effect would pull a developer's local config into tests and tools
unexpectedly.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Project root: this file is src/env.py, so two parents up.
_ROOT = Path(__file__).resolve().parent.parent


def load_local_env() -> None:
    """Load `.env` from the project root if it exists.

    `override=False`: a variable already set in the real environment wins, so a
    stray `.env` cannot redirect a production or CI process to the wrong place. A
    missing `.env` is not an error — configuration simply falls back to the
    environment and to the code defaults.
    """
    load_dotenv(_ROOT / ".env", override=False)
