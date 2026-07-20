# Spec: Load a local .env for configuration

> A gap found while reviewing `db.py`: the code reads `DATABASE_URL` from the
> environment but nothing loads a `.env` file, so the standard "keep it in
> `.env`" workflow does not actually work yet.

## Goal

Let local development configure the application through a git-ignored `.env`
file, without changing how production is configured (real environment variables,
per ADR-015). Ship a committed `.env.example` so a newcomer knows which variables
to set.

## Touches

- `pyproject.toml` — add `python-dotenv`.
- `src/config.py` or a small `src/env.py` — load `.env` once, early, if present.
- `.env.example` — a committed template with no secrets.
- `docs/architecture.md` — note where configuration comes from.

## Does NOT touch

- The data model, the schema, telephony.
- How secrets reach production. GitHub Actions secrets and the VPS environment
  stay the source there (ADR-015); `.env` is a local-only convenience.
- `DEFAULT_DATABASE_URL` stays passwordless — it is the local fallback, never a
  place for credentials.

## Design notes

- `.env` is already in `.gitignore`; this spec makes it actually take effect.
- Loading is **best-effort and non-overriding**: if a real environment variable
  is already set (production, CI), the `.env` file must not clobber it. This is
  `load_dotenv(override=False)`, the default — state it so it is not "fixed"
  later by accident.
- Load **once, at the edge** — as the application/CLI starts — not inside library
  functions. A library function reading the environment is fine; a library
  function with the side effect of reading a file off disk is not.
- `.env.example` lists variable *names* and harmless example values only. It is
  documentation, and it is the thing that must never contain a real secret.

## Acceptance criteria

- With a `.env` containing `DATABASE_URL=...`, starting the app (or invoking the
  load helper) makes `database_url()` return that value.
- With `DATABASE_URL` exported in the real environment *and* a different value in
  `.env`, the environment wins — `.env` does not override it.
- With no `.env` present, nothing fails; `database_url()` falls back to
  `DEFAULT_DATABASE_URL` exactly as today.
- `.env.example` exists, is committed, and contains no real credentials.
- `pytest` still passes with no network access.

## What could go wrong (risks & guards)

- **Risk:** `.env` silently overrides a real production/CI environment variable,
  so a deploy quietly talks to the wrong database.
  **Guard:** Test that an existing environment variable wins over `.env`
  (`override=False`).

- **Risk:** A real `.env` gets committed and leaks credentials.
  **Guard:** `.env` is in `.gitignore` (already); only `.env.example` is tracked.
  Verify `git status` does not see `.env`.

- **Risk:** A library function loads `.env` as an import side effect, so tests and
  tools pick up a developer's local config unexpectedly.
  **Guard:** Loading happens only from an explicit entry-point call, not at
  import time. Tests continue to use `TEST_DATABASE_URL` and do not depend on
  `.env`.

## Non-goals

- No secret management beyond a local file and environment variables. No vault,
  no encryption at rest.
- No new configuration keys. This is about *how* configuration is delivered, not
  *what* is configured.
