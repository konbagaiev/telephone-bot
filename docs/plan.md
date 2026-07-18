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

1. **Data model.** Questions, questionnaires and policy loaded from YAML;
   people, assignments, calls and answers in Postgres with Alembic migrations
   (ADR-016). No network.
2. **Accounts and environment.** Twilio and OpenAI credentials, a Spanish number
   (regulatory bundle — has lead time, start early), a verified test destination.
   On the VPS (ADR-015): a subdomain with DNS and TLS, the reverse proxy passing
   WebSocket upgrades through, secrets in place, a database and its credentials,
   and a GitHub Actions deploy that runs migrations (ADR-016).
   The public base URL must then be set in **both** the app config and the Twilio
   number's webhook/stream URLs. Runs in parallel with step 1.

   The VPS is Hetzner, Germany — well placed for both the German and Cypriot
   destinations (ADR-010). While configuring the number, check that Twilio uses a
   European region/edge: on the default, media may route via the US and cross the
   Atlantic twice before reaching a server that sits in Frankfurt. Unverified —
   confirm at setup time. The round trip to the Realtime model itself is likely
   transatlantic regardless and is not ours to optimise.
3. **Vertical slice — one live call, one question.** The whole path end to end:
   place a call, validate the Twilio signature, bridge the audio, the agent asks
   one question, `record_answer` is handled, the result is written. Deliberately
   narrow: Realtime behaves
   differently on a real phone line than in any offline harness, and we want to
   find that out first.
4. **Full questionnaire.** Multiple questions, required-answer completion logic,
   `Assignment.status` transitions.
5. **Policy.** Retries, calling window, timeouts, voicemail handling, opt-out.
6. **Multilingual.** English and Russian per `Person.language`.
7. **UI.** Only once the above works.

## Resolved

_(resolved items leave here and become ADRs in `docs/decisions.md`)_

- P1 Language & framework → ADR-001, ADR-006
- P2 Build vs buy → ADR-002, ADR-003, ADR-004, ADR-006
- P3 Code structure → ADR-002 (model owns speech / code owns facts), ADR-004
  (carrier behind an interface)
- P4 Conversation definition → ADR-007
- O6 Hosting → ADR-008 (laptop behind a tunnel)
