# Spec: Alembic takes its database URL explicitly; conftest stops mutating the environment

> A code smell found while reviewing the tests: `conftest.py` sets
> `os.environ["DATABASE_URL"]` so that Alembic will pick up the test database.
> That routes the URL through global process state instead of passing it, and the
> session fixture never restores it.

## Goal

Let a caller hand Alembic its database URL directly, so the test suite can point
migrations at the test database without writing to `os.environ`. Remove the
environment mutation from `conftest.py`.

## Touches

- `migrations/env.py` — prefer an explicitly-provided `sqlalchemy.url` on the
  Alembic config; fall back to `database_url()` (the environment) only when none
  was given.
- `tests/conftest.py` — the `engine` fixture passes the test URL via
  `alembic_cfg.set_main_option("sqlalchemy.url", url)` and no longer touches
  `os.environ`.

## Does NOT touch

- `src/db.py`'s `database_url()` — the environment stays the production source of
  the URL (ADR-015). This spec only stops *tests* from injecting through it.
- Application/runtime behaviour. This is test wiring and one Alembic-config read.
- `tests/test_migrations.py` — its own environment handling is a separate spec
  (`2026-07-20-2041-test-migrations-env-restore.md`). If that spec is implemented
  first and already added the `env.py` change below, keep it; do not duplicate.

## Design notes

The coupling to fix: `migrations/env.py` currently does, unconditionally,

```python
config.set_main_option("sqlalchemy.url", database_url())
```

so the *only* channel for the URL is `database_url()`, which reads the
environment. That forces `conftest.py` to shove the test URL into `os.environ`.

Make `env.py` honour a URL already set on its config:

```python
url = config.get_main_option("sqlalchemy.url") or database_url()
config.set_main_option("sqlalchemy.url", url)
```

Then a caller that sets `sqlalchemy.url` on the `AlembicConfig` object controls
the target directly, and a caller that sets nothing still falls back to the
environment exactly as production does. No behaviour changes for production —
nothing sets `sqlalchemy.url` there, so it still comes from `database_url()`.

`conftest.py` already builds `create_engine(url)` with an explicit URL, so only
the Alembic path needs the change.

## Acceptance criteria

- `conftest.py` contains no `os.environ[...] = ...`.
- With `sqlalchemy.url` set on the Alembic config, `upgrade`/`downgrade` run
  against that URL; with it unset, they use `database_url()` (the environment) as
  before.
- The full suite still passes against a local Postgres, with no network access.
- Running the suite leaves `os.environ` unchanged from before it ran (no
  `DATABASE_URL` added or altered by the fixtures).

## What could go wrong (risks & guards)

- **Risk:** `env.py` now reads a config value that is empty-string rather than
  `None`, so the `or` fallback misfires and migrations run against an empty URL.
  **Guard:** Test that with no `sqlalchemy.url` provided, `database_url()` is
  still used (an assertion in the migrations test, or a small unit test on the
  resolution logic).

- **Risk:** Production migrations silently change target because something now
  sets `sqlalchemy.url` unexpectedly.
  **Guard:** Nothing in the deploy sets it; `alembic.ini` ships it empty. Confirm
  by reading `alembic.ini` and the deploy step — accepted by inspection, no test.

## Non-goals

- No change to how production is configured.
- No switch to `monkeypatch` — the point is to stop touching the environment at
  all, not to auto-restore a mutation.
