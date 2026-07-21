# Spec: The call is always finalised on teardown

> **Finding — first live-smoke run of the vertical slice (roadmap step 4).**
> On the first full conversation (call 2) the agent recorded the answer and
> called `end_call`; our handler closed the Realtime socket, and the still-running
> Twilio→Realtime pump then tried to send one more frame to it, raising
> `websockets.ConnectionClosedOK`. `run_bridge` re-raised it, so the `finalize(…)`
> block after the bridge never ran: the `Call` row got no disposition/end_reason/
> `ended_at`, the assignment stayed `pending`, and uvicorn logged an ASGI ERROR.
> The answer was written; only the wind-down was lost.

## Goal
On **every** normal end of a call — the model calls `end_call`, the caller hangs
up, or the Realtime socket closes cleanly — the bridge winds down without raising,
and `finalize` always runs so the `Call` gets its disposition and `end_reason` and
the assignment's completion is recomputed (ADR-002). No stray ASGI error in the
logs for an ordinary hang-up.

## Touches
- `src/bridge.py` — the pumps tolerate a peer that has already closed: a
  connection-closed error while sending (or the source ending) makes a pump
  **return** rather than raise, so `run_bridge` ends normally instead of
  propagating a teardown-race exception. `run_bridge` treats "one side ended" as
  the expected stop, not an error.
- `src/app.py` (`/stream`) — `finalize` runs on every exit path (clean close,
  caller hang-up, or unexpected error): wrap the bridge in `try/finally`, and
  treat the expected close exceptions (Starlette `WebSocketDisconnect`, websockets
  `ConnectionClosed`) as a normal end, not an ASGI error.
- `tests/test_bridge.py` — new offline cases (see Acceptance).

## Does NOT touch
- **What `finalize` computes** — `finish_call` + `refresh_completion` are correct;
  the bug is that finalize was not *reached*, not that it is wrong.
- **The placement→`in_progress` gap** (roadmap step 5). That is a *different* root
  cause — a call in flight leaves the assignment re-pickable — and this fix does
  not address it. Here we only ensure the *end* of a call is recorded.
- **The GA session/event shape** (separate finding) and the tool-handling logic.
- **Drain-aware restart** (ADR-017) — still deferred.

## Acceptance criteria
Offline (`.venv/bin/python -m pytest`, no network):
- A pump whose sink raises a connection-closed error mid-send **returns without
  raising** (the exact first-run failure: sending to a Realtime socket the
  `end_call` handler just closed).
- `run_bridge` **completes without raising** when one side closes during teardown
  (the surviving pump is cancelled, the finished one's close is not re-raised).

Live smoke (on demand):
- A full call where the agent records the answer and calls `end_call` ends with
  the `Call` row carrying `disposition=answered`, `end_reason=agent_completed`,
  `ended_at` set, and the assignment recomputed to `completed` — and **no ERROR
  traceback** in the container logs.

## What could go wrong (risks & guards)
- **Risk: swallowing a connection-closed error hides a real mid-call failure**
  (e.g. Realtime dies abnormally). → Guard: an abnormal close ends the call
  regardless — it cannot continue without Realtime — and `finalize` still records
  it honestly as `remote_ended` (ADR-005). Acceptable; we lose no truth.
- **Risk: `finalize` runs twice or on a call that never connected.** → Guard:
  structure the `/stream` teardown so `finalize` runs exactly once on exit; a call
  that never established Realtime is finalised as `remote_ended`, which is
  accurate.
- **Risk: the two socket libraries raise different close types** (Starlette
  `WebSocketDisconnect` vs websockets `ConnectionClosed`) and one is missed. →
  Guard: handle both at the `/stream` boundary; the offline test covers the
  `ConnectionClosed` (Realtime) path, the live smoke covers the caller-hang-up
  (`WebSocketDisconnect`) path — `/stream` itself stays live-only by design.

## Non-goals
- Not the placement→`in_progress` transition (step 5).
- Not drain-aware restart (ADR-017).
- Not changing `finalize`'s computed result or the disposition/end_reason meanings
  (ADR-005).

## Status
Done. Offline acceptance met (54 green, ruff clean) and **verified live**: call 3
ended via `end_call` with `disposition=answered`, `end_reason=agent_completed`,
`ended_at` set, the assignment recomputed to `completed`, and no ERROR in the
logs. This also unblocked the vertical-slice spec (its live smoke is now green).
