# Plan Index

Status: current index.

This directory is organized by intent. Start here, then follow the
subdirectory README for the area you are working on.

## Current Entry Points

- [neo/README.md](neo/README.md): active next-major planning packet for
  `VoiceApp`, `VoiceServer`, browser-first development, production manifests,
  evals, replay-as-tests, and budgets.
- [roadmap/current-code-status.md](roadmap/current-code-status.md): latest
  static code inspection snapshot used to judge which plans are still current.
- [validation/README.md](validation/README.md): active validation strategy,
  implementation backlog, and source research.
- [operating-model.md](operating-model.md): rules for keeping this folder
  useful as plans age, land, or become historical.
- [archive/2026-04-combined-cleanup-audit.md](archive/2026-04-combined-cleanup-audit.md):
  consolidated cleanup backlog derived from earlier audit notes.
- [peripherals/README.md](peripherals/README.md): separable follow-up
  initiatives, including DX, CLI, redaction, observability, provider, and
  deployment work.

## Directory Map

| Directory | Purpose |
|---|---|
| [neo/](neo/README.md) | Active next-major product/platform plan for app-first APIs, production server runtime, and feedback loops. |
| [validation/](validation/README.md) | Validation strategy, recurring checks, latency/provider coverage, and implementation tasks. |
| [roadmap/](roadmap/current-code-status.md) | Cross-cutting product and architecture plans. |
| [workstreams/](workstreams/README.md) | Historical workstream records for the debug-first runtime redesign. |
| [session-decomposition/](session-decomposition/README.md) | Historical extraction phases plus residual guidance for reducing `Session` ownership. |
| [peripherals/](peripherals/README.md) | Valuable but separable follow-up initiatives. |
| [teaching/](teaching/README.md) | Historical teaching ladder planning; shipped curriculum lives in `docs/teaching/`. |
| [testing/](testing/README.md) | Historical broad test strategy plans backed by concrete tests. |

## Maintenance Rules

- Keep the root of `plan/` as an index only.
- Use [operating-model.md](operating-model.md) when adding, promoting,
  archiving, or refreshing plans.
- Put new documents in the narrowest matching subdirectory.
- Add new active work to the appropriate subdirectory README.
- Prefer actionable task files over long research dumps when a plan is
  ready for implementation.
- Keep only source research and historical notes that still have a named
  reader or acceptance role; otherwise summarize them in an index and remove
  the raw dump.
