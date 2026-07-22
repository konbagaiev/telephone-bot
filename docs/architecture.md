# Architecture — current state

> Describes how the system is built **right now**. Update this in the *same*
> change that alters the architecture. Describe shape (modules, flow), not
> line-level detail — the code is the source of truth for details.

**Status: data layer + deploy skeleton + a call that asks the whole
questionnaire + a token-gated admin UI.** The questionnaire, the people, and the
results exist; a FastAPI
service is deployed to the VPS behind Traefik by a GitHub Actions pipeline; and
the whole call path is wired — a runner places an outbound call (and marks the
assignment in-flight), the `/voice` webhook returns TwiML that streams the audio
to `/stream`, and the bridge relays it to an OpenAI Realtime session that asks
every question in order and writes each `record_answer` to Postgres. A minimal
`/ui` admin surface (ADR-023) now manages people/assignments, launches the next
pending call, and shows the answers and stored transcript. A policy seam
(`src/policy.py`) enforces two policies (ADR-024/ADR-025): retry-on-disconnect
(the runner redials on an escalating schedule; a never-answered call is detected
via the `/call_status` webhook) and an opt-in refusal-reason probe (`record_refusal`
stores a declined marker). The live
call itself is verified by a manual smoke, not in CI: Realtime behaves differently
on a phone line than in any offline harness. The rest of the policy set (calling
window, timeouts, voicemail, opt-out) is parked, and multilingual operation is step
8; the messy real-line conditions (background noise, drops) are deferred to the
edge-case step (`docs/plan.md`).

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
| `src/models.py` | `Person`, `Assignment`, `Call`, `Answer` (incl. the `declined`/`refusal_reason` refusal marker), `TranscriptSegment`; the status/role enums; phone normalisation; `completion_status()` |
| `src/db.py` | Postgres schema (SQLAlchemy Core), connections, queries; `select_next_to_call` applies the retry policy, `record_refusal`/`record_pre_answer_outcome` back the two new policies |
| `src/policy.py` | The policy-enforcement seam (ADR-024): pure `retry_decision`/`terminal_status` over `Policy` + calls + an injected clock. No I/O — the DB scan in `db.py` is the only writer |
| `src/env.py` | `load_local_env()` — loads a git-ignored `.env` for local dev, never overriding real env vars |
| `src/telephony/` | The carrier boundary (ADR-004): `Carrier` Protocol in `__init__`, Twilio adapter in `twilio.py` (REST dial, signature validation, TwiML). No Twilio type leaks past it |
| `src/agent/` | `session.py` — Realtime `session.update` and the tool definitions; `tools.py` — turning a tool call into a write (the primary test surface). The model owns speech, this owns facts (ADR-002) |
| `src/bridge.py` | The Media Streams ↔ Realtime relay (ADR-003): pure frame translations plus two async pumps; barge-in `clear`; an end-of-call `mark` so an agent-ended call drains Twilio playback before closing; dispatches tool calls and transcription events (ADR-011) out to callbacks |
| `src/runner.py` | `python -m src.runner` — place one call for the next assignment the policy calls for (ADR-013/ADR-024). Config is read per run. `place_next_call` is the shared entry the CLI and the UI's "Call next" both use |
| `src/app.py` | FastAPI ASGI app: `GET /health`, `POST /voice` (TwiML + signature), `POST /call_status` (Twilio status callback → disposition, ADR-024), `WS /stream` (the live bridge), and the token-gated `/ui` admin router (ADR-023) |
| `src/templates/` | Jinja templates for the admin UI: `index.html`, `assignment.html`, `transcript.html`. Self-contained, no external assets (ADR-023) |
| `migrations/` | Alembic; `0001_initial` creates the four tables, `0002` adds `transcript_segments`, `0003` adds the `answers` refusal columns |
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

**Policy is data with a code branch per value (ADR-007), and the branch lives in
`src/policy.py`.** A `Policy` value must map to a pure decision there; if it needs a
condition or a formula it should have been code. Retry keys on *how the last call
ended* (`end_reason`/`disposition`, ADR-024), never on a hangup-cause the carrier
does not give us. Only the enforced values live in the `Policy` model; parked ones
are commented in `policy.yaml` until they earn a branch, so nothing parses a value
it does not act on.

**The admin UI is gated; the webhooks are not.** The `/ui` router carries a
single-token dependency (`UI_TOKEN`, in the URL then a cookie, ADR-023). `/voice`,
`/stream`, and `/health` must stay off that gate — Twilio authenticates by
signature (ADR-004), and a token on `/voice` would reject every real call. Add new
operator surfaces under the `/ui` router so they inherit the gate; add new
carrier-facing endpoints outside it.

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

1. **Runner** (`src/runner.py`) asks `db.select_next_to_call` which assignment to
   dial — a fresh `pending` one, or one due for a retry under the policy
   (`policy.retry_decision`, ADR-024) — creates the `Call` row, and asks the carrier
   to dial. The answer URL names the call (`…/voice?call_id=<id>`); a
   `…/call_status?call_id=<id>` URL goes alongside it so the carrier reports the
   call's final status. Placement moves the assignment `pending → in_progress` in
   the same transaction, so a second run does not re-pick an in-flight call (its
   open `Call` row makes the policy `WAIT`) and dial the person twice. A call that
   is never answered is recorded by **`POST /call_status`** (disposition
   `no_answer`/`busy`/`failed`), which makes it eligible for a retry; a connected
   call is left to `/stream` teardown. Exhausting the retry schedule lands the
   assignment `unreachable` (never connected) or `partial` (connected, incomplete).
2. **`POST /voice`** validates the Twilio signature (against the *public* URL,
   reconstructed from `PUBLIC_BASE_URL` — behind Traefik the container's own URL
   differs) and returns TwiML: `<Connect><Stream>` pointed at `/stream`, carrying
   `call_id` as a `<Parameter>`. No `<Say>`/`<Play>` — the model owns speech.
3. **`WS /stream`** reads the stream's `start` event for `call_id`, loads the
   assignment and its questionnaire, opens the OpenAI Realtime socket, and sends
   `session.update` followed by `response.create` so the agent greets first. The
   session instructions carry every question in order; the model asks them all,
   records each answer as it comes, and says goodbye before `end_call` (ADR-002).
   This
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
   after validating the question id, `record_refusal` (offered only when the
   refusal-reason policy is on — ADR-025) to store a declined marker + reason,
   `end_call` to wind up. When the **agent** ends
   the call, teardown first drains Twilio playback — it sends an end-of-call `mark`
   and waits (bounded) for Twilio to echo it, plus a short grace — so the agent's
   goodbye is heard, not clipped, before the sockets close. On teardown —
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
| Retry cadence / which outcomes retry | `retry_delays_minutes` in `policy.yaml` (value); the decision itself in `src/policy.py` (ADR-024) |
| What "finished" means | `completion_status()` in `src/models.py` (declined answers excluded via `db.answered_question_ids`) |
| The stored shape of anything | `src/db.py` **and** a new migration — the two must move together |
| What the agent is told / may do | `instructions_for()` and the tool defs in `src/agent/session.py` (the refusal clause + `record_refusal` tool are gated on `probe_refusal_reason`) |
| How a tool call becomes data | `src/agent/tools.py` (`handle_tool_call`, `_record_refusal`, `finalize`) |
| The carrier (add a provider) | a new class satisfying `Carrier` in `src/telephony/`; nothing else changes |
| The admin UI (pages, forms) | the `/ui` router in `src/app.py` and the templates in `src/templates/` (ADR-023) |

**Last verified against commit:** the policy skeleton (roadmap step 7,
ADR-024/ADR-025) — retry-on-disconnect and the refusal-reason probe — offline only:
full suite green (103 tests), `ruff` clean. Retry firing on the live line waits on
a scheduler (O9) and is not yet smoked. It sits on the admin UI (step 9, ADR-023),
deployed to `phone-bot.bagaiev.com` on 2026-07-22 and verified in prod — the gate
behaves (`/ui` 401 without the token, 200 with it, `/voice` still 403-by-signature,
so the token never reached the webhook) and the panel works. It sits on the
full-questionnaire slice (step 6), live-smoked on call 7: a real call asked both
questions in order, recorded both answers, moved the assignment
`in_progress → completed`, stored the transcript for both roles, and let the agent
finish its goodbye before hanging up — over the GA Realtime API.
