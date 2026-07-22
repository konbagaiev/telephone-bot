# Spec: Store the call transcript for debugging

> **Motivated by a live finding (2026-07-21).** With a TV playing in the
> background, the model recorded `raw = "Yeah, it came right on time, no delays."`
> for an answer the respondent never gave — `raw` is the model's *claim*, filled
> when it calls `record_answer`, not a transcript of what was said. Meanwhile the
> Realtime transcription events (ADR-011) that carry what was *actually* said are
> received in the bridge and thrown away. This spec captures and stores that
> transcript so a call is debuggable after the fact.

## Goal
Persist the Realtime transcription of each call — what the respondent actually
said (and, best-effort, what the agent said) — in Postgres, keyed by call, so
that after a call we can compare the real utterances against what the model chose
to record. A debugging record, not a product feature: it does **not** change how
answers are stored.

## Approach (why one change, not two)
The main risk is that the exact GA (gpt-realtime) event names are unconfirmed
(see risks). Rather than a separate "log the names on a smoke first, then build"
sequence, the confirmation is **folded into the implementation**: the bridge
handles the *likely* event names *and* logs any other `*transcript*` event it
sees. So the next live smoke is self-revealing — if the names are right the
transcript is stored; if one is wrong, its real name appears in the app log as an
"unhandled transcript event", a one-line fix. One change, no throwaway pass.

## Why a table (not a column or logs)
Ordered, role-tagged segments are the shape a transcript actually has, and the
roadmap's step-9 UI depends on reading them ("Read the transcript", `docs/plan.md`).
A single `transcript` text column on `calls` would need read-modify-write per
utterance and lose the per-segment role/order; logs alone are not queryable after
the container recycles. At one-call-at-a-time scale (ADR-013) the extra table is
cheap, and it is the surface the UI will read.

## Touches
- `src/models.py` — `TranscriptSegment` dataclass and a `TranscriptRole` enum
  (`respondent`, `agent`).
- `src/db.py` — a `transcript_segments` table (`call_id` FK → `calls` cascade,
  `role`, `text`, `recorded_at`) and `add_transcript_segment(...)` /
  `transcript_for(call_id)` queries.
- `migrations/0002_*` — create the table and its `transcript_role` enum (schema
  and `db.metadata` move together); `test_migrations` covers it.
- `src/bridge.py` — handle the transcription server-events and dispatch them to a
  new `on_transcript(role, text)` callback, where `role` is a bare string
  (`"respondent"` / `"agent"`) so the bridge stays dumb transport with no model
  import — the same seam shape as `on_tool_call`. **Also** log any unhandled
  `*transcript*` event type, so a mis-guessed GA name surfaces in the smoke log.
- `src/app.py` (`/stream`) — wire `on_transcript` to a DB write in its own short
  transaction, mapping the role string to `TranscriptRole`; pass it into
  `run_bridge`. The write must not break the call (see risks).
- `src/agent/session.py` — input transcription is already configured under
  `session.audio.input.transcription` (whisper-1). The agent's output transcript
  is emitted by GA alongside the audio modality; no session change needed.
- `tests/` — bridge dispatch test (synthetic input- and output-transcription
  events → callback with the right role + text; a non-transcript event dispatches
  nothing) and a storage test (`add_transcript_segment` writes and
  `transcript_for` reads back in order, against real Postgres).
- `docs/architecture.md` — module-map row + a line in the call path.

## Does NOT touch
- **`record_answer` / `raw` / `value` semantics** — unchanged. This spec *adds* a
  transcript beside the answers; it does **not** repoint `raw` at the transcript
  or reconcile the two. That is a separate, larger decision (the `raw`-trust
  finding) and must not be smuggled in here.
- **No call audio** — ADR-014 stands; we store text, never audio.
- **No UI, no real-time display, no analytics** over the transcript.
- **The other live findings** — greet-first not firing, background noise ending
  the call — are separate items, not this one.

## Acceptance criteria
Offline (`.venv/bin/python -m pytest`, real Postgres per ADR-016):
- The bridge, given a synthetic input-transcription event, invokes `on_transcript`
  with `role = "respondent"` and the transcribed text; an output-transcription
  event yields `role = "agent"`. A non-transcript event invokes nothing.
- `add_transcript_segment` writes a row tied to a call; `transcript_for` returns
  the segments in insertion order. The migration builds the table (conftest runs
  migrations, so this is exercised every run).
- Existing audio-relay and tool-dispatch tests still pass (the new event handling
  is additive; the pump gains an `on_transcript` parameter).

Live smoke (on demand):
- After a call, `transcript_segments` holds the respondent's transcribed speech
  (the criterion), and the agent's if its GA event name was guessed right — so we
  can line it up against what `record_answer` stored, the exact comparison the
  finding needed. If a name was wrong, the app log shows the real one.

## What could go wrong (risks & guards)
- **Risk: wrong GA event names** — nothing gets captured. The guessed names are
  `conversation.item.input_audio_transcription.completed` (respondent) and
  `response.output_audio_transcript.done` (agent). → Guard: the bridge logs any
  unhandled `*transcript*` event, so a wrong name surfaces loudly in the smoke log
  instead of failing silently; a bridge test pins the names we handle.
- **Risk: transcript order ≠ utterance order.** whisper input transcription is
  async and can land *after* the `record_answer` it describes. → Not a bug to
  guard against but part of what the record reveals: segments are ordered by
  insertion id and stamped with `recorded_at` (wall-clock at write), which is what
  makes the divergence from the recorded answer visible in the first place.
- **Risk: extra DB writes on the call path.** → Guard: transcription events fire
  per utterance, not per audio frame, so volume is low; each is its own short
  transaction. Acceptable at one-call-at-a-time scale (ADR-013).
- **Risk: a transcript write failing aborts the call.** → Guard: a transcript is a
  debug aid, not the call's purpose — `on_transcript` swallows and logs its own
  errors so finalize and answers never depend on it.
- **Risk: migration in the deploy path (ADR-016) breaks a deploy.** → Guard:
  conftest builds the schema by running migrations, so a broken migration fails CI
  before deploy.

## Non-goals
- Not making the transcript the source of truth for answers (raw stays as-is).
- Not retaining audio (ADR-014).
- Not building any view or export of the transcript yet.

## Status
Implemented and **live-confirmed (2026-07-22, call 7)**. Realises the storage half
of ADR-011 (transcript from the Realtime API), which was configured but never
persisted. The live smoke stored both roles, so the guessed GA event names —
`conversation.item.input_audio_transcription.completed` (respondent) and
`response.output_audio_transcript.done` (agent) — are both **confirmed**; no
unhandled `*transcript*` event was logged. The record also did its job: it showed
a whisper mis-transcription of the respondent diverging from the correct `raw` the
model recorded — the exact "raw is the model's claim" divergence it was built to
surface (plan step 5).
