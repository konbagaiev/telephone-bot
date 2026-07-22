# Spec: Policy skeleton — retry-on-disconnect and refusal-reason

> Roadmap step 7 (policy), taken as a *framework* proof rather than the whole step:
> stand up one enforcement seam and wire two policies through it end to end — retry
> on a broken/unanswered call, and probing the reason for a per-question refusal.
> The remaining policy values (calling window, call/silence timeouts, voicemail,
> opt-out) are deliberately parked — commented in YAML, removed from the model —
> because each needs live-line debugging that belongs to steps 5/10, not to
> proving the skeleton. Policy is loaded and validated today (`config.py`) but
> **nothing enforces it**; this change makes two values actually bite.

## Goal
Introduce a policy-enforcement seam (`src/policy.py`, pure decision functions over
`Policy` + operational facts + an injected clock) and drive two policies through
it:

1. **Retry on disconnect.** A call that dropped, was hung up on, or was never
   answered is re-dialled on an escalating schedule (`[0, 2, 60]` minutes:
   immediately, +2 min, +1 h). A call the agent wound up itself (the respondent
   declined the whole thing, or the questionnaire completed) is **not** retried.
   Retries exhausted → the assignment lands `unreachable` (never connected) or
   `partial` (connected but incomplete).
2. **Refusal reason.** An opt-in behaviour: when on, the agent asks *once* why a
   question is declined and records the reason as first-class data; when off (the
   default) behaviour is unchanged from step 6 (accept, move on, record nothing).

## Why these two, and why keyed on `end_reason`
ADR-005 already anticipated retries: *"`end_reason != agent_completed` together
with `status == partial` … is sufficient to drive retries."* This change realises
that. The three respondent outcomes map cleanly onto the fields we already record,
which is what lets "hung up → retry" and "said no → leave them alone" be told
apart without any new carrier signal:

| What happened | Recorded as | Retry? |
|---|---|---|
| Hung up / line dropped | `end_reason = remote_ended` | ✅ |
| Didn't pick up / busy | `disposition = no_answer` / `busy` (never connected) | ✅ |
| Declined the call; agent said goodbye | `end_reason = agent_completed` | ❌ |
| Questionnaire completed | `status = completed` (terminal) | ❌ |

The two policies also exercise **both** enforcement surfaces: retry lives in the
runner's selection, refusal-probing lives in the session instructions and tools
(ADR-002 — the model owns the speech, code owns the fact). That is the point of a
skeleton: prove the seam reaches everywhere policy will eventually need to reach.

## Approach

### The seam — `src/policy.py`
Pure functions, no I/O, clock injected as a `now` argument so tests are
deterministic:

- `retry_decision(policy, calls, now) -> RetryDecision` — given an assignment's
  `Call` rows in order and the current time, return one of `DIAL_NOW`, `WAIT`,
  `EXHAUSTED`, `STOP`:
  - no calls, or last call still open (`ended_at is None`) → `WAIT` (an attempt is
    pending; never dial over a live/unresolved call — ADR-013).
  - last outcome **not** retriable (`end_reason == agent_completed`) → `STOP`.
  - `attempts >= 1 + len(retry_delays_minutes)` → `EXHAUSTED`.
  - otherwise due at `last.ended_at + retry_delays_minutes[attempts - 1]`:
    `now >= due` → `DIAL_NOW`, else `WAIT`.
  - retriable outcomes: `end_reason in {remote_ended, agent_error}` or
    `disposition in {no_answer, busy, carrier_failed}`.
- `terminal_status(calls) -> AssignmentStatus` — `unreachable` if no call ever
  connected (all pre-answer failures), else `partial`. Used to label an
  `EXHAUSTED`/`STOP` assignment.

The runner is the only writer; `policy.py` decides, the runner acts.

### Retry in the runner
Replace `db.next_pending_assignment` with a policy-driven scan
(`db.select_next_to_call` + `policy.retry_decision`), still one call per run
(ADR-013):

- Candidates: assignments **not** in a final-terminal state (`!= completed`;
  opt-out is out of scope). Ordered by id (deterministic).
- For each candidate: `pending` → dial (first attempt); otherwise apply
  `retry_decision`:
  - `DIAL_NOW` → this is the one; place the call (existing
    `place_call_for_assignment`, which already sets `in_progress` and creates the
    `Call` row).
  - `EXHAUSTED` / `STOP` → set the assignment to `terminal_status(calls)` and move
    on (so it leaves the candidate pool and is visible in reporting).
  - `WAIT` → skip (not yet due, or an attempt is in flight).
- Attempts = count of `Call` rows for the assignment (a row is created at every
  placement, including calls that never connect).

### "Didn't pick up" — the `/call_status` webhook
A never-answered call currently leaves the assignment stuck `in_progress` with an
open `Call` row forever (the step-6 finding); nothing tells us it failed. Twilio's
**status callback** is the honest signal (ADR-004 lists these pre-answer outcomes
as distinguishable). Add a minimal callback:

- `place_call` gains a `status_callback_url` argument on the `Carrier` protocol and
  the Twilio adapter (passed to `calls.create` as `status_callback`, with
  `status_callback_event=["completed"]` so the final outcome is delivered). This
  extends the deliberately-minimal carrier boundary — ADR-004 says the rest of
  Twilio's surface stays out "until a later roadmap step needs them"; step 7 needs
  this one.
- The runner passes `{base}/call_status?call_id={call.id}`.
- `POST /call_status` (in `app.py`): validate the Twilio signature (same
  reconstruct-the-signed-URL path as `/voice`), map `CallStatus` →
  `Disposition` (`no-answer`→`no_answer`, `busy`→`busy`, `failed`→`carrier_failed`),
  and record it **only if the call has no `ended_at` yet** (idempotent: a connected
  call is owned by `/stream` teardown, which sets `answered` + `end_reason`; a
  stray `completed` callback is ignored). `canceled`/`completed` are no-ops here.

### Refusal reason (opt-in) — resolves plan step 11
- Policy gains `probe_refusal_reason: bool = False`.
- `session.py`: `instructions_for(questionnaire, policy)` and
  `session_update(questionnaire, policy, voice=…)` take the policy. When the flag
  is **on**, the instructions add: *"if they'd rather not answer, gently ask once
  why, then accept it and move on — do not press further; if they give a reason,
  call `record_refusal` with it."* and the tool set includes a new
  `RECORD_REFUSAL_TOOL(question_id, reason)`. When **off**, behaviour and tool set
  are exactly today's (accept, move on, record nothing).
- `tools.py`: `handle_tool_call` dispatches `record_refusal` →
  `db.record_refusal(...)`, returning `ToolResult(ok=True, …)` with the same
  refusal-safe reminder shape.
- Storage (migration `0003`): two nullable-safe columns on `answers` —
  `declined boolean not null default false` and `refusal_reason text`.
  `db.record_refusal` upserts the `(assignment, question)` row with
  `declined=true, refusal_reason=<reason>, raw=""`.
- **Completion stays honest:** `db.answered_question_ids` filters
  `declined == false`, so a declined *required* question keeps the assignment
  `partial`, never `completed` (the step-11 invariant). A declined question is not
  "answered", and — because retry is keyed on `end_reason`, not on the answered set
  — a per-question decline does not by itself trigger a retry.

## Touches
- `src/policy.py` **(new)** — `RetryDecision` enum, `retry_decision`,
  `terminal_status`. Pure; the primary test surface for the retry policy.
- `src/config.py` (`Policy`) — add `retry_delays_minutes: list[int]`
  (default `[0, 2, 60]`, each `ge=0`) and `probe_refusal_reason: bool = False`.
  **Remove** `max_attempts`, `retry_after_minutes`, `call_window`,
  `max_call_seconds`, `silence_timeout_seconds`, `on_voicemail`, `on_opt_out`
  (and the now-unused `CallWindow`, `VoicemailAction`, `OptOutAction`). Keep
  `default_region`.
- `data/example/policy.yaml` — `retry_delays_minutes: [0, 2, 60]`,
  `probe_refusal_reason: false`, `default_region: ES`. The removed values move to a
  commented `# Parked for later (steps 5/10)` block so the intent is not lost.
- `src/models.py` — `Answer` gains `declined: bool = False` and
  `refusal_reason: str | None = None`.
- `src/db.py` — `declined`/`refusal_reason` columns on `answers`;
  `record_refusal(...)`; `answered_question_ids` filters `declined == false`;
  `answers_for` selects the two new columns; `select_next_to_call(conn, config,
  policy, now)` (scan + `policy.retry_decision`, replacing
  `next_pending_assignment`'s role in `runner.main`); a guarded
  `record_pre_answer_outcome(conn, call_id, disposition)` (updates only where
  `ended_at is null`). `next_pending_assignment` is removed if nothing else uses it.
- `src/runner.py` (`main`) — select via the policy scan; label
  `EXHAUSTED`/`STOP` assignments terminal. `place_call_for_assignment` passes the
  status-callback URL through the carrier.
- `src/telephony/__init__.py` + `src/telephony/twilio.py` — `place_call` gains
  `status_callback_url: str | None = None`; the Twilio adapter forwards it. (A
  contained extension of the carrier boundary — ADR-004.)
- `src/agent/session.py` — policy-aware `instructions_for` / `session_update`;
  `RECORD_REFUSAL_TOOL`; conditional tool set + instruction clause.
- `src/agent/tools.py` — `record_refusal` dispatch and handler.
- `src/app.py` — `POST /call_status`; pass `config.policy` into `session_update`.
- `migrations/versions/0003_*.py` — the two `answers` columns.
- `tests/` — see Acceptance.
- `docs/architecture.md` — module map (`src/policy.py`, `/call_status`), the
  assignment lifecycle (retry loop, `unreachable`/`partial` as retry-exhausted
  terminals), the "policy is step 7" status line; `docs/plan.md` — mark step 7
  partially landed (these two policies) and resolve step 11; `docs/decisions.md` —
  an ADR is warranted on acceptance (see Decisions).

## Does NOT touch
- **`completion_status()` / `finalize`** — completion still computed over the
  required set; the only change is that declined questions are excluded from
  "answered" (in `db.answered_question_ids`), which `completion_status` already
  consumes. `finalize`'s `end_reason` writing is unchanged.
- **The bridge (`src/bridge.py`) and `/stream` audio path** — untouched;
  `max_call_seconds`/`silence_timeout` (which would live here) are parked.
- **`src/telephony/` transport behaviour** beyond threading one optional
  `status_callback_url` argument — no change to signature validation, TwiML, or the
  media relay.
- **Retry that resumes mid-questionnaire** (skipping already-answered or declined
  questions on the re-dial) — a re-dial re-asks; the model does not yet see prior
  state. Explicitly deferred (see Non-goals).
- **The parked policies** — calling window, call/silence timeouts, voicemail,
  opt-out: commented, not enforced.

## Acceptance criteria
`.venv/bin/python -m pytest` green, `ruff` clean, and:
- **`tests/test_policy.py` (new)** — `retry_decision` over synthetic `Call` lists
  and a fixed `now`: `remote_ended` and `no_answer` last calls → `DIAL_NOW` once
  the schedule delay has elapsed and `WAIT` before it; `agent_completed` → `STOP`;
  a 4th attempt (schedule `[0,2,60]`) → `EXHAUSTED`; an open last call → `WAIT`.
  `terminal_status` → `unreachable` when no call connected, `partial` otherwise.
- **`tests/test_runner.py`** — with `[0, 2, 60]`: after a `remote_ended` call and
  `now = ended_at`, `select_next_to_call` returns the same assignment (immediate
  retry); at `ended_at + 1 min` after the *second* attempt it does **not** (2-min
  gate); after the schedule is exhausted the assignment is labelled `unreachable`/
  `partial` and is no longer selected. A first-ever `pending` assignment is still
  selected. Existing placement / double-call tests still pass.
- **`tests/test_voice_webhook.py`** (or a new `test_call_status.py`) — a
  `no-answer` status callback with a valid signature sets the call's disposition
  and `ended_at`; a callback for a call that already has an `ended_at` (connected,
  teardown ran) is a no-op; a bad signature → 403.
- **`tests/test_tools.py`** — with probing on, a `record_refusal(question_id,
  reason)` stores a row with `declined=true` and the reason, and that question is
  **absent** from `answered_question_ids`, so a required-but-declined question
  leaves `completion_status` at `partial`. `record_answer` unchanged.
- **`tests/test_session.py`** — `instructions_for(q, policy)` includes the
  ask-once-why clause and the tool set includes `record_refusal` **iff**
  `probe_refusal_reason` is true; the default (off) instruction/tools match today.
  (Asserts our instruction string, not the model's words — AGENTS.md.)
- **`tests/test_config.py`** — the new `Policy` shape loads from the example YAML;
  the removed fields are gone; `retry_delays_minutes` rejects a negative entry.

## What could go wrong (risks & guards)
- **Risk: a live call is treated as a failed attempt and re-dialled** (two calls to
  one person, violating ADR-013). → Guard: `retry_decision` returns `WAIT` whenever
  the last `Call` has no `ended_at`; only a callback (`/call_status`) or teardown
  (`/stream`) sets `ended_at`, so an in-flight or unresolved call never yields
  `DIAL_NOW`. Covered by the open-last-call case in `test_policy.py`.
- **Risk: the status callback double-finalises a connected call** (overwriting the
  teardown's `answered`/`end_reason` with `completed`, or racing it). → Guard:
  `record_pre_answer_outcome` updates only where `ended_at is null`, and
  `completed` is a no-op in the handler; teardown owns connected calls. Covered by
  the "already-ended → no-op" webhook test.
- **Risk: a declined question silently counts as answered and flips a required
  assignment to `completed`.** → Guard: `answered_question_ids` filters
  `declined == false`; the tools test asserts a required-but-declined assignment
  stays `partial`.
- **Risk: an infinite retry loop** (mis-counted attempts). → Guard: attempts =
  `Call`-row count, `EXHAUSTED` at `1 + len(retry_delays_minutes)`; the
  exhausted-assignment test asserts it stops and is labelled terminal.

## Non-goals
- Not enforcing the parked policies (calling window, `max_call_seconds`,
  `silence_timeout_seconds`, voicemail, opt-out) — commented in YAML, out of the
  model, deferred to steps 5/10.
- Not resuming a questionnaire mid-way on retry (skipping answered/declined
  questions): the re-dial re-asks; passing prior state into the instructions is a
  later refinement (plan step 10).
- Not scheduling the runner. Retry *timing* is decided from `now`; something must
  invoke `python -m src.runner` repeatedly (cron/loop) for a due retry to fire —
  that scheduling is out of scope. The decision function is correct given `now`.
- Not answering-machine detection: `/call_status` records only the pre-answer
  failure outcomes now; `AnsweredBy`/voicemail is the parked `on_voicemail` policy.

## Decisions (for `docs/decisions.md` on acceptance)
This realises ADR-005 (retry driven by `end_reason` + `status`) and ADR-007
(policy as data with a code branch per value) — no fork there. But it **resolves
plan step 11** by choosing one of its listed options: a per-question refusal is
persisted as a `declined` marker plus a `refusal_reason`, excluded from completion.
Picking that over "lean on `required` only" is a real fork → write an ADR on
acceptance covering both the refusal-as-data shape and the escalating
`retry_delays_minutes` schedule (a list of concrete delays, not a formula — still
within ADR-007).

## Status
Implemented (2026-07-22). Landed as specced, over the admin-UI commit (the retry
selection replaced `next_pending_assignment` inside the shared `place_next_call`, so
both the CLI and the UI's "Call next" honour retries). Offline-verified: full suite
green (103 tests, incl. `test_policy`, `test_call_status`, and the retry/refusal
additions to `test_runner`/`test_tools`/`test_session`/`test_config`), `ruff` clean,
migrations run clean from empty. Decisions recorded as ADR-024 (retry) and ADR-025
(refusal as data), resolving plan O8 and step 11. Live retry firing waits on a
runner scheduler (plan O9) and a real-line smoke — not exercised here.
