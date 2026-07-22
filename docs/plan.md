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

### O8 — Retry & policy model shape (step 7)
**Question.** How the retry/policy layer is modelled. Sketched while scoping step
7; none of the sub-forks is resolved — do not promote to an ADR until decided.
**Forks.**
- **Retry cadence shape.** `retry_delays_minutes: [0, 2, 60]` (an ordered list;
  its length encodes the attempt cap; each delay measured from the previous
  call's `ended_at`) · vs today's two scalars `max_attempts` + `retry_after_minutes`.
  The list would supersede those ADR-007 policy fields.
- **`unreachable` terminal status.** Add a terminal assignment status for "retries
  exhausted, never completed" · vs leaving such assignments in `in_progress`
  (today's step-6 behaviour) · vs reusing `partial`. Touches ADR-005's status
  lifecycle.
- **Redial trigger.** Broad — any non-`completed`, non-`opted_out` outcome
  including a never-connected `in_progress` · vs strict — only a technical
  `remote_ended` drop, with no-answer/busy handled separately.
- **Refusal-reason probe.** A `probe_refusal_reason` flag that has the agent ask
  once for the *reason* a question was declined — deliberately softening step-6's
  "accept the refusal, don't press". The reason stays transcript-only until step
  11 decides persistence. This re-opens an already-shipped behaviour, so it needs
  an explicit decision, not a silent flag.
- **Enforcement seam.** Retry logic in the runner (the assignment-selection gate)
  · vs the refusal probe in the session instructions (ADR-002).
**Needs first.** Step 7 to start. **Review the commented-out policies** —
`call_window`, `max_call_seconds`, `silence_timeout` are stubbed in `policy.yaml`,
shipped inert to prove the framework, and are not yet decided; settle them here
when step 7 begins.

### O9 — What drives the runner on a schedule?
**Question.** Retry timing is decided from `now` (O8, spec `2026-07-22-0934`), but
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
7. **Policy.** Retries, calling window, timeouts, voicemail handling, opt-out.
   First slice specced (`2026-07-22-0934`): a policy-enforcement skeleton
   (`src/policy.py`) with retry-on-disconnect and refusal-reason; the rest parked.
   Retries only *fire* once something runs the runner on a schedule — see O9.
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
11. **Per-question refusal as data.** _(deferred from step 6 — 2026-07-22.)_ Step 6
    handles a refusal in the model's *behaviour* only: it accepts a skip
    graciously, moves on, and records nothing — so a refused required question
    lands `partial` and the refusal survives only in the transcript. That removed
    the blocker (and the false-`completed` risk of the model recording a refusal as
    an answer), but leaves an open design question: should a refusal be a
    first-class *fact*, distinct from "never asked"? Options to weigh later:
    - **Lean on `required` vs optional** — already in the config (`Question.required`,
      `completion_status`): an optional question refused is a non-event; only a
      *required* refusal is the real case. Possibly enough on its own.
    - **Persist a "declined" marker** — e.g. a nullable column on `answers` or a
      distinct answer value — so retry logic (step 7) does not re-badger someone who
      declined, and reporting can tell "declined" from "unanswered".
    - **Keep completion honest either way** — a declined required question must stay
      `partial`, never flip to `completed`. Decide the shape when step 7's retry
      policy needs it; not before.

## Resolved

_(resolved items leave here and become ADRs in `docs/decisions.md`)_

- P1 Language & framework → ADR-001, ADR-006
- P2 Build vs buy → ADR-002, ADR-003, ADR-004, ADR-006
- P3 Code structure → ADR-002 (model owns speech / code owns facts), ADR-004
  (carrier behind an interface)
- P4 Conversation definition → ADR-007
- O6 Hosting → ADR-008 (laptop behind a tunnel)
