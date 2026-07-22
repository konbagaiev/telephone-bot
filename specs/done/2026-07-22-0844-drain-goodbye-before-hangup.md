# Spec: Let the agent finish its goodbye before hanging up

> **Motivated by a live finding (2026-07-22).** On a real call the respondent
> declined the last question; the agent said "no problem" and began to say
> goodbye, but the line dropped mid-phrase. Cause: `end_call` closes the Realtime
> socket immediately, and the bridge tears down and closes the Twilio socket —
> while Twilio still has 1–2s of the goodbye buffered but not yet played. The
> agent's closing words are cut.

## Goal
When the **agent** ends the call (`end_call`), wait until the respondent has
actually heard the goodbye before closing the sockets: send a Twilio `mark` after
the last audio frame, wait for Twilio to echo it back (playback drained), add a
short grace pause, then close. A bounded timeout guarantees we still hang up if
the mark never returns (e.g. the respondent already hung up). This does not change
how answers or completion are computed — only *when* the call is torn down.

## Approach — playback drain, not generation-done
The Realtime side finishes *generating* the goodbye well before Twilio finishes
*playing* it (the model streams audio faster than real time, and Twilio buffers
it). So "the agent stopped emitting audio" is the wrong signal — closing on it
still clips playback. The reliable signal that the caller has heard the whole
goodbye is Twilio's own **`mark`** mechanism: after the last media frame we send a
`mark`, and Twilio echoes a `mark` event back once it has played everything queued
up to it. We wait for that echo (bounded), then a 0.5s grace, then close.

This only applies to the **agent-initiated** end (`end_call`). If the respondent
hangs up or the line drops (`REMOTE_ENDED`), there is nothing to drain and the
existing teardown stands.

## Touches
- `src/bridge.py`
  - `END_OF_CALL_MARK = "end-of-call"` — the mark name we send and match.
  - `end_of_call_mark(stream_sid, name)` — pure translation to the Twilio
    `{"event": "mark", "streamSid": …, "mark": {"name": name}}` frame (sibling of
    `agent_audio_to_respondent` / `interrupt_agent_playback`).
  - `BridgeState.playback_drained: asyncio.Event` (default factory) — set when the
    end-of-call mark echo is seen.
  - `pump_twilio_to_realtime` — handle `event == "mark"`: if
    `mark.name == END_OF_CALL_MARK`, `state.playback_drained.set()`. (Other marks
    ignored.) Twilio sends `mark` on the same inbound socket the pump already reads.
  - `await_playback_drained(drained, timeout) -> bool` — `asyncio.wait_for` on the
    event; returns `True` if drained, `False` on timeout. The one piece with
    branching worth a unit test; the sleep and socket close stay in `app.py`.
- `src/app.py` (`/stream`, `on_tool_call`)
  - On `result.ended`: if we have a `stream_sid`, send `end_of_call_mark` through
    the Twilio sink (it orders after the buffered goodbye), `await
    await_playback_drained(state.playback_drained, DRAIN_TIMEOUT)` (~3s), then
    `await asyncio.sleep(GOODBYE_GRACE)` (0.5s), then `await realtime.close()`.
    Without a `stream_sid`, close as before.
  - **Drop the `response.create` on the ended path** — today `on_tool_call` sends a
    `function_call_output` *and* a `response.create` before closing, asking the
    model to speak again just to cut it off. Send the `function_call_output`, then:
    if not ended → `response.create` (unchanged); if ended → drain-and-close.
- `tests/test_bridge.py`
  - `end_of_call_mark` builds the right frame.
  - `pump_twilio_to_realtime` sets `state.playback_drained` on an end-of-call mark
    event, and does **not** on a mark with a different name.
  - `await_playback_drained` returns `True` when the event is set, `False` after a
    short timeout when it is not.
- `docs/architecture.md` — call-path step 5 / bridge row: agent-initiated teardown
  drains Twilio playback (mark + grace) before closing, so the goodbye is not cut.
- `docs/plan.md` — record the live finding under the debug/edge-case steps.

## Does NOT touch
- **`record_answer` / completion / `finalize`** — unchanged; this is teardown
  timing only. Completion is still computed at teardown (ADR-002).
- **Respondent-initiated end / drops (`REMOTE_ENDED`)** — no drain; the socket is
  already going. Unexpected mid-call drops remain a step-10 concern.
- **Barge-in (`clear`)** — unchanged; a separate mechanism from the end mark.
- **The Realtime session config / instructions** — the model still says goodbye
  then calls `end_call`; we only stop cutting it off.

## Acceptance criteria
`.venv/bin/python -m pytest` green, `ruff` clean, and:
- **Offline** — `end_of_call_mark` frame shape asserted; the twilio→realtime pump
  sets `playback_drained` only for the end-of-call mark name; `await_playback_drained`
  returns `True` on a set event and `False` on timeout. Existing bridge audio-relay,
  barge-in, tool-dispatch, and transcript tests still pass (the pump gains one
  `mark` branch; the audio path is unchanged).
- **Live smoke (on demand)** — the agent finishes its goodbye and *then* the call
  ends; the closing words are no longer clipped, including on the decline path that
  surfaced the finding.

## What could go wrong (risks & guards)
- **Risk: the mark never echoes** (respondent already hung up, or Twilio drops the
  socket) → the drain would hang. → Guard: `await_playback_drained` is bounded by
  `DRAIN_TIMEOUT`; on timeout we close anyway. Unit-tested on the timeout branch.
- **Risk: blocking `on_tool_call` while awaiting the mark stalls the bridge.** →
  Guard: the two pumps are separate tasks — the Twilio→Realtime pump keeps reading
  and is exactly what sets the event; the await lives in the Realtime pump, which
  has no more work to do once the call is ending. Bounded by the timeout regardless.
- **Risk: mark sent before the last audio frame, so Twilio echoes early and we
  still clip.** → Guard: the goodbye audio deltas all arrive *before* the
  `response.done` that carries `end_call`, and both the audio and the mark are sent
  from the same task (the Realtime pump), so the mark is enqueued after the audio;
  Twilio echoes it only after playing what precedes it.

## Non-goals
- Not a fixed-delay heuristic (it clips a longer goodbye — the drain is
  mark-driven, with the pause only as grace on top).
- Not draining on a respondent hang-up or a mid-call drop (nothing to drain).
- Not drain-aware *restart* (ADR-017 — a container recycle mid-call is separate).
- Not a new ADR: this is correct call termination realising ADR-002 (let the model
  finish speaking), not a new fork.

## Status
Implemented (2026-07-22). Offline-verified: full suite green (68 tests), `ruff`
clean — the mark translation, the pump's mark→`playback_drained` dispatch (and its
non-match), and `await_playback_drained`'s drained/timeout branches are unit-tested.
The end-to-end effect (the goodbye actually heard before hang-up, on the decline
path that surfaced it) is confirmed by the next live smoke — the `/stream`
orchestration itself is the untested-in-CI seam, as ever.
