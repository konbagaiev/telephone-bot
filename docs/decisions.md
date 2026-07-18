# Decisions (ADRs)

> **Append-only.** Record a decision when we pick one option over a viable other
> — or deliberately reject one. Never edit a resolved entry to match new reality;
> add a new entry that supersedes it and link them. Each entry: Context →
> Decision → Consequences → Status.
>
> **Status lifecycle:** `Accepted` / `Rejected` / `Superseded`. Items are worked
> out first in `docs/plan.md` and only land here once resolved — so this file
> holds real decisions only, never open debate.

---

## ADR-001 — Python as the implementation language

**Context.** Resolves P1. The system glues together a telephony carrier, a
realtime speech model, and a small control plane. Vivid's own stack is Go +
Kotlin, so this is a deliberate divergence.

**Decision.** Python.

**Consequences.** The public example corpus for every component of this
architecture (Twilio, OpenAI Realtime, audio bridging) is predominantly Python,
which is the dominant factor for a prototype. The code stays easy to read for
non-authors, which matters for a codebase meant to be edited live with AI
assistance. Cost: divergence from the house stack, so this service will not
share libraries with the rest of Vivid.

**Status:** Accepted

---

## ADR-002 — Speech-to-speech via OpenAI Realtime; model owns speech, code owns facts

**Context.** The overriding product requirement is that the call feels like
talking to a person. A classic STT → LLM → TTS pipeline adds latency at every
hop and loses prosody; a scripted dialogue driven from our code makes the agent
sound robotic no matter how good the voices are.

**Decision.** Use a speech-to-speech realtime model (OpenAI Realtime) and split
ownership:

- The **model owns speech** — wording, turn-taking, barge-in, handling of a live
  human within the current question.
- **Our code owns facts** — which questions exist, who is being called, what has
  been recorded, and when the call ends.

The two meet through tool calls (`record_answer`, `end_call`, and similar). Our
code never dictates individual utterances.

**Consequences.** Naturalness is delegated to the model, which is the only way
to get it. In exchange we give up a deterministic dialogue by design: we cannot
assert what the agent said, only what it recorded. Therefore the test surface is
the **tool-call handling** — unknown question id, duplicate answer, call ending
with no answers — never the model's wording. This is consistent with the testing
rules in `AGENTS.md`.

Crucially, **completion is determined by us, not by the model**: an assignment is
complete when every required question has a recorded answer, regardless of the
model calling `end_call`. The model may say goodbye prematurely; the data must
not take its word for it.

**Status:** Accepted

---

## ADR-003 — Media-streams bridge, not a direct SIP connection

**Context.** Two ways to connect a carrier to the realtime model: (1) the carrier
streams audio to our server over a WebSocket and we relay it to the model, or
(2) a SIP trunk connects the carrier directly to the model and our server only
receives events.

**Decision.** Option 1 — our server sits in the media path.

**Consequences.** We can observe and retain the raw audio of every call, which is
the basis for keeping a full record of the conversation independent of the
model's own transcription (whose accuracy varies by language — see ADR-007 on
multilingual operation). It also leaves room to intervene in the stream later.

Cost: more code than a SIP hand-off, one extra network hop of latency, and our
server becomes availability-critical for the duration of every call. Accepted
because the recording and observability requirement is explicit, and the bridge
itself is small (relaying base64 audio frames between two WebSockets).

**Status:** Accepted

---

## ADR-004 — Twilio first, behind a carrier interface

**Context.** Resolves part of P2. Carrier choice depends on outbound
deliverability and price in the target countries (EU/US), which is not settled
and will not be settled by argument — only by trying. Candidates: Twilio,
Telnyx, Plivo, SignalWire, Bandwidth.

**Decision.** Build against Twilio first, but keep it behind a narrow internal
interface (place a call, receive call events, hang up). No carrier-specific type
leaks past that boundary.

**Consequences.** Twilio has the largest public example corpus, including the
Media Streams ↔ OpenAI Realtime path we need, which minimises prototype risk.
Per-minute cost is higher than Telnyx, which is irrelevant at prototype volume.
Swapping carriers later is a contained change rather than a rewrite.

Verified carrier capabilities that this interface must account for (Twilio docs,
checked 2026-07-18):

- Answering machine detection is available (`MachineDetection`), returning
  `AnsweredBy` ∈ `human` / `machine_start` / `fax` / `unknown`.
- Pre-answer outcomes are distinguishable: `busy`, `no-answer`, `failed`,
  `canceled`.
- **The cause of a hangup is not available.** A callee hangup, our own hangup,
  and a mid-call network drop all report `CallStatus=completed` with
  `SipResponseCode=200`. The Media Streams `stop` event carries no reason at
  all. Only Voice Insights exposes `disconnected_by`, it reflects nothing but the
  direction of the SIP BYE (often `unknown`), and programmatic access is a paid
  add-on.

See ADR-005 for how the data model responds to that last point.

**Status:** Accepted

---

## ADR-005 — Call outcome is modelled in three independent fields

**Context.** "Did we reach them" and "did we get the answers" are different
questions with different lifecycles, and mixing them into one status makes
"dropped halfway through the questionnaire" invisible. Constrained by ADR-004:
the carrier does not tell us *why* a call ended.

**Decision.** Three fields, deliberately independent:

- `Call.disposition` — what became of the *call attempt*: `answered`,
  `no_answer`, `busy`, `voicemail`, `carrier_failed`.
- `Call.end_reason` — how an *answered* call ended: `agent_completed`,
  `agent_stopped` (our timeout/policy), `remote_ended`, `agent_error`.
- `Assignment.status` — what became of the *questionnaire* for this person:
  `pending`, `in_progress`, `completed`, `partial`, `unreachable`, `opted_out`.

`remote_ended` deliberately conflates "the person hung up" with "the connection
dropped", because ADR-004 establishes that no carrier field distinguishes them.
We model what we can actually observe rather than inventing a field we cannot
populate.

**Consequences.** A call that broke off mid-questionnaire is identified without
any hangup-cause signal: `end_reason != agent_completed` together with
`status == partial`, and the recorded answers show which question it stopped at.
That is sufficient to drive retries.

If distinguishing a human hangup from a network drop later proves to matter,
enabling paid Voice Insights refines the meaning of `remote_ended` without a
schema change.

**Status:** Accepted

---

## ADR-006 — No voice framework; CLI + YAML before any UI

**Context.** Frameworks exist that implement exactly this bridge (Pipecat,
LiveKit). Separately, the control plane needs *some* interface.

**Decision.** No voice framework. Direct use of the carrier and model APIs, with
a thin dependency set: FastAPI + uvicorn (the carrier webhooks and the media
WebSocket require an HTTP server regardless), Pydantic (data models, tool-call
validation, and JSON Schema for the tool definitions), and a CLI.

Configuration and inputs live in YAML files. Call results are appended to
JSONL — never written back into the input YAML. A web UI comes later, on top of a
system that already works.

**Consequences.** Voice frameworks earn their keep by orchestrating STT/TTS
pipelines across providers; under ADR-002 that pipeline collapses into a single
speech-to-speech connection, leaving little for a framework to do but insert an
abstraction exactly where ADR-003 wants direct access to the stream. We keep the
bridge small and owned.

YAML inputs are diffable and version-controlled, which suits a prototype whose
questions change constantly. Append-only JSONL results mean an interrupted call
cannot corrupt prior data — a real risk with rewrite-in-place YAML. Both are
replaceable by a database behind the same repository interface.

Cost: no admin UI for non-technical users until it is built.

**Status:** Accepted — storage clause superseded by ADR-016 (the rest stands:
no voice framework, the dependency set, CLI before UI)

---

## ADR-007 — Conversation and policy are data; behaviour is a closed set in code

**Context.** Resolves P4. Questions change constantly and must be editable
without a release. The system must also operate in more than one language
(English and Russian initially).

**Decision.** Questions are defined as **intent plus expected answer type**,
not as a script to be read aloud — following ADR-002, phrasing belongs to the
model. Per-language wording is an optional override, for questions where exact
phrasing matters.

Answers are stored twice: the raw utterance in the language of the call, and a
normalised value.

Policy for edge cases is a set of **parameters** in data (attempt count, retry
delay, calling window, maximum call duration, silence timeout, voicemail
behaviour, opt-out behaviour). Every value corresponds to an existing branch in
code. Adding a *kind* of behaviour is a code change with a test; tuning an
existing one is a data change.

**Consequences.** Adding or reordering questions requires no code. Supporting a
language requires no translation pass, only a language attribute on the person —
which matters because we cannot review translations for languages nobody on the
team reads. Normalised answers keep responses comparable across languages;
without them, multilingual results cannot be aggregated.

The boundary has a clear test: if a policy value needs a condition or a formula,
it should have been code. Guarding that boundary is a permanent review duty —
policy-as-data degenerates into a bad programming language if it is not enforced.

**Status:** Accepted

---

## ADR-008 — The prototype runs on a laptop behind a tunnel

**Context.** Resolves O6. ADR-003 puts our server in the media path, so it must
be reachable from the carrier over public HTTPS (webhooks) and WSS (audio) for
the whole duration of every call. Demos are given from a laptop.

**Decision.** Run on the laptop, exposed through a tunnel. The public base URL is
configuration, never a constant in code. Prefer a named tunnel with a stable
hostname (e.g. Cloudflare) over a per-restart random URL.

**Consequences.** No hosting to build or pay for, and the edit-and-call loop
stays immediate — the point of the prototype.

The costs are real and accepted:

- **The laptop's network is inside the latency budget.** Audio traverses
  carrier → laptop → model and back, so home Wi-Fi jitter lands directly in the
  pause before the agent speaks. Under ADR-002 that pause *is* the product
  requirement. Demo hygiene (wired network, sleep disabled, no competing
  traffic) is therefore functional, not cosmetic.
- **The laptop is a single point of failure during a call.** A closed lid or a
  dropped link ends the call, and per ADR-005 it is recorded as `remote_ended` —
  indistinguishable from the person hanging up. Our own instability is therefore
  invisible in the data; treat `remote_ended` rates from laptop runs with
  suspicion.

This decision does not survive anyone outside the team relying on the system;
revisit it then, not before.

**Status:** Superseded by ADR-015

---

## ADR-009 — Recording disclosure is a policy option, off by default

**Context.** Resolves O1. ADR-003 retains call audio. Consent law differs across
our targets (ADR-010): as a participant, recording is permissible for us in
Spain; Germany's §201 StGB makes recording another party's spoken word without
consent a **criminal** offence, not merely a data-protection matter; Cyprus
requires a GDPR basis.

**Decision.** The spoken recording disclosure is a policy parameter (per
ADR-007), defaulting to **off** for the prototype.

**Consequences.** The opening seconds of the call stay natural, which is where
ADR-002's requirement is most fragile.

This default is only lawful under a condition that must be stated explicitly,
because it is invisible in the code: **it assumes every callee has consented out
of band** — ourselves and colleagues who agreed in advance. Consent need not be
spoken during the call, which is what makes the default acceptable at prototype
scale.

The moment a call goes to someone who has not separately consented, the
disclosure must be switched on — and for German destinations that is a criminal
threshold, not a policy preference. Anyone widening the callee list inherits this
obligation.

Alternative considered and rejected for now: retaining no audio and keeping only
the transcript (ADR-011). It removes the legal exposure but also removes the
reason for ADR-003's media-path architecture.

**Status:** Superseded by ADR-014

---

## ADR-010 — Spanish number; German and Cypriot destinations

**Context.** Resolves O2. The operator is based in Spain; the intended callees
are in Germany or Cyprus.

**Decision.** Buy a Spanish number on Twilio (ADR-004). Target destinations are
Germany and Cyprus.

**Consequences.** Spanish number provisioning requires a regulatory bundle with
address documentation, so this has lead time and belongs in the accounts step of
the roadmap, not on the day of a demo.

A Spanish caller ID calling a German number is a foreign number to the callee,
which depresses answer rates and raises spam-filter risk. If answer rates turn
out to be the bottleneck rather than conversation quality, buying a local number
per destination country is the remedy — cheap, and unaffected by anything else in
this architecture.

Germany's recording law is the binding constraint on ADR-009.

**Status:** Accepted

---

## ADR-011 — The transcript comes from OpenAI

**Context.** Resolves O3. The conversation record could come from the Realtime
API's own transcription events, from our own STT pass over the retained audio, or
both.

**Decision.** Use the Realtime API's transcription events as the transcript. No
second STT pass.

**Consequences.** No extra component, no extra cost, no extra latency, and the
transcript is aligned by construction with what the model actually heard — which
is what matters when diagnosing why it recorded a given answer.

Accepted risk: transcription quality is lower for Russian than for English, so
the transcript is a weaker source of truth once ADR-007's multilingual step
lands. The retained audio (ADR-003) remains the authority for any disputed
answer, and a second STT pass can be added later without disturbing anything —
it reads the same stored audio.

**Status:** Accepted

---

## ADR-012 — Hangup cause is inferred from the transcript, not obtained from the carrier

**Context.** Resolves O4. ADR-004 establishes that no carrier field distinguishes
a callee hangup from a dropped connection, and ADR-005 therefore merges both into
`remote_ended`. Paid Voice Insights would only report the direction of the SIP
BYE.

**Decision.** Do not buy Voice Insights. Where the distinction matters, infer it
from the conversation record (ADR-011): a call that ended after the person said
goodbye reads differently from one that stopped mid-utterance.

**Consequences.** No added cost, and the inference is likely better than
`disconnected_by` — which is `unknown` for many failure modes and says nothing
about intent.

The inference is a **heuristic, not a fact**, and must be stored as such: never
written back into `Call.end_reason`, which records only what we observed. Under
ADR-008 our own laptop dropping a call is indistinguishable from either case at
the carrier level, so this inference is also our main signal for our own
instability.

**Status:** Accepted

---

## ADR-013 — One call at a time

**Context.** Resolves O5.

**Decision.** The prototype places a single call at a time. No queue, no worker
pool — a loop over pending assignments.

**Consequences.** The runner stays simple enough to read in one sitting, and
there is no concurrent access to the JSONL results (ADR-006), so no locking.

This is a property of the runner alone. Concurrency later means adding a queue in
front of an unchanged call path, provided nothing in the session handling assumes
global state — which is the one thing to keep honest while this decision holds.

**Status:** Accepted

---

## ADR-014 — The prototype retains the transcript only; no call audio is stored

**Context.** Supersedes ADR-009. ADR-009 kept call audio and made the spoken
recording disclosure an off-by-default policy, resting on the assumption that
every callee had consented out of band. That assumption is fragile in exactly the
way defaults are: it is invisible at the point of use, and ADR-010 targets
Germany, where recording without consent is a criminal matter.

**Decision.** The prototype stores **no call audio**. The conversation record is
the transcript from the Realtime API (ADR-011) and nothing else. Audio frames
passing through the bridge are relayed and discarded, never written to disk.

The recording-disclosure policy parameter is dropped along with the recording; it
has nothing left to disclose.

**Consequences.** The §201 StGB exposure disappears — the statute governs
recording spoken word, not retaining notes of a conversation. The consent
condition that ADR-009 relied on is no longer load-bearing, so widening the
callee list no longer carries a hidden legal obligation. GDPR obligations over
the transcript and the answers remain, and are ordinary.

Two clauses of earlier ADRs lapse:

- **ADR-011's** designation of retained audio as the authority for a disputed
  answer. There is no such authority now; the transcript is the only record, with
  its known weakness on Russian.
- **ADR-003's** stated rationale. Sitting in the media path was justified
  primarily by audio retention, and that justification is gone. What remains is
  direct control over the stream and independence from whether the Realtime API
  offers a SIP path. The decision to keep the bridge stands for the prototype —
  it is small and already scoped — but it now rests on weaker grounds than when
  it was made. Tracked as O7 in `docs/plan.md`.

**Status:** Accepted

---

## ADR-015 — Runs on a VPS under a subdomain, deployed from GitHub Actions

**Context.** Supersedes ADR-008. Running from a laptop behind a tunnel carried
two costs that proved too high: home network jitter sat inside the latency budget
that ADR-002 depends on, and a closed lid or dropped link ended calls — recorded
as `remote_ended` (ADR-005) and therefore indistinguishable from the callee
hanging up, hiding our own instability in the data. An existing VPS and domain
are already available.

**Decision.** Deploy to an existing VPS under a dedicated subdomain, delivered by
GitHub Actions on push.

**Consequences.** Stable public HTTPS and WSS endpoints, a network path that does
not depend on where the operator is sitting, and a demo that does not require the
operator's machine to be awake.

Requirements this creates, none of which existed under ADR-008:

- **The public base URL is configured in two places** — the application's own
  configuration, and the Twilio number's webhook and media-stream URLs. These
  must agree; a mismatch fails silently, with calls simply never reaching us.
  Treat it as one setting recorded in two systems, not two settings.
- **Twilio webhook signatures must be validated.** A permanent public domain is
  discoverable and can be posted to by anyone, so signature validation is a
  correctness requirement, not hardening. This lands with the vertical slice.
- **Deploys must not interrupt a live call.** Under ADR-013 there is at most one,
  so draining before restart is sufficient. Without it, a deploy mid-call is
  recorded as `remote_ended` — reintroducing exactly the blind spot this ADR
  exists to remove.
- **Secrets live in GitHub Actions secrets and on the VPS**, never in the
  repository. The repository holds YAML inputs (ADR-006), which are not secret;
  credentials are not.

Two properties are inherited rather than chosen, and are worth naming because
they are easy to forget: the **VPS region is inside the latency budget** (audio
traverses carrier → VPS → model), and the VPS is **shared with unrelated
projects**, whose CPU or I/O spikes surface as audible hesitation in the agent —
the bridge is latency-sensitive, not bandwidth-hungry.

**Status:** Accepted

---

## ADR-016 — Configuration in YAML, operational data in Postgres

**Context.** Supersedes the storage clause of ADR-006, which put everything in
files (YAML in, JSONL out). That choice assumed a database was an added
dependency worth avoiding at prototype scale. It is not: Postgres already runs
both locally and on the VPS, so the real cost is a connection string. The
weakness of "files now, a database later" is that later rarely arrives.

Files are not uniformly wrong, though. The system holds two kinds of data with
opposite requirements, and ADR-006's mistake was treating them alike.

**Decision.** Split by kind:

- **Configuration — questions, questionnaires, policy — stays in YAML**, in git.
- **Operational data — people, assignments, calls, answers — lives in Postgres**,
  accessed through SQLAlchemy Core or psycopg with explicit queries. No heavyweight
  ORM mapping layer; at this size it earns nothing.

Schema changes are managed with Alembic and applied by the deploy (ADR-015).

**Consequences.** Configuration keeps the property ADR-007 exists to protect:
changing a question is a diff, reviewable and revertible, visible in the
project's history. Moving it into a database would make that edit require SQL or
a UI and leave no trace — strictly worse for the one thing that changes most.

Operational data gains what files cannot give: queries. "Who has not been reached
yet", "which question do people drop out on", "which assignments are `partial`"
are the core loop of the runner, and on files each is a full read-and-rewrite.

The split also resolves a contradiction the file-based design had left open.
Respondent phone numbers are personal data and are excluded from git — under a
public repository, emphatically so — which left people with nowhere to live but
an ignored file sitting beside the tracked ones. The boundary between
configuration and operational data now coincides with the boundary already drawn
in `.gitignore`: what is in git is safe to publish, what is personal is in the
database.

Costs, accepted:

- Migrations enter the deploy path, so a schema change can now break a
  deployment in a way a file format could not.
- **Tests require a running Postgres.** `AGENTS.md` demands a fast suite that
  never touches the network; a local database satisfies that literally but adds a
  precondition to running the tests. Tests run against real Postgres, not SQLite
  — a substitute engine would diverge from production exactly where storage bugs
  live, and testing against something we do not ship is a false guarantee.

**Status:** Accepted
