# Refactor outcome measurement

Status: pre-registered inputs from WS6.1a and the WS6.1b report engine for the
[bug-resistant refactor plan](../roadmap/2026-08-02-bug-resistant-refactor-plan.md).

These files freeze the measurement choices before the bug-resistant refactor
changes production code. The report is longitudinal telemetry and never blocks
refactor sequencing:

- `refactor-families.json` owns cohorts, controls, bug classes, thresholds, and
  completion anchors;
- `adjudications.json` owns the human classifications used by fix-density and
  recurrence calculations;
- `report.schema.json` owns the generated JSON output contract; and
- `report.json` and `report.md` are the checked generated views.

`scripts/refactor_metrics.py` is the sole report generator. Generated reports
must never be edited to supply classifications or incidents. Use an explicit
UTC decision timestamp so reruns are reproducible:

```bash
uv run python scripts/refactor_metrics.py --as-of 2026-08-02T00:00:00Z
uv run python scripts/refactor_metrics.py --as-of 2026-08-02T00:00:00Z --check
```

For a real observation, replace the example timestamp with the chosen review
time. `--check` compares both checked outputs byte-for-byte with a fresh
first-parent history calculation.

## Frozen history and exposure rules

Measurement walks the first-parent history of the branch containing the cohort's
completion SHA. A commit's diff is taken against its first parent, so a merged PR
counts once; a direct commit also counts once. Merge-side commits are not counted
again. Timestamps are committer timestamps normalized to UTC.

For a completion anchor `D`, the pre-window is exactly `[D-60d,D)` and the
post-window is exactly `[D,D+60d)`. A member or control is touched when its
first-parent diff changes at least one registered path. Exposure is:

- the number of distinct touching commit SHAs; and
- changed lines, the sum of numeric additions and deletions from `--numstat`
  for registered paths. Binary entries contribute zero changed lines.

Both the touching-commit and changed-line minima in the cohort manifest must be
met in both windows. Control exposure is pooled after deduplicating commit SHAs.
A zero denominator or under-exposed treated/control window is
`insufficient_data`, never a pass.

Only SHAs listed in `anchor.migration_commits` are excluded as migrations.
Message heuristics, date ranges, and broad path exclusions are forbidden. When
the treatment completes, the anchoring PR records every treatment/migration SHA,
the immutable merge SHA and date, and the four computed window endpoints.

## Completion anchors and peer selection

An anchor has four states:

- `pending`: treatment membership is frozen but treatment is incomplete;
- `blocked`: membership cannot yet be frozen because the peer ADR is missing
  (no cohort is in this state since the 2026-08-04 lock);
- `active`: the completion SHA/date, migration SHAs, and exact windows are
  recorded; or
- `superseded`: retained only under `superseded_anchors` after a reset.

All three cohorts are now pre-registered with frozen membership.

The bridge and transport cohorts were locked on 2026-08-04 by the peer-set ADR
(merge SHA `df517aeca8409b9cd5eab3b0767d837ec41b0afe`, recorded in each cohort's
`member_selection`). The ADR retained every candidate in-tree, so each cohort's
`members` array is its former candidate set copied verbatim: seven agent bridges
and five transports. `candidate_members` was removed at lock time — keeping both
would let the two drift with no rule saying which one counts. The exact member
ids are pinned by `tests/test_refactor_metrics.py`, so a later addition or
removal fails loudly rather than silently changing what a peer-family outcome
means.

A peer cannot be added after treatment begins; removing one after treatment begins invalidates that
cohort rather than silently changing the denominator.

A later production change that extends the treatment before the post-window
closes resets `D`. Append the old anchor to `superseded_anchors`, record the new
completion SHA/date and complete migration list, then recompute both windows.
Never rewrite or delete the prior anchor.

## Commit classification and recurrence adjudication

Every non-migration touching commit in both windows must have one
`commit_classifications` entry for its cohort:

```json
{
  "cohort_id": "tier_a_session_lifecycle_staleness",
  "sha": "40 lowercase hexadecimal characters",
  "classification": "fix",
  "bug_classes": ["lifecycle_cancellation"],
  "affected_members": ["runtime_scope"],
  "evidence": ["issue, PR, revert, reproduction, or diff reference"],
  "rationale": "why the change is or is not a fix in a declared class",
  "reviewer": "GitHub handle",
  "reviewed_at": "UTC RFC 3339 timestamp"
}
```

`classification` is `fix` or `not_fix`. A fix requires at least one declared
bug class and member; `not_fix` requires empty class/member arrays. Missing,
duplicate, contradictory, or unresolved classifications make the cohort
`insufficient_data`. The named cohort reviewer owns classifications; a subject
author may provide evidence but cannot self-resolve a dispute.

For each bug class and each window independently, order treated fix commits by
`(committer timestamp, SHA)`. Starting with the earliest unassigned commit, a
candidate cluster contains it and every subsequent unassigned fix less than or
equal to seven days after that first timestamp. A cluster is a recurrence
candidate only when it has at least two distinct commits and the union of
affected members has at least two members. A fix attributed only to a control
does not enter a treated recurrence cluster, even if its commit also touched a
treated path. A single well-factored commit touching several members is
therefore counted once as a fix, not as a recurrence. Clusters never bridge the
pre/post boundary.

Each candidate needs one `recurrence_adjudications` entry:

```json
{
  "candidate_id": "stable generator-owned identifier",
  "cohort_id": "tier_a_session_lifecycle_staleness",
  "bug_class": "lifecycle_cancellation",
  "commit_shas": ["...", "..."],
  "verdict": "same_fix",
  "evidence": ["linked diffs or reproductions"],
  "rationale": "why these commits are or are not one repeated logical fix",
  "reviewer": "GitHub handle",
  "reviewed_at": "UTC RFC 3339 timestamp"
}
```

`verdict` is `same_fix` or `not_same_fix`. The commit list must exactly match
the generated candidate. Missing or disputed adjudication is
`insufficient_data`. Any `same_fix` candidate is a multi-member recurrence and
makes the cohort observation fail.

## Formula and pass threshold

For cohort/control `c` and window `w`, after exact migration exclusions:

```text
touching(c,w) = distinct first-parent commits touching registered members
fixes(c,w)    = touching commits adjudicated as fix
density(c,w)  = |fixes(c,w)| / |touching(c,w)|
delta(c)      = density(c,post) - density(c,pre)
```

Pooled-control numerator and denominator deduplicate commit SHA across control
groups before division. Per-KLOC churn is a sensitivity view only and cannot
change the decision.

The frozen non-inferiority tolerance is one percentage point (`epsilon=0.01`).
A cohort passes only when all of these are true:

1. treated and pooled-control exposure meets both minima in both windows;
2. every touching commit and recurrence candidate has resolved adjudication;
3. no adjudicated multi-member recurrence exists in the post-window;
4. `treated_delta <= control_delta + epsilon`; and
5. when treated pre-density is positive, post-density is strictly lower; when
   pre-density is zero, post fix count is zero instead.

Every other result is `fail` or `insufficient_data`; the report must state which
condition decided it.

## Control invalidation

A control is invalid if, during either measured window, it adopts any treatment
primitive or engine named by that cohort, or is intentionally used as a
treatment target. Record the first contaminating SHA in `invalidated_by`.
Do not select a replacement control after seeing results. Any invalid control,
unreachable anchor, membership change after treatment start, force-pushed
history, unresolved reviewer dispute, or missing migration SHA yields
`insufficient_data`. This affects only the observation result; it does not stop
the corresponding implementation workstream.

## Sequencing policy

The fixed 60-day pre/post windows are deliberately observational. A result of
`pass`, `fail`, or `insufficient_data` never authorizes or blocks a refactor
slice. Work advances when its named code dependencies, focused tests, global
checks, and review requirements are satisfied. Regressions discovered at any
time use the normal issue, regression-test, fix, or rollback workflow.
