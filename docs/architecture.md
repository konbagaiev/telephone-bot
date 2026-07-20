# Architecture — current state

> Describes how the system is built **right now**. Update this in the *same*
> change that alters the architecture. Describe shape (modules, flow), not
> line-level detail — the code is the source of truth for details.

**Status: data layer + deploy skeleton.** The questionnaire, the people, and the
results exist, and a minimal FastAPI service (only `/health`) is deployed to the
VPS behind Traefik by a GitHub Actions pipeline. Nothing places a call yet —
telephony and the Realtime session are roadmap step 4 and beyond (`docs/plan.md`).

## Shape

Two storage engines, split by the kind of data rather than by convenience
(ADR-016):

- **Configuration — in YAML, in git.** Questionnaires and policy. Changing a
  question is a reviewable diff.
- **Operational data — in Postgres.** People, assignments, calls, answers.
  Personal data, never in the repository, queryable.

The boundary is the same one `.gitignore` draws: what is tracked is safe to
publish.

## Module map

| Module | Holds |
|---|---|
| `src/config.py` | `Question`, `Questionnaire`, `Policy`; YAML loading; `ConfigError` naming file and field |
| `src/models.py` | `Person`, `Assignment`, `Call`, `Answer`; the status enums; phone normalisation; `completion_status()` |
| `src/db.py` | Postgres schema (SQLAlchemy Core), connections, queries |
| `src/env.py` | `load_local_env()` — loads a git-ignored `.env` for local dev, never overriding real env vars |
| `src/app.py` | FastAPI ASGI app; `GET /health`. The Twilio webhook and Realtime WebSocket bridge join here in step 4 |
| `migrations/` | Alembic; `0001_initial` creates the four tables |
| `Dockerfile`, `docker-compose.yml` | The app image and its service, behind Traefik on `phone-bot.bagaiev.com`, using the shared Postgres (ADR-017) |
| `.github/workflows/deploy.yml` | On push to `main`: test → pull → migrate → recreate container → health check |
| `data/example/` | A fictional questionnaire and policy |
| `tests/` | Unit tests; `conftest.py` builds the test schema by running migrations |

## Conventions

**A question is an intent, not a script.** Wording belongs to the model
(ADR-002); `phrasing` is a per-language override for the rare case where exact
words matter.

**Completion is computed, never stored as a claim.** `completion_status()` is a
pure function of the questionnaire and the answers on record. The model calling
`end_call` does not make an assignment complete.

**Phone numbers are E.164 everywhere.** `normalise_phone()` on the way in, a
unique constraint behind it.

**Three independent status fields**, not one — see ADR-005. Do not collapse them.

**Configuration references are validated up front.** `validate_references()`
catches an assignment pointing at a questionnaire id that no longer exists in
YAML — no foreign key can span the two engines, so this must be called before
calls are placed.

**Configuration reaches the process through the environment.** `DATABASE_URL`
(and `TEST_DATABASE_URL`) are read from the environment. Locally, `load_local_env()`
loads a git-ignored `.env` without overriding anything already set; in production
the real environment is the source (ADR-015). Credentials never live in the code
default (`DEFAULT_DATABASE_URL` is passwordless) nor in git. Copy `.env.example`
to `.env` to start.

## Deployment

On every push to `main`, GitHub Actions runs the suite and, on green, SSHes to
the VPS to pull, run migrations, and recreate the container (ADR-017). The app
runs as a Docker container fronted by the existing Traefik at
`https://phone-bot.bagaiev.com` (TLS via `letsencrypt`), and reaches the server's
shared Postgres — a dedicated `vividi` database — over the `backend` network. The
deploy ends by health-checking the public URL. Secrets live on the VPS and in
GitHub Actions, never in git (ADR-015). A restart recreates the container, so
drain-aware restart is still owed once a live call has state to drain (ADR-017).

## Testing

Tests run against a real Postgres, not SQLite (ADR-016). The test schema is built
by running the migrations, so every run exercises them and the migration cannot
drift from `db.metadata` unnoticed. Tests never touch the network.

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # needs a local Postgres
.venv/bin/python -m ruff check .
```

The suite creates `vividi_test` (and `vividi_migrations_test`) if absent.
Override with `TEST_DATABASE_URL`. Each test runs in a transaction that is rolled
back, so tests cannot see each other.

## Where to make a change

| To change… | Edit |
|---|---|
| The questions asked | `data/example/questionnaires.yaml` — no code change |
| Edge-case behaviour values | `data/example/policy.yaml` — the *value* only; a new *kind* of behaviour is code plus a test |
| What "finished" means | `completion_status()` in `src/models.py` |
| The stored shape of anything | `src/db.py` **and** a new migration — the two must move together |

**Last verified against commit:** the commit that added the deploy pipeline and
skeleton service (roadmap step 3).
