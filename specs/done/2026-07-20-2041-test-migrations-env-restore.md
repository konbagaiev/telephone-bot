# Spec: test_migrations.py stops mutating the environment (and mis-restoring it)

> A correctness bug found alongside the `conftest.py` smell: the migrations test
> also writes `os.environ["DATABASE_URL"]`, and its "restore" in the `finally`
> sets the variable to the *test* URL rather than to whatever was there before —
> which is usually nothing at all.

## Goal

Make `tests/test_migrations.py` point Alembic at its dedicated migrations
database without writing to `os.environ`, removing both the mutation and the
incorrect restore.

## Touches

- `tests/test_migrations.py` — the `_alembic` helper and the `finally` block. Pass
  the URL via `alembic_cfg.set_main_option("sqlalchemy.url", url)`; drop the
  `os.environ` writes and the restore entirely.

## Does NOT touch

- `tests/conftest.py` — handled by the sibling spec
  (`2026-07-20-2041-alembic-url-via-config.md`).
- Application/runtime behaviour.

## Design notes

Two defects live here today:

1. `os.environ["DATABASE_URL"] = url` in `_alembic` — same coupling as the
   `conftest.py` spec: the URL is injected through global state.
2. The `finally` restores with `os.environ["DATABASE_URL"] = _test_url()`. That
   is wrong twice over: it writes the *test suite's* URL, not the value present
   before the test, and if `DATABASE_URL` was unset to begin with (the normal
   case) the correct action is to **delete** it, not set it.

The fix is the same mechanism as the sibling spec — hand Alembic the URL through
its config object:

```python
alembic_cfg.set_main_option("sqlalchemy.url", migration_url)
```

**Dependency.** This requires `migrations/env.py` to honour an explicitly-set
`sqlalchemy.url` (preferring it over `database_url()`). That change is the core
of the sibling spec. If the sibling spec has already landed, reuse it. If this
spec is implemented first, add the `env.py` change here — see the sibling spec's
Design notes for the exact edit — and the sibling spec then simply finds it done.

`create_engine(migration_url)` in the test is already explicit, so once the
Alembic path is fixed there is nothing left that needs the environment, and the
whole `try/finally`-around-`os.environ` structure goes away.

## Acceptance criteria

- `tests/test_migrations.py` contains no `os.environ[...] = ...`.
- The migrations test still runs `upgrade`/`downgrade` against its own
  `telbot_migrations_test` database and passes.
- After the test runs, `os.environ` is unchanged from before it ran — in
  particular, `DATABASE_URL` is not left set to the test URL.
- The full suite passes against a local Postgres, with no network access.

## What could go wrong (risks & guards)

- **Risk:** Removing the `finally` restore leaves some later test depending on
  `DATABASE_URL` being set as a side effect of this one.
  **Guard:** No test should rely on that; the suite already gets its URL from
  `TEST_DATABASE_URL`/`database_url()` independently. Confirm the suite passes in
  a fresh process where `DATABASE_URL` is unset.

- **Risk:** The migrations test and the `conftest` engine fixture race on a
  shared Alembic config or database.
  **Guard:** They already use different databases (`telbot_migrations_test` vs
  `telbot_test`) and separate `AlembicConfig` objects; keep them separate.

## Non-goals

- No change to `conftest.py` (sibling spec).
- No `monkeypatch`; the goal is to not touch the environment at all.
