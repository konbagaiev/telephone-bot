# Spec: Ship the example configuration inside the Docker image

> **Finding — first live-smoke run of the vertical slice (roadmap step 4).**
> The very first `python -m src.runner` in the container failed with
> `ConfigError: /app/data/example/questionnaires.yaml: file not found`. The image
> carried code but not the runtime configuration: the slice loads questionnaires
> and policy at run time (both the runner and `/stream` call `load_config`), but
> the `Dockerfile` copied only `src`, `migrations`, and `alembic.ini`. This spec
> records shipping the config into the image.

## Goal
Make the runtime configuration (questionnaires + policy) present in the deployed
image, so the runner and the `/stream` handler can load it. Config travels with
the image, consistent with ADR-016 (configuration is YAML in git).

## Touches
- `Dockerfile` — add `COPY data/example ./data/example` before the install step.

## Does NOT touch
- **The whole `data/` tree** — only `data/example` is copied. `data/*` is
  git-ignored except the example (real respondent data may sit beside it locally
  and must never enter the image, per AGENTS.md / `.gitignore`).
- **`CONFIG_DIR`** — its default (`data/example`) is unchanged; the fix makes that
  path exist in the container rather than repointing it.
- **Application code** — this is a packaging fix only; no source changes.
- **A mounted-volume / external-config approach** — deliberately not adopted;
  baking the tracked config into the image matches "config in git, deployed with
  the code" and is simplest for the demo.

## Acceptance criteria
- The container has `/app/data/example/questionnaires.yaml` and `policy.yaml`.
  **Verified: after the fix, `docker exec … ls /app/data/example` lists both, and
  the runner loaded config and placed a call.**
- No git-ignored real data is present in the image (only `data/example`).

## What could go wrong (risks & guards)
- **Risk: copying `data/` wholesale bakes personal data into a public image.** →
  Guard: copy only `data/example`; on the VPS the build context is the git
  checkout, which contains nothing but the tracked example anyway.
- **Risk: config drift between the image and what a call expects** — the image is
  a snapshot at build time. → Guard: the deploy rebuilds the image from
  `origin/main` on every push, so a question edit ships on the next deploy (the
  "config-per-call" property still holds within a running process; across a
  content change it needs a redeploy — acceptable at demo scale).

## Non-goals
- Not moving configuration to a mounted volume or a config service.
- Not changing how config is read (`load_config`, `CONFIG_DIR`) — only ensuring
  the files exist in the image.

## Status
Implemented and deployed in commit `40ea3fc`; verified by the subsequent call
reaching `/voice` and `/stream` with config loaded. Recorded here after the fact —
this fix shipped during the live smoke before its spec existed.
