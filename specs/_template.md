# Spec: <short title>

> One spec = one intended change. Copy this file into `specs/active/` and name
> it `YYYY-MM-DD-HHMM-<slug>.md` (date + hours + minutes). Fill it in, implement,
> verify, then move it to `specs/done/`.

## Goal
What change is requested, and why. One or two sentences.

## Touches
- Files / slots this change edits: …

## Does NOT touch
- Explicitly out of bounds (guards against scope creep): …
- Default: the transport layer `src/telephony/` is off-limits for behavior changes.

## Acceptance criteria
How we know it's done. Prefer a concrete simulator run:
- Given scenario `…`
- When run via `python -m sim`
- Then the collected JSON contains `…`

## What could go wrong (risks & guards)
List 1–3 *real* failure modes this change could introduce. For each, name the
guard — a test where a test makes sense, else why not (eval / manual smoke /
existing test / accepted). Don't manufacture risks.
- Risk: … → Guard: …

## Non-goals
What we are deliberately NOT doing in this change.
