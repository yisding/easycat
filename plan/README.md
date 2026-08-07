# Plan Index

Status: index.

`plan/` records decisions, work queues, and rationale. Code, tests, `docs/`,
and CI define current behavior. Read [operating-model.md](operating-model.md)
before adding, promoting, retiring, or refreshing anything here.

## Start Here: The Bug-Resistant Refactor

This is the active program. Read it in this order.

1. [roadmap/2026-08-02-bug-resistant-refactor-plan.md](roadmap/2026-08-02-bug-resistant-refactor-plan.md)
   — the PR-level backlog: tiered one-concern slices, target files,
   acceptance criteria, the session-refactor invariants, and the
   optional re-measurement telemetry.
2. [roadmap/2026-08-02-bug-resistant-architecture.md](roadmap/2026-08-02-bug-resistant-architecture.md)
   — the design reference behind it: the recurring bug classes, the
   primitives and engines that remove them, and the enforcement ratchets.
3. [roadmap/2026-08-03-peer-set-adr.md](roadmap/2026-08-03-peer-set-adr.md)
   — the accepted peer-set decision that unblocks the per-peer slices, with
   twelve per-peer obligations and three revisit triggers.
4. [roadmap/2026-08-02-bug-resistant-completion-log.md](roadmap/2026-08-02-bug-resistant-completion-log.md)
   — what the finished slices actually did, kept out of the backlog so the
   backlog reads forward.

## Everything Else We Intend To Do

- [roadmap/open-backlog.md](roadmap/open-backlog.md): the single queue for
  work outside the bug-resistant program — security and privacy, provider and
  bridge contract gaps, evals and promote hardening, API-DX papercuts,
  structural and validation residue, and the explicit do-not-revive
  decisions.
- [peripherals/README.md](peripherals/README.md): separable follow-up
  initiatives, each item pinned to a source path.
- [peripherals/peripheral-deployment.md](peripherals/peripheral-deployment.md):
  the per-platform deployment tiers, runbooks, and rejection rationale that
  `docs/deployment/` does not carry yet.

## Frozen Records

- [roadmap/current-code-status.md](roadmap/current-code-status.md): the
  source-tree snapshot used to judge whether an older claim is still current.
- [metrics/README.md](metrics/README.md) and its JSON artifacts: the
  pre-registered inputs and generated outputs for optional longitudinal
  outcome observations. Never edit them to manufacture a favorable result.
- [critique/2026-07-26-full-critique.md](critique/2026-07-26-full-critique.md):
  the 2026-07-26 adversarial audit. Historical; its still-live residue was
  moved into `roadmap/open-backlog.md`.
- [archive/](archive/): retired plans. Each file carries its own status
  banner naming the current source of truth, so the directory has no index
  and nothing routes work through it.

## Directory Map

| Directory | Operating-model role | Contents |
|---|---|---|
| [plan/](README.md) | index | This file plus [operating-model.md](operating-model.md). No other document lives at the root. |
| [roadmap/](roadmap/current-code-status.md) | active backlog, design reference, current snapshot, historical record | The bug-resistant program, the open backlog, the completion log, and the code-status snapshot. |
| [peripherals/](peripherals/README.md) | active backlog | Separable follow-ups that are not on the bug-resistant critical path. |
| [metrics/](metrics/README.md) | design reference plus pre-registration artifacts | Pre-registered cohorts, thresholds, and adjudications for optional longitudinal reporting. Test-frozen. |
| [critique/](critique/2026-07-26-full-critique.md) | historical record | The full 2026-07-26 audit, kept byte-stable because other documents cite its findings by number and anchor. |
| [archive/](archive/) | historical record | Retired plans, banner-per-file. Off the reading path by construction. |

## Maintenance Rules

- Keep the root of `plan/` to this index and `operating-model.md`.
- Use [operating-model.md](operating-model.md) when adding, promoting,
  archiving, or refreshing a plan, and label every document with one of its
  status labels.
- Put new work in the narrowest existing backlog. Do not create a directory
  that would exist only to host its own index.
- Prefer adding a slice to an existing backlog over adding a file. A new file
  earns its place when it needs its own acceptance criteria.
- Retire by moving to `archive/` with a status banner naming the current
  source of truth. Do not delete, and do not silently rewrite old history —
  state the known drift instead.
- Update this index only when a directory appears or disappears.
