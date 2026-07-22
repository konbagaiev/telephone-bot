# Plan — open decisions

> Working backlog of things to **discuss and accept or reject**. The mutable
> antechamber to `decisions.md`. It keeps `architecture.md` and the append-only
> `decisions.md` clean: nothing lands there until it is resolved here.
>
> **Lifecycle of an item:** Open → discuss → resolved:
> - **Accepted** → write an ADR (Accepted) in `decisions.md`; update
>   `architecture.md` if the shape changed; remove the item here.
> - **Rejected** (significant) → write an ADR (Rejected) in `decisions.md`;
>   remove the item here.
> - **Dropped** (trivial) → just delete the item.
>
> Keep items terse: the fork and the options, no pre-baked conclusion.

## Open

### O7 — Is the media-path bridge still warranted?
**Question.** ADR-003 put us in the media path chiefly to retain audio; ADR-014
removed audio retention, and ADR-011 sources the transcript from the Realtime API
directly. Does the bridge still earn its cost?
**Options.** Keep it (control over the stream, no dependence on a SIP path) ·
move to a direct SIP connection between carrier and model, leaving us with events
and tool calls only.
**Needs first.** Verification of whether the Realtime API offers a usable SIP
path and what it costs in control — unverified as of 2026-07-18.
**Notes.** Not urgent: the bridge is small and already in the roadmap. Revisit
once the vertical slice runs, not before — this is a simplification of code that
does not exist yet.

### O9 — What drives the runner on a schedule?
**Question.** Retry timing is decided from `now` (ADR-024, spec `2026-07-22-0934`), but
something must invoke `python -m src.runner` repeatedly for a due retry — or any
pending assignment — to actually fire. What runs it?
**Options.** Cron on the VPS · a systemd timer · a long-running loop inside the
app container (sleep-poll) · an in-app scheduler (e.g. APScheduler) sharing the
process.
**Constraints.** Must not overlap runs — ADR-013 is one call at a time, so two
runners racing would waste a slot or double-dial. The placement→`in_progress`
transaction guards the DB, but the *scheduler* should still serialise. Deploy is
Docker Compose behind Traefik on the VPS (ADR-017), shared with other projects.
The runner is single-shot today (`main()` places at most one call and exits).
**Notes.** Cadence is bounded below by the shortest retry delay (`0` = "next
tick"), so the poll interval sets how "immediate" the instant retry really is. Not
urgent for the offline skeleton — the decision functions are correct given `now`
and tests inject it — but required before retries fire on the live line.

### O10 — Split `app.py` by HTTP boundary
**Question.** `app.py` (~460 lines) now colocates three concerns: the
carrier-facing edge (`/health`, `/voice`, `/stream`, `/call_status` — signature,
TwiML, bridge wiring; the only place `telephony.twilio` is imported), the
token-gated admin UI (`/ui/*`, the gate, render helpers), and shared wiring (the
`FastAPI()` app, `get_db_conn`, url/config helpers). When does it split, and along
what lines?
**Options.**
- **`src/web/` package** — `app.py` a thin composition root (`FastAPI()` +
  `include_router`), `webhooks.py` (the Twilio edge), `ui.py` (the admin router +
  its gate), `deps.py` (only what >1 router shares), `templates/` moves in. Mirrors
  the `agent/`/`telephony/` package style.
- **Flat siblings** — `src/webhooks.py` + `src/ui.py` + `src/deps.py`, `app.py`
  stays as the root. Less churn, no new package.
- **Leave as one file** until it actually hurts.
**Rules if split.** One `APIRouter` per boundary owning its own routes/deps;
imports point one way (routers → `db`/`config`/`runner`/`bridge`, never back to
`app.py`, so shared deps live in `deps.py`, not `app.py`); the UI gate stays on the
UI router and the webhooks stay off it (existing convention). Mechanical and
behaviour-preserving — `TestClient` still hits `app`, `dependency_overrides`
resolve by function identity (only test import paths change), plus
`uvicorn src.app:app` → the new root in the Dockerfile.
**Needs first / trigger.** Not yet — tolerable at ~460 lines. The natural moment is
when `/call_status` + the retry work (spec `0934`) grow the webhook side enough
that the UI block gets in the way of reading the telephony logic. Extract `ui.py`
first (most self-contained), then `webhooks.py`.
**Notes.** No ADR — a mechanical refactor, not a fork (unless package-vs-flat is
made a deliberate repo convention). Update `architecture.md`'s module map in the
same change (AGENTS.md step 6).

### O11 — A carrier rejection must not surface as HTTP 500
**Question.** When Twilio refuses to place a call, `place_call`
(`src/telephony/twilio.py:75`) raises `TwilioRestException`, which nothing
catches: it propagates through `place_next_call` → `ui_call_next`
(`src/app.py:422`) and FastAPI returns a bare **500 + traceback**. Observed live
2026-07-22 dialling a Serbian number (`+381…`): Twilio HTTP 400 "Account not
authorized to call … enable some international permissions" — an expected,
actionable outcome (geo-permissions off, bad number, no balance, unverified
destination), not a server fault. How do we handle it explicitly?
**Options.** Catch `TwilioRestException` at the carrier boundary and translate to
a typed domain error (e.g. `CarrierRejected` carrying Twilio's code/message),
handled by callers · catch in the UI/runner edge and re-raise as `HTTPException`
4xx · both (translate at the boundary, present at the edge). The UI should show
the reason inline (the geo-permissions case is a one-click fix in the Twilio
console), not a stack trace.
**Also.** Decide the runner/CLI path (`place_next_call` outside HTTP) — it should
fail the one assignment with a logged reason, not crash the process, and leave the
assignment re-runnable (it currently flips `pending → in_progress` at placement,
step 6). Whether a rejected placement should roll that back is part of this.
**Notes.** Small and worth doing before wider live testing (step 10), where messy
destinations are the point. No ADR unless the domain-error shape becomes a
convention.

## Roadmap

> Not decisions — the agreed order of work. Each step ends in something that
> runs. Steps become specs in `specs/active/`.

1. **Data model.** _(done)_ Questions, questionnaires and policy loaded from
   YAML; people, assignments, calls and answers in Postgres with Alembic
   migrations (ADR-016). No network.
2. **Accounts and environment.** _(in progress)_ Twilio and OpenAI credentials,
   the existing Twilio dev number (no Spanish number and no regulatory bundle —
   demo-only scope, ADR-018), a verified test destination. The VPS already exists
   (Hetzner, Germany — well placed for both the German and Cypriot destinations,
   ADR-010). Runs in parallel with step 1.

   While configuring the number, check that Twilio uses a European region/edge:
   on the default, media may route via the US and cross the Atlantic twice before
   reaching a server that sits in Frankfurt. Unverified — confirm at setup time.
   The round trip to the Realtime model itself is likely transatlantic regardless
   and is not ours to optimise.
3. **Deploy pipeline and skeleton service.** The whole path from `git push` to a
   live public endpoint, stood up before the app that fills it, so the deployment
   risk is separated from the latency-sensitive audio risk and each is verified on
   its own (ADR-017). A minimal health-check service, packaged as a Docker image
   and routed by the existing Traefik on `phone-bot.bagaiev.com` (Traefik
   terminates TLS and passes the WebSocket upgrade through); a GitHub Actions
   pipeline that gates on the test suite, then pulls, runs migrations (ADR-016),
   recreates the container, and checks health. The server bootstrap lands here:
   the checkout, Docker, a `vividi` database and its credentials, secrets in a
   VPS-local `.env` (ADR-015), and a dedicated CI deploy key. Ends in: a push to
   `main` updates a live HTTPS endpoint.
4. **Vertical slice — one live call, one question.** _(done — live smoke passed
   2026-07-21: a real call recorded an answer and finalised to `completed`, over
   the GA Realtime API. Findings along the way: config must ship in the image, the
   Realtime GA shape, and finalise-on-teardown — all in `specs/done/`.)_ The whole
   call path end to
   end, filling the skeleton service: place a call, validate the Twilio
   signature, bridge the audio, the agent asks one question, `record_answer` is
   handled, the result is written. The public base URL is set in **both** the app
   config and the Twilio number's webhook/stream URLs — one setting recorded in
   two systems, a silent failure if they disagree (ADR-015). Configuration is read
   per call, so editing a question takes effect on the next call without a restart
   — the property the whole repo exists to make fast and safe (AGENTS.md).
   Deliberately narrow: Realtime behaves differently on a real phone line than in
   any offline harness, and we want to find that out first.
5. **Debug the live behaviour of the stack on a real line.** _(new — from step-4
   findings.)_ The slice records an answer, but a real call exposes behaviour no
   offline harness shows, and it must be tuned before widening the questionnaire.
   Known findings (2026-07-21):
   - **The agent does not greet first.** On answer there is silence until the
     respondent speaks — a bare `response.create` after `session.update` does not
     reliably open the turn under server VAD. Earlier calls only seemed to work
     because the person said "hello", which triggered the model.
   - **Background noise ends the call.** Server VAD treats any audio as speech, so a
     TV in the room floods the model with phantom input and the call degrades or
     ends. Needs VAD tuning, input noise reduction, or semantic VAD.
   - **`raw` is the model's claim, not the utterance.** Under noise the model
     recorded an answer the respondent never gave — `raw` is filled by the model in
     `record_answer`, not transcribed. Storing the real transcript (spec
     `2026-07-22-0012`) is the first move: it makes the divergence visible.
     Repointing `raw` at the transcript is a later, separate decision.
   - **The goodbye is clipped on hang-up (2026-07-22).** When the agent ends the
     call, `end_call` closed the sockets immediately while Twilio still had the
     goodbye buffered, so the closing words were cut. Fixed by draining Twilio
     playback (an end-of-call `mark` + grace) before closing — spec
     `2026-07-22-0844`; live-confirmed on call 7 (the closing words played in full).
   The transcript-storage spec is the debugging instrument for this step.
6. **Full questionnaire.** _(done 2026-07-22, spec `2026-07-22-0755` in
   `specs/done/` — live-verified on call 7: both questions recorded, assignment
   `completed`, goodbye intact.)_ The model is handed the whole ordered question
   list in its session instructions and drives the conversation itself (ADR-002):
   it asks each in turn, records every answer, and thanks the person and says
   goodbye before `end_call`. A refusal is accepted graciously — move on, do not
   press, do not record a skipped question; `record_answer` returns a refusal-safe
   reminder to cover any remaining questions (not an enumeration of what is missing,
   which would push re-asking a declined one — see step 11). Completion is computed
   over the full required set (`completion_status`), unchanged. Also landed the
   carried double-call fix: `place_call_for_assignment` now moves the assignment
   `pending → in_progress` at placement (same transaction as the Call row), so a
   second runner run skips an in-flight call. **New behaviour to recover later:** a
   call that never connects now stays `in_progress` instead of being accidentally
   re-dialled — deliberate retry/unreachable handling is step 7.
7. **Policy.** _(first slice done 2026-07-22, spec `2026-07-22-0934` in
   `specs/done/`, ADR-024/ADR-025.)_ A policy-enforcement skeleton (`src/policy.py`,
   pure decisions over `Policy` + facts + an injected clock) with two policies wired
   through it: retry-on-disconnect (escalating `retry_delays_minutes: [0, 2, 60]`,
   keyed on `end_reason`/`disposition`, never-answered detected via the new
   `/call_status` webhook, exhausted → `unreachable`/`partial`) and refusal-reason
   (opt-in `probe_refusal_reason`, `record_refusal` stores a `declined` marker +
   reason, excluded from completion — closes step 11). Offline-verified: full suite
   green (103 tests). **Still parked** (commented in `policy.yaml`, out of the
   model): calling window, `max_call_seconds`, `silence_timeout_seconds`, voicemail,
   opt-out — each needs live-line debugging (steps 5/10). Retries only *fire* once
   something runs the runner on a schedule — see O9.
   **Live finding (2026-07-22, call 10) — fixed, re-verify on the line.** The flag
   was on and `record_refusal` fired (both declines stored with a `refusal_reason`),
   but the respondent was never *asked* why: the model treated the refusal utterance
   itself ("I don't want to reply to this question") as the reason and moved on. So
   the wiring was fine — the fault was prompt adherence: `_REFUSAL_PROBE` collapsed
   "ask once why → then record" into one beat and skipped the spoken probe.
   Rewrote the clause to force a distinct spoken follow-up *before* recording and to
   forbid treating the initial decline as the reason. The clause lives in code
   (`src/agent/session.py`), not the YAML, so this ships by deploy (commit + push →
   image rebuild), not `push_config`; then re-run `reset_and_call` and read the
   transcript to confirm the agent actually voices the "why".
8. **Multilingual.** English and Russian per `Person.language`.
9. **UI.** _(done 2026-07-22, spec `2026-07-22-0923` in `specs/done/`, ADR-023.)_
   A minimal token-gated `/ui` admin surface in the same ASGI app, server-rendered
   (Jinja, no JS build). Built ahead of steps 7–8 because ADR-006's precondition —
   a system that already runs — is met (call 7). Surfaces delivered:
   - **Add a person and an assignment** — a form, replacing the hand-run Python
     snippet against the prod DB. Plus delete and **reset** (wipe an assignment's
     history back to `pending`) for re-running a demo.
   - **Launch a call** — a "Call next" button reusing the runner's next-pending pick
     (`place_next_call`), no per-person on-demand dial.
   - **View answers** — per assignment: `raw`/`value` and the computed completion.
   - **Read the transcript** — the stored `transcript_segments` per call (ADR-022),
     for comparing what was said against what was recorded.
   Verified in CI (`tests/test_ui.py`, gate + wiring; `tests/test_storage.py`, the
   reset/delete boundary) and in prod after deploy (gate: `/ui` 401 without token,
   200 with, `/voice` still 403-by-signature). `UI_TOKEN` is read from the
   environment for now; it moves into the typed settings object when spec
   `2026-07-21-1448` lands.
10. **Edge-case testing.** Deliberately exercise the messy real-line conditions a
    happy-path smoke never hits, and make the stack behave under each:
    - **Background noise** — a TV or a room of people: server VAD treats it as
      speech and the model records answers no one gave (the step-5 finding). Needs
      VAD tuning / input noise reduction / semantic VAD, verified against the
      stored transcript.
    - **Unexpected drops** — the respondent hangs up mid-answer, the carrier cuts
      out, the socket dies: the call must finalize cleanly and the assignment land
      `partial`, never a phantom `completed`.
    - **Callbacks after a delay** — retrying a person later (missed, busy,
      voicemail) without double-calling or re-asking answered questions; overlaps
      step 7's policy and the step-6 `in_progress` transition.
    - **Others as they surface** — silence / no answer, the respondent talking over
      the greeting, very long or refused answers, a hang-up during the agent's
      turn. This step is where the live findings that are not features get chased
      down; it grows as smokes expose more.
11. **Per-question refusal as data.** _(done 2026-07-22 with step 7, spec
    `2026-07-22-0934`, ADR-025.)_ Resolved by the "persist a declined marker"
    option: `answers` gains `declined` + `refusal_reason`, written by a new
    `record_refusal` tool, excluded from `answered_question_ids` so a declined
    required question stays `partial`. Probing for the reason is opt-in
    (`probe_refusal_reason`, default off = step-6 behaviour).

## Resolved

_(resolved items leave here and become ADRs in `docs/decisions.md`)_

- P1 Language & framework → ADR-001, ADR-006
- P2 Build vs buy → ADR-002, ADR-003, ADR-004, ADR-006
- P3 Code structure → ADR-002 (model owns speech / code owns facts), ADR-004
  (carrier behind an interface)
- P4 Conversation definition → ADR-007
- O6 Hosting → ADR-008 (laptop behind a tunnel)
- O8 Retry & policy model shape → ADR-024 (retry-on-disconnect), ADR-025 (refusal
  as data). The parked policy values remain future roadmap-step-7 work, not an
  open fork.
