# Plan Index

Status: current index.

This directory is organized by intent. Start here, then follow the
subdirectory README for the area you are working on.

## Current Entry Points

- [roadmap/current-code-status.md](roadmap/current-code-status.md): latest
  static code inspection snapshot used to judge which plans are still current.
- [validation/README.md](validation/README.md): active validation strategy,
  implementation backlog, and source research.
- [operating-model.md](operating-model.md): rules for keeping this folder
  useful as plans age, land, or become historical.
- [roadmap/essential-debug-first-runtime.md](roadmap/essential-debug-first-runtime.md):
  debug-first runtime redesign and its required work.
- [roadmap/combined-cleanup-tasks.md](roadmap/combined-cleanup-tasks.md):
  consolidated cleanup backlog from the earlier audit notes.

## Directory Map

| Directory | Purpose |
|---|---|
| [validation/](validation/README.md) | Validation strategy, recurring checks, latency/provider coverage, and implementation tasks. |
| [roadmap/](roadmap/README.md) | Cross-cutting product and architecture plans. |
| [workstreams/](workstreams/README.md) | Operational workstream plans for the debug-first runtime redesign. |
| [session-decomposition/](session-decomposition/README.md) | Focused session split phases that reduce `Session` ownership. |
| [peripherals/](peripherals/README.md) | Valuable but separable follow-up initiatives. |
| [teaching/](teaching/README.md) | Teaching ladder chapter plans. |
| [testing/](testing/README.md) | Test plans that are broader than a single unit or feature PR. |

## Maintenance Rules

- Keep the root of `plan/` as an index only.
- Use [operating-model.md](operating-model.md) when adding, promoting,
  archiving, or refreshing plans.
- Put new documents in the narrowest matching subdirectory.
- Add new active work to the appropriate subdirectory README.
- Prefer actionable task files over long research dumps when a plan is
  ready for implementation.
- Keep source research and historical audit notes, but label them as
  reference material.
