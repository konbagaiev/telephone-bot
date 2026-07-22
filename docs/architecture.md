# Architecture — current state

> Describes how the system is built **right now**. Update this in the *same*
> change that alters the architecture. Describe shape (modules, flow), not
> line-level detail — the code is the source of truth for details.

**Status: data layer + deploy skeleton + vertical slice (one call, one
question).** The questionnaire, the people, and the results exist; a FastAPI
service is deployed to the VPS behind Traefik by a GitHub Actions pipeline; and
the whole call path is wired — a runner places an outbound call, the `/voice`
webhook returns TwiML that streams the audio to `/stream`, and the bridge relays
it to an OpenAI Realtime session whose `record_answer` tool call is written to
Postgres. The live call itself is verified by a manual smoke, not in CI: Realtime
behaves differently on a phone line than in any offline harness (roadmap step 4).
Debugging the live behaviour on a real line, multiple questions, policy
enforcement, and multilingual operation are step 5 and beyond (`docs/plan.md`).

## Shape

Two storage engines, split by the kind of data rather than by convenience
(ADR-016):

- **Configuration — in YAML, in git.** Questionnaires and policy. Changing a
  question is a reviewable diff.
- **Operational data — in Postgres.** People, assignments, calls, answers, and
  the per-call transcript. Personal data, never in the repository, queryable.

The boundary is the same one `.gitignore` draws: what is tracked is safe to
publish.

## Module map

| Module | Holds |
|---|---|
| `src/config.py` | `Question`, `Questionnaire`, `Policy`; YAML loading; `ConfigError` naming file and field |
| `src/models.py` | `Person`, `Assignment`, `Call`, `Answer`, `TranscriptSegment`; the status/role enums; phone normalisation; `completion_status()` |
| `src/db.py` | Postgres schema (SQLAlchemy Core), connections, queries |
| `src/env.py` | `load_local_env()` — loads a git-ignored `.env` for local dev, never overriding real env vars |
| `src/telephony/` | The carrier boundary (ADR-004): `Carrier` Protocol in `__init__`, Twilio adapter in `twilio.py` (REST dial, signature validation, TwiML). No Twilio type leaks past it |
| `src/agent/` | `session.py` — Realtime `session.update` and the tool definitions; `tools.py` — turning a tool call into a write (the primary test surface). The model owns speech, this owns facts (ADR-002) |
| `src/bridge.py` | The Media Streams ↔ Realtime relay (ADR-003): pure frame translations plus two async pumps; barge-in `clear`; dispatches tool calls and transcription events (ADR-011) out to callbacks |
| `src/runner.py` | `python -m src.runner` — place one call for the next pending assignment (ADR-013). Config is read per run |
| `src/app.py` | FastAPI ASGI app: `GET /health`, `POST /voice` (TwiML + signature), `WS /stream` (the live bridge) |
| `migrations/` | Alembic; `0001_initial` creates the four tables, `0002` adds `transcript_segments` |
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

## Call path

One call, end to end:

1. **Runner** (`src/runner.py`) takes the next pending assignment (ADR-013),
   creates the `Call` row, and asks the carrier to dial. The answer URL it hands
   the carrier names the call: `…/voice?call_id=<id>`.
2. **`POST /voice`** validates the Twilio signature (against the *public* URL,
   reconstructed from `PUBLIC_BASE_URL` — behind Traefik the container's own URL
   differs) and returns TwiML: `<Connect><Stream>` pointed at `/stream`, carrying
   `call_id` as a `<Parameter>`. No `<Say>`/`<Play>` — the model owns speech.
3. **`WS /stream`** reads the stream's `start` event for `call_id`, loads the
   assignment and its (one) question, opens the OpenAI Realtime socket, and sends
   `session.update` followed by `response.create` so the agent greets first. This
   is the **GA (`gpt-realtime`) API shape**, not the beta one (a live-smoke
   finding): `session.type = "realtime"`, audio config under
   `session.audio.input/output` with format `audio/pcmu` (= G.711 μ-law), server
   VAD and transcription under `audio.input`, and no `OpenAI-Beta` header.
4. **The bridge** (`src/bridge.py`) relays μ-law frames unchanged in both
   directions and flushes Twilio on barge-in. Both sides speak G.711 μ-law 8 kHz,
   so no transcoding hop is added (ADR-003). GA server events: output audio is
   `response.output_audio.delta`; a tool call arrives inside `response.done` as a
   `response.output[]` item of `type == "function_call"`. Transcription events
   (ADR-011) are dispatched to an `on_transcript` callback that `/stream` writes to
   `transcript_segments` — a debug record of what was actually said, isolated so a
   failed write never breaks the call.
5. **A tool call** (`src/agent/tools.py`) is written to Postgres: `record_answer`
   after validating the question id, `end_call` to wind up. On teardown —
   reached on every exit path via a `finally`, even when a socket closes mid-flush
   — completion is recomputed from the answers on record; an early `end_call`
   leaves the assignment `partial`, never `completed` (ADR-002).

The seams are the carrier (a `Protocol`, faked in tests), the bridge pumps (fake
sockets), and tool handling (synthetic tool-call events against real Postgres).
`WS /stream` itself is the only unverified-in-CI piece — it wires the tested
parts around two live sockets, and the manual smoke is its check.

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
| What the agent is told / may do | `instructions_for()` and the tool defs in `src/agent/session.py` |
| How a tool call becomes data | `src/agent/tools.py` (`handle_tool_call`, `finalize`) |
| The carrier (add a provider) | a new class satisfying `Carrier` in `src/telephony/`; nothing else changes |

**Last verified against commit:** the teardown-finalise fix (roadmap step 4). The
vertical slice's live smoke passed on 2026-07-21 — a real call recorded an answer
and finalised to `completed`, over the GA Realtime API.
