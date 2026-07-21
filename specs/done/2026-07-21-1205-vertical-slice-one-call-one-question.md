# Spec: Vertical slice — one live call, one question

> Roadmap step 4 (`docs/plan.md`). The first change where the server actually
> answers a phone. Built as one vertical slice — the telephony path and the audio
> bridge are not independent deliverables (one is inert without the other) — but
> with three independently testable seams (carrier, bridge relay, tool handling)
> so failure modes stay isolated during development.

## Goal
Place one outbound call to a verified number and ask **one** question end to end:
Twilio dials, our webhook returns TwiML that connects the Media Stream to our
WebSocket, we bridge audio to an OpenAI Realtime session, the model asks the
question, and when it calls `record_answer` the answer lands in Postgres and
completion is recomputed. Proves the latency-sensitive audio path on a real line,
which behaves differently than any offline harness (ADR-002, ADR-003).

## Touches
- `src/telephony/__init__.py` — new. Narrow carrier `Protocol`: `place_call`,
  `hang_up`, and inbound-webhook signature validation. No Twilio type leaks past
  it (ADR-004).
- `src/telephony/twilio.py` — new. Twilio adapter: REST `calls.create`,
  `RequestValidator` for `X-Twilio-Signature`, TwiML generation.
- `src/bridge.py` — new. The Media Streams ↔ Realtime relay: two async tasks
  passing base64 μ-law frames through unchanged, plus barge-in `clear`.
- `src/agent/session.py` — new. Realtime `session.update` payload (instructions
  from the question intent, `g711_ulaw` in/out, server VAD) and the JSON-Schema
  tool definitions for `record_answer` / `end_call`.
- `src/agent/tools.py` — new. Handles a `function_call` event → validates
  `question_id`, writes via `db.record_answer`, recomputes completion; `end_call`
  finishes the call (ADR-002).
- `src/runner.py` — new. `python -m src.runner`: pick one pending assignment,
  `validate_references`, `start_call`, `carrier.place_call`. Config is loaded
  **per call**, not at import (edit-a-question-takes-effect-next-call).
- `src/app.py` — grows `POST /voice` (returns TwiML, validates signature) and
  `WS /stream` (hands the socket to the bridge). Keeps `/health`.
- `pyproject.toml` — add `twilio` and `websockets`.
- `.env.example` — add `OPENAI_API_KEY`, `TWILIO_*`, `PUBLIC_BASE_URL` (values in
  `.env`/secrets only, ADR-015).
- `tests/` — new tests for each seam (see Acceptance).
- `docs/architecture.md` — the shape changes, so update it in this same change.

## Does NOT touch
- **Full questionnaire logic** — one question only. Multi-question flow,
  required-answer completion across many questions, and `Assignment.status`
  transitions beyond this single write are roadmap step 5.
- **Policy enforcement** — retries, calling window, timeouts, voicemail, opt-out
  are step 6. Policy values are read but not acted on.
- **Multilingual** — English only here (step 7).
- **Drain-aware restart** — still deferred (ADR-017); ADR-013 makes a plain
  restart between calls acceptable for now.
- `src/config.py`, `src/models.py`, `src/db.py` schema — reused as-is, no schema
  change (all needed columns already exist).
- **O7** (is the bridge still warranted) — this slice is the prerequisite for
  answering it, not the place to answer it.

## Acceptance criteria
The suite never touches the network (AGENTS.md); the live call is a manual smoke,
not a test.

Automated (`.venv/bin/python -m pytest`, real Postgres per ADR-016):
- **Signature validation** — a request with a valid `X-Twilio-Signature` passes;
  a tampered one is rejected. Pure, deterministic.
- **TwiML** — `POST /voice` returns XML containing the configured
  `wss://…/stream` URL inside `<Connect><Stream>`.
- **Bridge relay** — driven by two in-memory fake sockets: a Twilio `media` frame
  arrives at the Realtime side unchanged; a Realtime `response.audio.delta`
  arrives at the Twilio side as a `media` frame with the right `streamSid`; a
  `speech_started` event sends a `clear` to Twilio.
- **Tool handling** (the primary surface, AGENTS.md) — injecting a synthetic
  `record_answer` function-call event writes the answer to Postgres; an unknown
  `question_id` is rejected without a write; a second answer to the same question
  replaces the first; `end_call` with no answers leaves the assignment `partial`,
  and after the one required answer it is `completed` — completion is computed,
  never taken from the model (ADR-002).
- **Runner** — placing a call invokes `carrier.place_call` (via `FakeCarrier`,
  no network) and writes a `Call` row with the returned carrier id; config is
  re-read on each run.

Manual smoke / eval (on demand, not in CI):
- One real call from the Twilio dev number (ADR-018) to a verified destination:
  the agent asks the question, the answer appears in Postgres, the call ends
  cleanly.

## What could go wrong (risks & guards)
- **Risk: the public stream URL disagrees between the app config and the Twilio
  number** — a silent failure, calls never reach `/stream` (ADR-015). → Guard:
  the URL is one config value read in both the TwiML we emit and the runner; the
  TwiML test asserts the emitted URL, and the manual smoke fails loudly if they
  diverge (no automated guard can compare against Twilio's stored setting).
- **Risk: a forged POST to the public `/voice`** drives a call leg. → Guard:
  signature validation with a rejecting test; unsigned/forged requests get 403.
- **Risk: tool-call handling writes a bad or unvalidated answer** (unknown
  question id, duplicate, model calling `end_call` early marks it done). → Guard:
  the tool-handling tests above; completion is recomputed from the DB, so an
  early `end_call` yields `partial`, not `completed`.
- **Risk: audio latency on the real line** makes the agent feel unnatural
  (ADR-002). → Guard: not unit-testable; the manual smoke is the check, and the
  μ-law formats match so no transcoding hop is added.

## Non-goals
- Not building a queue or concurrency (ADR-013 — one call at a time).
- Not storing call audio (ADR-014 — transcript only, from Realtime, ADR-011).
- Not a UI, not multiple questions, not policy behaviour — all later roadmap
  steps.
