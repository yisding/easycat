# REFACTOR — Implementation Planning

This folder is a **planning deliverable**, not code. It contains detailed, step-by-step
implementation guides for every recommendation produced by two adversarially-verified audits of
the EasyCat codebase, plus the sequencing needed to land them safely.

Nothing here changes source code. Each document is written so an engineer (or an agent) can pick
up a single item and implement it without re-reading the original audit — every item carries its
file:line anchors, concrete steps, the lockstep test/doc edits, and the exact validation command.

## How the two audits were produced

Two multi-agent workflows ran over the repo: parallel researchers/hunters fanned out across
subsystems and 2026-practice topics, every candidate was then **adversarially verified** against
the live code by a skeptic whose default was to reject it, and the survivors were synthesized into
the two reports under [`reports/`](reports/). Counts after verification:

- **Maintainability plan** — 22 confirmed proposals (8 rejected).
- **Code-quality report** — 44 confirmed findings (12 refuted).

## Start here

1. **[`SEQUENCING.md`](SEQUENCING.md)** — the execution order. Hard dependencies, the shared-file
   collision table (where a refactor and a bug fix touch the same file), and a recommended wave
   order. **Read this before starting any item.**
2. Pick an item from the area docs below.
3. Check **[`rejected.md`](rejected.md)** before proposing anything new — 20 ideas were
   deliberately killed with reasons; don't re-litigate them.

## Documents

### Maintainability (`maintainability/`)
| Doc | Covers |
|-----|--------|
| [`01-quick-wins.md`](maintainability/01-quick-wins.md) | QW1–QW11 — hours-each config/CI/tooling/API wins |
| [`02-structural-refactors.md`](maintainability/02-structural-refactors.md) | QS1–QS7 — the line-budget ratchet, module splits, Import Linter, LaneHarness, WebRTC convergence |
| [`03-process-automation.md`](maintainability/03-process-automation.md) | QP2 — PyPI Trusted Publishing release workflow |

### Code quality (`code-quality/`)
| Doc | Covers |
|-----|--------|
| [`01-priority-bugs.md`](code-quality/01-priority-bugs.md) | 2 HIGH + medium bugs and functional error-handling defects |
| [`02-low-bugs-and-error-handling.md`](code-quality/02-low-bugs-and-error-handling.md) | Low-severity bugs and error-handling (#18–#22, #32–#37) |
| [`03-duplication-consolidation.md`](code-quality/03-duplication-consolidation.md) | Duplication that drifted alongside existing shared abstractions (#8–#10, #26–#31) |
| [`04-architecture-and-smells.md`](code-quality/04-architecture-and-smells.md) | Layering/hot-path fixes (#15–#17, #38) and smells/cleanups (#23–#25, #39–#43) |

### Provenance (`reports/`)
The raw synthesized audit output, preserved verbatim:
[`maintainability-plan.md`](reports/maintainability-plan.md) ·
[`code-quality-report.md`](reports/code-quality-report.md).

## Decisions already locked

- **License:** BSD-2-Clause, `Copyright (c) 2026 Yi Ding`. This resolves the only owner-gated
  question in the maintainability plan (QW5) and unblocks the publish workflow (QP2).

## The two things worth doing this week

Everything is prioritized in `SEQUENCING.md`, but if you only touch two things:

1. **OpenAI Agents bugs #1 + #2** (`code-quality/01-priority-bugs.md`). On any tool-using turn,
   a barge-in leaves the SDK run executing in the background — billing tokens, firing tool
   side-effects after the user interrupted, and feeding nondeterministic state into the next turn.
   These are the only HIGH-severity findings and both have a correct sibling (`LlamaAgentsBridge`)
   to mirror.
2. **QS1 module line-budget ratchet** (`maintainability/02-structural-refactors.md`). One guard
   test that freezes the 13 oversized files from growing further — cheap, and it protects every
   split that follows.

## Status legend (for tracking as items land)

Suggested convention if you check items off in-place: `TODO` · `IN PROGRESS` · `LANDED (PR #…)` ·
`DEFERRED`. Nothing is started yet — this is the plan.
