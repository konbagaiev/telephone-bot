# Spec: Realtime session uses the GA API shape, not the beta one

> **Finding — first live-smoke run of the vertical slice (roadmap step 4).**
> The slice's Realtime code was written against **outdated documentation** (the
> beta `realtime=v1` API shape). On the first live call the OpenAI socket rejected
> our `session.update` with `4000 invalid_request_error.beta_api_shape_disabled`,
> because the model `gpt-realtime` is GA and the beta shape is disabled for it.
> This spec records the migration to the GA contract.

## Goal
Move the Realtime session and event handling from the beta API shape to the GA
(`gpt-realtime`) shape, so a live call establishes the session and the model can
speak, listen, and call tools. No change to *what* the agent does — only to the
wire contract with OpenAI.

## Touches
- `src/agent/session.py` — `session_update` rebuilt to the GA shape:
  `session.type = "realtime"`, audio config under `session.audio.input/output`,
  format `audio/pcmu` (G.711 μ-law, unchanged on the wire), `turn_detection` and
  `transcription` under `audio.input`, `output_modalities: ["audio"]`, `voice`
  under `audio.output`.
- `src/bridge.py` — GA server-event names in `pump_realtime_to_twilio`:
  output audio is `response.output_audio.delta` (was `response.audio.delta`); a
  tool call now arrives inside `response.done` as an item of `response.output[]`
  with `type == "function_call"` (was the standalone
  `response.function_call_arguments.done`).
- `src/app.py` — drop the `OpenAI-Beta: realtime=v1` connection header (GA needs
  none).
- `tests/test_bridge.py` — updated to the GA event names and the `response.done`
  tool-call shape; added a case for `response.done` with no function call.

## Does NOT touch
- **The tool definitions** (`RECORD_ANSWER_TOOL`, `END_CALL_TOOL`) — their shape
  is unchanged between beta and GA.
- **`instructions_for`** — the agent's behaviour and wording are untouched.
- **Twilio side** — μ-law frames are relayed exactly as before; `audio/pcmu` *is*
  the same G.711 μ-law, so nothing about the bridge's transcode-free relay
  changes (ADR-003).
- **Teardown handling** — the clean-close bug the same run surfaced is a separate
  finding/spec, not this one.

## Acceptance criteria
- A live call no longer gets `beta_api_shape_disabled`; the Realtime session is
  accepted and the agent speaks. **Verified on the first run's retry (call 2):
  the session established and `record_answer` wrote an answer to Postgres.**
- `tests/test_bridge.py` passes against the GA event names (offline, no network).
- Full suite green, ruff clean.

## What could go wrong (risks & guards)
- **Risk: GA field/event drift** — a wrong key in `session.update` silently
  degrades the call (e.g. transcription placement under `audio.input`). → Guard:
  the live smoke is the check; the bridge tests pin the event names we depend on.
  Transcription is not on the critical path (answers come via tool calls), so a
  bad transcription key would not block the smoke — worth re-verifying against the
  transcript once the call completes cleanly (ADR-011).
- **Risk: `gpt-realtime` alias vs a pinned version** (`gpt-realtime-2.1`) behaving
  differently. → Guard: the model id is env config (`OPENAI_REALTIME_MODEL`),
  swappable without a code change.

## Non-goals
- Not adding new agent behaviour, tools, or questions.
- Not fixing call teardown / `finalize` (separate finding).
- Not pinning a specific dated model version — the GA alias stays in env config.

## Status
Implemented and deployed in commit `e82dbcd`; verified by the first run's retry.
Recorded here after the fact — this fix shipped during the live smoke before its
spec existed. GA session shape to be noted as a fact in `docs/architecture.md`
(not an ADR: the API simply *is* GA, not a fork between viable options).
