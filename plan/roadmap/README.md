# Roadmap Plans

Status: current index.

Cross-cutting plans that shape multiple implementation areas.

## Read First

- [current-code-status.md](current-code-status.md): canonical static
  inspection snapshot for what is implemented now and what remains active.
- [combined-cleanup-tasks.md](combined-cleanup-tasks.md): triaged cleanup
  backlog derived from April audit history. Re-check current code before
  executing any item.

## Active

- [2026-08-02-bug-resistant-architecture.md](2026-08-02-bug-resistant-architecture.md):
  design reference for eliminating the recurring implementation-bug classes
  (lifecycle/cancellation races, staleness fencing, N-times peer fixes) by
  construction — primitives, engines, and enforcement ratchets.
- [2026-08-02-bug-resistant-refactor-plan.md](2026-08-02-bug-resistant-refactor-plan.md):
  the PR-level backlog for that design — tiered workstreams with acceptance
  criteria, the peer-set blocking decision, and re-measurement gates.

## Historical Architecture

- [essential-debug-first-runtime.md](essential-debug-first-runtime.md):
  original debug-first runtime architecture and rationale. Core pieces have
  landed, but detailed class names, line counts, and task sequencing are not
  authoritative anymore.

Prefer updating `current-code-status.md` for factual source-tree status and
`combined-cleanup-tasks.md` for executable backlog changes.
