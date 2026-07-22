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
   The transcript-storage spec is the debugging instrument for this step.
6. **Full questionnaire.** Multiple questions, required-answer completion logic,
   `Assignment.status` transitions.

   **Confirmed limitation from step 4:** `/stream` asks only
   `questionnaire.questions[0]` (hard-coded), so a questionnaire that defines more
   than one question — like the example's `delivery_feedback` (`was_on_time` +
   `improvement`) — still gets only the first asked, and only that one can be
   recorded. Observed live on 2026-07-21: the second question was never put. Step 6
   iterates all questions in order and drives the conversation until every required
   one is answered (completion is already computed over the full set in
   `completion_status`).

   **Fix carried from step 4:** placing a call does not move the assignment off
   `pending` (`place_call_for_assignment` never sets a status; `IN_PROGRESS` is
   defined but unused). So a second runner run before the call finishes re-picks
   the same `pending` assignment and calls the person twice — the assignment only
   leaves `pending` when `/stream` runs `refresh_completion` at teardown. The fix
   is a `pending → in_progress` transition at placement time, so the next pick
   skips an in-flight call. Coordination is via the DB, since placement (runner)
   and completion (web app) are separate processes.
7. **Policy.** Retries, calling window, timeouts, voicemail handling, opt-out.
8. **Multilingual.** English and Russian per `Person.language`.
9. **UI.** Only once the above works (ADR-006: UI last, on top of a system that
   already runs). Concrete surfaces:
   - **Add a person and an assignment** — replacing today's ad-hoc DB seeding (a
     hand-run Python snippet against the prod DB) with a form.
   - **View answers** — per person/assignment: the recorded `raw`/`value` and the
     computed completion status.
   - **Read the transcript** — the stored call transcript, for comparing what was
     actually said against what was recorded. Depends on step 5's transcript
     storage (spec `2026-07-22-0012`).

## Resolved

_(resolved items leave here and become ADRs in `docs/decisions.md`)_

- P1 Language & framework → ADR-001, ADR-006
- P2 Build vs buy → ADR-002, ADR-003, ADR-004, ADR-006
- P3 Code structure → ADR-002 (model owns speech / code owns facts), ADR-004
  (carrier behind an interface)
- P4 Conversation definition → ADR-007
- O6 Hosting → ADR-008 (laptop behind a tunnel)
