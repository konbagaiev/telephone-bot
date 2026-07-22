# Spec: Ask the whole questionnaire, and stop double-calling

> Roadmap step 6. The vertical slice asks only `questionnaire.questions[0]`
> (hard-coded in `/stream`), so a questionnaire that defines more than one
> question — the example's `delivery_feedback` (`was_on_time` + `improvement`) —
> gets only the first asked (observed live 2026-07-21). Separately, placing a call
> never moves the assignment off `pending`, so a second runner run re-picks the
> same assignment and calls the person twice. This change closes both.

## Goal
Drive the model through **every** question in the assignment's questionnaire, in
order, recording each answer; and mark an assignment `in_progress` at call
placement so an in-flight call is not re-picked and dialled again. Completion is
already computed over the full required set (`completion_status`) — this change
makes the *conversation* cover the set the completion logic already judges.

## Approach — the model drives, code stays reactive (ADR-002)
The model owns speech and turn-taking (ADR-002), so it also owns the *sequence*:
we hand it the whole ordered list of questions in the session instructions and let
it ask, record, and close the call itself. Code does not orchestrate turn-by-turn
— it reacts to each `record_answer`, feeds back what still remains, and recomputes
completion at teardown. An early `end_call` therefore yields `partial`, exactly as
it does today for one question; nothing new guards that path.

The `record_answer` feedback carries a light nudge — which required questions are
still unanswered — so the model reliably knows when the set is closed and it is
time to give a closing goodbye. This stays within ADR-002: it informs the model,
it does not script its words. The instructions require the model to thank the
respondent and **say goodbye** before calling `end_call`.

The tool layer already supports this: `_record_answer` validates `question_id`
against the whole questionnaire, and `record_answer` upserts per
`(assignment, question)`. Only who is *asked* changes.

## Touches
- `src/agent/session.py`
  - `instructions_for(questionnaire)` — takes the `Questionnaire` (not a single
    `Question`). Enumerates every question in order (id + intent, plus a `phrasing`
    override where one is given), instructs the model to ask each in turn, call
    `record_answer` for each with the matching `question_id`, and — once the
    questions are covered — **thank the respondent, say goodbye, then `end_call`**.
    Drops the "ask only this one question; do not add others" clause.
  - `session_update(questionnaire, voice=…)` — same signature swap; passes the
    questionnaire through to `instructions_for`.
  - Tool descriptions: `RECORD_ANSWER_TOOL` / `END_CALL_TOOL` reworded from "the
    question" / "one question" to the multi-question flow (record each answer as it
    comes; end after the last question and the goodbye).
- `src/agent/tools.py`
  - `_record_answer` returns a `ToolResult.message` that names the required
    question ids still unanswered (a "still to ask" nudge), or signals all required
    questions are answered. Reads current answers via
    `db.answered_question_ids(session.conn, session.assignment_id)` against
    `session.questionnaire.required_question_ids`. Still `ok=True` on a stored
    answer; unchanged on refusal/unknown id.
- `src/app.py` (`/stream`)
  - Drop `question = questionnaire.questions[0]`; pass `questionnaire` to
    `session_update`.
- `src/runner.py` (`place_call_for_assignment`)
  - After the carrier accepts and the carrier id is stored, set the assignment to
    `AssignmentStatus.IN_PROGRESS` (`db.set_assignment_status`) in the *same*
    transaction as the Call row. `next_pending_assignment` already filters on
    `PENDING`, so the next pick skips an in-flight call. Placement and completion
    are separate processes (runner vs web app); the DB is the coordination point.
- `tests/` — see Acceptance.
- `docs/architecture.md` — call-path step 3 ("its (one) question" → all
  questions), the "one question in this slice" notes, and the assignment
  lifecycle: `pending → in_progress` (placement) `→ completed`/`partial`
  (teardown).

## Does NOT touch
- **`completion_status()` / `refresh_completion` / `finalize`** — completion is
  already computed over the required set; unchanged. Optional questions
  (`improvement`) still do not gate `completed`.
- **The `record_answer` upsert, the `(assignment, question)` uniqueness, the
  transcript storage (ADR-011)** — unchanged.
- **Recovery of a call that never connects** — see the risk below; that is step 7
  (policy: retries, voicemail, unreachable) and step 10 (drops), not this change.
- **Code-driven sequencing** — deliberately rejected in favour of the model-driven
  flow above (ADR-002).
- **`src/telephony/`** — no carrier/transport behaviour changes.

## Acceptance criteria
`.venv/bin/python -m pytest` green, `ruff` clean, and:
- **`test_tools.py`** — recording answers to *both* `was_on_time` and
  `improvement` in one session stores two rows keyed to their question ids; the
  `record_answer` result for the first (a required question still missing) names
  what remains, and after the last required answer the result signals the required
  set is complete. Existing single-answer, replace, unknown-id, and
  `finalize`→`partial`/`completed` tests still pass unchanged.
- **`test_session.py` (new, or added to an existing session test)** —
  `instructions_for(questionnaire)` mentions *every* question id in the example
  questionnaire and instructs a goodbye before ending. (Asserts our instruction
  string — our output — not the model's words; AGENTS.md.)
- **`test_runner.py`** — after `place_call_for_assignment`, the assignment is
  `IN_PROGRESS` and `next_pending_assignment(conn)` no longer returns it (the
  double-call fix). Existing placement/oldest-pending tests still pass.

## What could go wrong (risks & guards)
- **Risk: a call that never connects (no-answer, voicemail, carrier drop) now
  stays `in_progress` forever** — previously such an assignment stayed `pending`
  and got accidentally re-dialled on the next run. We trade that accidental retry
  (which *was* the double-call bug) for a stuck `in_progress`. → Guard: accepted
  and documented for step 6; deliberate recovery (retry within policy, mark
  `unreachable`) is step 7. At one-call-at-a-time manual-runner scale (ADR-013) an
  operator sees the state. Not manufactured into a code path here.
- **Risk: the model ends early and leaves required questions unanswered** →
  Guard: not new — completion is computed at teardown (`finalize`), so an early
  `end_call` lands `partial`, never a phantom `completed` (existing
  `test_finalize_without_the_required_answer_is_partial`). The "still to ask" nudge
  reduces how often this happens but is not relied on for correctness.
- **Risk: the `in_progress` write and the Call row diverge** (one commits, the
  other not) → Guard: both happen in the single `engine.begin()` transaction in
  `runner.main()`; a carrier failure raises and rolls back the whole thing,
  leaving the assignment `pending`. Covered by the runner placement test.

## Non-goals
- Not code-driving the question sequence, and not blocking `end_call` until the
  required set is closed (the model drives; completion is computed).
- Not adding retry/voicemail/unreachable recovery for a non-connecting call (step
  7), nor draining an in-flight call on restart (ADR-017).
- Not repointing `raw` at the transcript, nor any reconciliation of the two
  (separate decision).
- Not a new ADR: this realises ADR-002/ADR-007 (model owns the multi-question
  speech) and the `IN_PROGRESS` status that ADR-005 already defined but left
  unused. No fork is opened.

## Status
Implemented (2026-07-22). Offline-verified: full suite green (62 tests), `ruff`
clean. The model-driven multi-question flow is confirmed live by the next smoke;
the instruction string and the `record_answer` nudge are unit-tested, but the
model actually asking all questions and closing with a goodbye is a live check.
