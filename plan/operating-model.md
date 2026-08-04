# Plan Folder Operating Model

Status: design reference.

The `plan/` folder should help contributors decide what to do next without
pretending to be the source of truth for implemented behavior. Code, tests,
README files, and CI define current behavior; plans capture decisions,
tradeoffs, work queues, and historical rationale.

## Document Types

Every plan document should fit one of these roles:

| Type | Purpose | Upkeep rule |
|---|---|---|
| Current-state snapshot | Summarize what the codebase does now. | Update from source inspection; include snapshot date and whether tests ran. |
| Active backlog | List work we intend to execute. | Keep tasks small, ordered, and tied to acceptance checks. |
| Design reference | Preserve rationale, research, command vocabulary, or architecture options. | Label planned items as planned; avoid making it look like shipped behavior. |
| Historical record | Keep completed or superseded plans useful for context. | Add a status banner and link to the current source of truth. |

If a document mixes these roles, split it or add clear sections named
`Current State`, `Backlog`, `Reference`, and `Historical Notes`.

## Directory Roles

- `roadmap/`: cross-cutting status and backlog. Keep
  `roadmap/current-code-status.md` as the broad source-tree snapshot.
- `validation/`: active validation strategy and implementation backlog. This
  should stay the highest-signal active plan until the validation CLI, reports,
  and CI artifacts exist.
- `workstreams/`: historical implementation records for the debug-first
  runtime redesign. Re-open items only after checking current code.
- `session-decomposition/`: historical extraction plans plus residual cleanup
  guidance for shrinking `Session`.
- `peripherals/`: separable follow-up initiatives. Promote work out of here
  only when it becomes part of an active roadmap slice.
- `teaching/`: chapter planning records. Shipped curriculum belongs in
  `docs/teaching/`.
- `testing/`: broad test strategy plans that are larger than one feature PR.

## Status Labels

Use a short status line near the top of each planning doc:

- `Status: active backlog` for work expected to drive near-term PRs.
- `Status: current snapshot` for source-tree inventory documents.
- `Status: design reference` for research or architecture documents.
- `Status: historical record` for completed, superseded, or drift-prone plans.

When a doc has drift, say what drift is known instead of silently editing old
history. Example: "The class name in this plan did not land; current behavior
lives in `CancelOrchestrator` and `TurnRunner`."

## Promotion Flow

1. Research starts as `reference.md` or a clearly labeled design note.
2. Work becomes real when it moves into an active backlog with acceptance
   checks, target files, and test evidence.
3. Completed work graduates into code, tests, and user docs.
4. The plan becomes a historical record or is reduced to a short status note.

This keeps the folder from accumulating long checklists that look actionable
after the code has already moved on.

## Review Cadence

- At the start of a larger work session, read `plan/README.md` and
  `roadmap/current-code-status.md`.
- Before implementing from any old checklist, verify the named files/classes
  still exist with `rg` or `find`.
- When a PR changes behavior covered by an active plan, update that plan in
  the same PR or mark the plan as stale.
- After a major feature lands, update the relevant subdirectory README and
  move detailed completed plans to historical status.
- Periodically refresh `roadmap/current-code-status.md` with a static
  inspection date and explicit test evidence.

## Backlog Shape

Active task docs should be easy to execute:

- one ordered list of slices;
- clear dependencies between slices;
- target files or modules;
- acceptance checks;
- suggested focused test commands;
- explicit out-of-scope notes for tempting adjacent work.

Avoid mixing raw research, old audit notes, and ready-to-run tasks in the same
checklist. Keep raw notes in `archive/` or a reference document.

## Hygiene Checks

For documentation-only plan changes, run at least:

```bash
find plan -name '*.md' -print
rg 'easycat validate|scripts/validate.py|EventTraceLogger|SpanManager|InMemoryMetrics|agent_runner.py' plan -n
```

Then inspect whether the matches are intentionally current, planned, or
historical. For larger reorganizations, also run a Markdown relative-link
check before committing.

## Amendments (2026-08-04)

Added after the reorganization that retired `neo/`, `workstreams/`,
`session-decomposition/`, `teaching/`, `testing/`, and `validation/`. The
document types and promotion flow above were sound; the tree simply stopped
complying with them. These amendments close the gaps that allowed that.

### Two more status labels

Eleven files independently invented their own status strings, which is evidence
the original four were incomplete. Add:

- `Status: shipped` for a plan whose work is fully in code, tests, and user
  docs, kept only until it is archived.
- `Status: index` for a README whose entire job is routing to other documents.

Every document in `plan/` carries exactly one status label. A document with no
status line is a defect — 21 of 50 files had none before this reorganization,
and they were the hardest to judge.

### A directory must not exist solely to host its own index

If a subdirectory's only content is its README, delete the directory and fold
the content into the parent index. Three directories failed this test.

Related admission rule, carried forward from the retired `testing/` index: add a
document only for material that is **not** already represented by a current test
module, a maintained docs route, or shipped code. Prefer a test over a plan.

### Archive rather than delete, but archive decisively

Move a superseded document to `archive/` with a `Status: historical record`
banner naming the current source of truth. Do this as a pure `git mv` and add
the banner in a separate commit, so rename detection stays at R100 and
`git log --follow` keeps working.

`archive/` has no index by design: each file carries its own banner, and nothing
routes work through the directory. Deleting is reserved for genuine duplicates
and for documents fully superseded by shipped code plus tests.

### Corrected directory roles

- `roadmap/`: the active program, the open backlog, the completion log, and the
  source-tree snapshot.
- `peripherals/`: separable follow-up initiatives, each item pinned to a source
  path.
- `metrics/`: pre-registered measurement inputs. The JSON files are frozen by
  test; never edit them to make a gate pass.
- `critique/`: adversarial audits, kept for their findings, not their status.
- `archive/`: retired plans.

### Word count is a symptom, not the target

`plan/` reached 229k words while the repo's own critique named the meta-layer a
second product competing for the same maintenance budget. The metric that
matters is the **reading path** — what a maintainer must read to decide what to
do next — not the total. Archiving reduces the first without reducing the
second, and that is the correct trade.
