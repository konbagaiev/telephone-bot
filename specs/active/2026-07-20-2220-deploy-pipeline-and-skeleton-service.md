# Spec: Deploy pipeline and skeleton service

> Roadmap step 3. Stands up the whole deploy path against a minimal Dockerised
> service, so the deployment risk is verified before the latency-sensitive audio
> work (ADR-017). The vertical slice (step 4) later fills this service with the
> Twilio webhook and the Realtime bridge.

## Goal
A push to `main` runs the tests, and on green updates the VPS: pull, migrate,
rebuild and recreate the container, health-check. The result is reachable at
`https://phone-bot.bagaiev.com/health`, proving DNS → Traefik → container end to
end.

## Context (fixed facts)
- Repo `github.com/konbagaiev/telephone-bot` is **public** — the server pulls
  over HTTPS with no credential.
- Server `178.104.91.144` (root) already runs **Traefik** fronting its other
  projects as **Docker** containers; `phone-bot.bagaiev.com` resolves to it.
- Runtime deps are declared in `pyproject.toml`. ADR-015 (VPS + Actions deploy),
  ADR-016 (migrations on deploy), ADR-017 (Docker behind Traefik) govern this.
- The service will grow into the Twilio webhook + Realtime **WebSocket** bridge
  (step 4), so the framework chosen here must do async HTTP **and** WebSocket.

## Decisions taken in this spec
- **Web framework: FastAPI + uvicorn.** ASGI with native WebSocket (needed for
  the media bridge, ADR-003); Pydantic is already a dependency. Overkill for a
  health endpoint, chosen now so step 4 does not swap frameworks.
- **Own Postgres as a compose `db` service.** The app reaches `db` over the
  compose network with the `vividi` credentials; keeps the project self-contained
  rather than coupling to how the VPS's other Postgres is wired. ADR-016 asks only
  that Postgres run on the VPS, which a compose service satisfies.
- **Migrations run inside the image** (`docker compose run --rm app alembic
  upgrade head`), so the host needs only Docker + git — no venv, no host Python.

## Touches (new files)
- `src/app.py` — FastAPI `app`, `GET /health` → `{"status": "ok"}` (200).
- `Dockerfile` — install the package, run
  `uvicorn src.app:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml` — `app` (Traefik labels: router on host
  `phone-bot.bagaiev.com`, TLS via the existing resolver, service port 8000,
  joined to the Traefik network) + `db` (Postgres, named volume, `vividi` creds
  from `.env`).
- `.github/workflows/deploy.yml` — job **test** (Postgres service + `pytest`)
  gating job **deploy** (raw `ssh` → pull → build → migrate → `up -d` → health).
- `pyproject.toml` — add `fastapi`, `uvicorn[standard]`.
- **GitHub repo secrets:** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`,
  `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`.
- **Server one-time bootstrap** (manual, documented here): `git clone` into a
  folder beside the other projects; Docker present; a git-ignored `.env` with
  `DATABASE_URL` (→ the `db` service) and the Twilio/OpenAI secrets (ADR-015); a
  dedicated CI deploy key in `authorized_keys`; confirm the Traefik docker
  network name.

## Deploy mechanism
Two jobs. `test` runs the suite against a throwaway Postgres service; `deploy`
runs only on green, over raw `ssh` (no marketplace action holding a key to a root
server). The remote script is deterministic:

    cd "$DEPLOY_PATH"
    git fetch --prune origin && git reset --hard origin/main
    docker compose build
    docker compose run --rm app alembic upgrade head
    docker compose up -d
    curl -fsS http://localhost:8000/health      # non-zero on unhealthy → run fails red

A `concurrency` group serialises deploys. A failed build or migration exits
before `up -d`, so the previously running container keeps serving.

## Acceptance criteria
- Given the secrets are set and the server is bootstrapped,
- When a commit is pushed to `main`,
- Then the `test` job runs `pytest` green, `deploy` succeeds, and:
  - `curl https://phone-bot.bagaiev.com/health` returns 200 `{"status":"ok"}`
    over TLS (proves DNS → Traefik → container),
  - on the server `git rev-parse HEAD` equals the pushed SHA, `alembic current`
    reports head, and the `app` container is running.
- And a red test **blocks** the deploy; a failed build/migration fails the run
  **without** replacing the running container.

## What could go wrong (risks & guards)
- **Risk:** broken code or a broken migration reaches the server. → **Guard:** the
  CI `test` job gates the deploy; migrations are transactional on Postgres; a
  failed build/migrate aborts before `up -d`, leaving the old container up; the
  health check fails the run red.
- **Risk:** Traefik / certificate / routing misconfigured — container up but
  unreachable. → **Guard:** acceptance checks the **public** HTTPS URL, not just
  `localhost`.
- **Risk:** the CI SSH key leaks or a supply-chain action abuses it against a root
  server. → **Guard:** dedicated deploy key (revocable independently of `do_cs`),
  host key pinned via `DEPLOY_KNOWN_HOSTS`, raw `ssh`, no third-party action.
- **Risk:** overlapping deploys race the reset or recreate. → **Guard:**
  `concurrency: { group: deploy, cancel-in-progress: false }`.
- **Assumption:** the VPS `.env` is simple `KEY=value` (compose `env_file`);
  Docker and the Traefik docker network already exist on the host.

## Non-goals (later steps, per ADR-017)
- The Twilio webhook, signature validation, and the Realtime WebSocket bridge
  (step 4) — this service answers only `/health`.
- Setting the Twilio number's webhook/stream URLs to the public base URL (step 4).
- Drain-aware restart — there is no call state to drain yet; a recreate between
  calls is acceptable under ADR-013.
- Any UI or questionnaire logic.
