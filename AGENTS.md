# AGENTS.md

> Entry point for any AI agent (or human) working in this repo.
> Keep this file SHORT and **architecture-agnostic**: durable rules and process
> only. Anything specific to *how the app is built* lives in
> `docs/architecture.md` and changes as the architecture evolves.

## What this is

An outbound voice agent: it places a phone call to a given number, asks a short
list of questions, and collects structured answers. The point of the codebase is
that a small change to the agent's behavior is **fast and safe to make** —
including live, with AI assistance.

## Rules (durable, independent of architecture)

- **Docs and code comments are in English.** (What the bot *says* to the callee
  may be in any language.)
- **One spec per change** — see `specs/_template.md`.
- **Name specs by timestamp:** `YYYY-MM-DD-HHMM-<slug>.md` (date + hours +
  minutes). A timestamp needs no "next number" lookup, so specs can be created
  on the fly without collisions.
- **ADRs are append-only** — never edit an accepted decision; supersede it.
- **A resolved fork is not done until it's an ADR.** Whenever we pick one viable
  option over another — or deliberately reject one — record it in
  `docs/decisions.md` as part of the same change, not only in a commit message,
  spec, or chat. Reasoning left in a commit or a session is reasoning the next
  reader can't find. The bar is a *genuine fork*: a mechanical choice with no
  viable alternative stays out; a design or architecture decision earns an ADR
  (route it through `docs/plan.md` first if it needs discussion — see
  **Decisions**).
- **Never `git commit` or `git push` without the user's explicit request or
  consent.** Do the work, run the tests, show what changed — then stop and let the
  user decide when it lands. This includes not amending or pushing existing
  commits on your own initiative. "Implement X" is not permission to commit X.
- Keep this file a **router**: link to deeper docs, don't inline architecture.
- **Never let a shell operator into a search pattern.** An unquoted `>` `<` or
  `|` in a `grep` argument is read by the shell as redirection and *truncates the
  target file to zero bytes before the command runs* — this has already emptied
  two source files in this repo. Quote patterns (`grep '\->' file`) or use a
  dedicated search tool. If a file turns up emptied right after a command you
  ran, suspect that command first — verify before blaming anything external.

## Testing

Tests are the guardrail that makes an AI-assisted change safe: after a change,
the suite catches a break in seconds. Rules:

- **Every behavior change ships with a test** (or an updated acceptance check).
- **Each spec names its failure modes and how each is guarded** (test where
  sensible; else eval / manual / accepted) — see `specs/_template.md`.
- **Test behavior and contracts, not implementation.**
- **Test what's deterministic and ours; mock the external edges** (the LLM
  provider, the telephony carrier). The suite never touches the network.
- **Never assert the model's exact wording** — it's flaky. Test *your handling
  of the model's output* (inject a synthetic tool-call event), not the output.
- **Fast and deterministic** — runs in CI on every change.
- Conversation-level "does it really work" checks are **evals** (model in the
  loop, few, on demand), not unit tests. Keep the two separate.

Concrete layering and where the mocks sit will be defined in
`docs/architecture.md` once the architecture is ratified.

## Deeper docs

- `docs/architecture.md` — how it's built right now (a stub until code lands;
  will hold the module map, conventions, and the "where to make a change" map
  once built). **Read before editing and investigating code.**
- `docs/plan.md` — open decisions to discuss / accept / reject (mutable
  antechamber to `decisions.md`).
- `docs/decisions.md` — why we chose what we chose (append-only ADRs, resolved only).
- `specs/` — one spec per intended change.

## Decisions

Open questions live in `docs/plan.md`. When resolved they become ADRs in
`docs/decisions.md` (Accepted or Rejected). Never put open debate in
`decisions.md` — it is append-only and holds resolved decisions only.

## Change workflow

1. Turn the request into a spec in `specs/active/` (copy `_template.md`).
2. _(Future)_ A critic agent reviews the spec.
3. Implement the change per the spec.
4. Verify the change (how to run/test: see `docs/architecture.md`).
5. If the change involved a real fork → record it via `docs/plan.md` → `docs/decisions.md` (see **Decisions** above).
6. If the *shape* of the architecture changed → update `docs/architecture.md`.
7. Move the spec to `specs/done/`.
