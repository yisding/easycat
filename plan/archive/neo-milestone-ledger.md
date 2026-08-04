# Neo packet: milestone ledger

> **Status: historical record.** Archived 2026-08-03, replacing the ten
> documents that were `plan/neo/`. Current source of truth: the shipped code
> and its docs — [../../docs/architecture.md](../../docs/architecture.md) for
> the layering, [../../docs/public-api.md](../../docs/public-api.md) for the
> surface — plus [../roadmap/open-backlog.md](../roadmap/open-backlog.md) for
> the parts of Phase 3 that are still open. Nothing in this file is
> actionable.

The neo packet proposed a next-major product surface in three phases:
`VoiceApp`, `VoiceServer`, and a feedback loop. All eleven of its documents
declared `Status: active` while the packet was entirely backward-looking.
This ledger records what actually happened to each milestone so nobody
rebuilds shipped code.

## Milestones

Every SHA in the "Landed as" column was checked with `git log -1`; every
Phase 1/2 SHA is an ancestor of `main`, and no Phase 3 SHA is.

| Milestone | Landed as | PR | State |
|---|---|---|---|
| M1 VoiceApp run modes | `6d6e755e` (2026-06-14) | #283 | Merged to `main` |
| M2 serve routed through VoiceApp | `00a512ca` (2026-06-14) | #283 | Merged to `main` |
| M3 VoiceApp Twilio mode | `e7c0d4ae` (2026-06-14) | #283 | Merged to `main` |
| M4 VoiceServer skeleton | `7162fefa` (2026-06-14) | #284 | Merged to `main` |
| M5 auth and graceful draining | `7bcef6d3` (2026-06-15) | #284 | Merged to `main` |
| M6a manifest loader + secret-redaction contract | `9668c0bd` (2026-06-15) | #284 | Merged to `main` |
| M6b provider planning + readiness wiring | `01584756` (2026-06-15) | #284 | Merged to `main` |
| M7 WebRTC in VoiceServer | `934e4d1a` (2026-06-15) | #284 | Merged to `main` |
| M8 server metrics + read-only endpoints | `ae68f551` (2026-06-15) | #284 | Merged to `main` |
| M9 shared budget API | `8aed223c` (2026-06-17) | #290 | **Closed, never merged** |
| M10 eval scenario runner | `a7216a90` (2026-06-17) | #290 | **Closed, never merged** |
| M11 promote bundle turns to tests | `ec3a977a` (2026-06-17) | #290 | **Closed, never merged** |
| M12 first-audio budget milestones | `e9d1f8bb` (2026-06-17) | #290 | **Closed, never merged** |
| M13 dev session dashboard | `002dda54` (2026-06-17) | #290 | **Closed**; re-landed independently — see below |

PR #283 ("Neo Phase 1: VoiceApp foundation (M1–M3)") and #284 ("Neo Phase 2:
VoiceServer, manifest projects, provider planning (M4–M8)") both merged on
2026-06-20 as `68418254` and `61893bb7`. PR #290 ("Neo Phase 3: Feedback loop
— M9-M13") is `CLOSED` with `mergedAt: null`.

## Three things a future reader must not get wrong

**M13 was independently re-landed on `main`.** Do not rebuild it. The
always-available dev debugger shipped as `2d159801` (2026-06-28) — see
`src/easycat/debugger/dev.py`, `src/easycat/debugger/session_registry.py`, and
the `EASYCAT_DEV` arming at `src/easycat/cli/serve.py:161-166`. This is a
different implementation from the closed branch's `002dda54`.

**The budget foundation M9 and M12 built on was deleted.** Commit `db3ca9cc`
(2026-06-28, "session: remove runtime cost and latency-budget features")
removed `session/_latency_budget.py`, `session/_cost_budget.py`,
`runtime/costs.py`, `max_session_cost_usd`, the `cost_budget_*` records, and
the debugger cost rollup as undercooked and duplicative with the journal.
Latency is reported (`turn_total_latency_ms`, `text_turn_latency_ms`, per-stage
`elapsed_ms`) but not gated. Reviving cost budgets is an explicit do-not-revive
decision in [../roadmap/open-backlog.md](../roadmap/open-backlog.md); it needs
a new decision, not a resumed milestone.

**A full unmerged Phase 3 implementation still exists on a remote branch.**
`origin/neo/phase-3-feedback-loop` holds ten commits, `8aed223c..32daebd5`,
and is 2,065 commits behind `main`. The commit that corrects the packet's own
status (`ebb20bd6`, "plan: mark Neo phases 1-2 shipped, phase 3 in progress")
lives only on that branch, which is why `plan/neo/` never learned it had
shipped. Treat the branch as a reference implementation to read, not to
rebase.

## Where the rest went

- The Layer / Owns / Must-Not-Own table from `architecture-boundaries.md` was
  promoted into [../../docs/architecture.md](../../docs/architecture.md) as a
  stated invariant.
- The genuinely unstarted Phase 3 content — the evals package, promote
  hardening, the pytest-free constraint on `easycat.evals`, the
  redact-by-default promotion defaults, and the `journal promote` privacy
  defect — moved to [../roadmap/open-backlog.md](../roadmap/open-backlog.md).
- The adversarial review of the packet is retained separately at
  [neo-plan-review.md](neo-plan-review.md).
