# Spec: Centralise config into a typed Settings object

> Follow-up to the vertical slice. Reviewing it surfaced scattered
> `os.environ["…"]` reads across `app.py`, `runner.py`, and `db.py` — a mild
> smell: untyped, unvalidated, and read from a global mid-handler. Replace them
> with one typed settings object read at the edge, symmetrical with how
> `config.py` already loads YAML into validated Pydantic models.

## Goal
Introduce `src/settings.py` — a `pydantic_settings.BaseSettings` object that reads
all process configuration from the environment (and, locally, from `.env`) into
typed, validated fields. Every raw `os.environ[…]` read for a runtime key is
replaced by a field access. A missing required key fails with a Pydantic error
naming the field, not a bare `KeyError` — the same "name the file and field"
philosophy as `ConfigError`. No behaviour changes; this is a structure change.

This keeps ADR-015's principle intact — configuration still reaches the process
through the environment — and only changes its in-process representation. It does
not need a new ADR; it is an implementation refinement, recorded as a convention
in `architecture.md`.

## Touches
- `src/settings.py` — new. `Settings(BaseSettings)` with `env_file=".env"`:
  `database_url` (passwordless default), `twilio_account_sid`,
  `twilio_auth_token`, `twilio_phone_number`, `openai_api_key`,
  `openai_realtime_model` (default `gpt-realtime`), `public_base_url`,
  `config_dir` (default `data/example`). Required fields have no default, so a
  misconfigured process fails fast.
- `src/app.py` — build a `Settings()` at each entry (the `/voice` request, the
  `/stream` connection); drop `_public_base_url`, `_config_dir`, `_realtime_url`
  reading `os.environ` and the raw `TWILIO_AUTH_TOKEN` / `OPENAI_API_KEY` reads.
- `src/runner.py` — build `Settings()` once in `main()`; drop `_config_dir`,
  `_carrier_from_env`'s raw reads, and the `PUBLIC_BASE_URL` read.
- `src/db.py` — `create_db_engine(url)` takes its URL from the caller (which
  passes `settings.database_url`); remove `database_url()` and the
  `os.environ["DATABASE_URL"]` read. `DEFAULT_DATABASE_URL` moves to Settings.
- `src/env.py` — **removed**. `pydantic-settings` reads `.env` directly, so
  `load_local_env()` / `python-dotenv` are no longer needed.
- `pyproject.toml` — add `pydantic-settings`; remove `python-dotenv`.
- `tests/test_env.py` — removed; `tests/test_settings.py` — new (see Acceptance).
- `tests/test_voice_webhook.py` — its env fixture now sets the full required set
  (building `Settings` needs all required keys present).
- `docs/architecture.md` — the "Configuration reaches the process through the
  environment" convention updated to name `settings.py`; module map row.

## Does NOT touch
- **`tests/conftest.py`** — it builds the test engine from `TEST_DATABASE_URL`
  with SQLAlchemy directly, independent of Settings. `TEST_DATABASE_URL` stays a
  test-only concern, out of the app Settings.
- **Behaviour** — no endpoint, tool, bridge, or DB semantics change. Same keys,
  same values, same precedence (a real env var still beats `.env`).
- **The carrier / bridge / tools modules** — they already receive config as
  arguments; only who *reads* it changes, not their signatures.
- **No new configuration keys**, and no secret manager (that stays a possible
  future step beyond the `.env.prod`/`update_env` workflow).

## Acceptance criteria
`.venv/bin/python -m pytest` green, `ruff` clean, and:
- **`test_settings.py`** — with a full env set, `Settings()` exposes each key with
  the right type; a missing required key (e.g. `PUBLIC_BASE_URL`) raises a
  `ValidationError` naming that field; a real env var overrides a `.env` value;
  defaults apply for `openai_realtime_model` and `config_dir` when unset.
- **No raw runtime `os.environ` reads remain**: `grep -rn 'os.environ' src/`
  returns nothing for the migrated keys (only test/infra reads, if any, remain).
- **`import src.app` still succeeds with no secrets set** — Settings is built
  inside handlers, never at import, so the suite imports the app as before.
- The existing webhook, tool, bridge, and runner tests still pass unchanged in
  intent (only `test_voice_webhook`'s env fixture grows).

## What could go wrong (risks & guards)
- **Risk: building `Settings()` at import (module level) would crash every test
  that imports `app`/`runner` without secrets set** — required fields raise on
  construction. → Guard: construct only at the edges (per request/connection/CLI
  run), never at import; `test_app`'s `/health` needs no Settings and proves the
  import stays cheap.
- **Risk: dropping `load_local_env` means `.env` is no longer loaded into
  `os.environ`, so any lingering `os.environ` read (notably `DATABASE_URL` in
  `db.py`) silently stops seeing `.env` locally.** → Guard: route `db` through
  `settings.database_url`; the "no raw `os.environ`" grep check above catches a
  missed reader.
- **Risk: field/env-name mismatch** (e.g. `public_base_url` not mapping to
  `PUBLIC_BASE_URL`) ships a process that can't find its config. → Guard:
  `test_settings.py` asserts each field reads from its expected env var.

## Non-goals
- Not caching Settings in a global/singleton — per-edge construction is cheap and
  avoids stale-config surprises in tests.
- Not unifying `TEST_DATABASE_URL` or conftest into Settings.
- Not changing deployment, the `.env.prod`/`update_env` flow, or `.env.example`
  (the documented keys are unchanged).
