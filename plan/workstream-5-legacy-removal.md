# Workstream 5: Legacy Removal

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstreams 1, 2, 3, and 4 must all be complete.
> **Successors**: None. This workstream is the final deliverable of the
> essential plan.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2-agent-bridge.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-4-replay-and-bundle.md`

> **Compatibility policy**: Backwards compatibility is explicitly not a
> goal here. This workstream intentionally removes and renames public
> symbols; migration completeness is the gate, not API preservation.

## Goal

Delete the three pre-existing observability systems and the
compatibility shims introduced for the strangler-fig migration, leaving
a codebase with one debugging model: the journal.

## Scope

**In scope:**

- Remove `EventTraceLogger` and `src/easycat/event_logging.py`
- Remove custom `Tracer`, `Span`, `SpanManager`, and
  `src/easycat/tracing.py`, `src/easycat/_span_manager.py`
- Remove `InMemoryMetrics` and `src/easycat/metrics.py`
- Remove `src/easycat/agent_runner.py` (absorbed into stages and
  bridge)
- Remove strangler-fig adapters from Workstream 1
- Collapse the `EASYCAT_LEGACY_OBS_DUAL_WRITE` feature flag and its
  dual-write code paths
- Remove any duplicated state handling paths left on Session after
  Workstream 3
- Remove or rename obsolete top-level exports and config fields that only
  exist to preserve the pre-redesign surface
- Update imports and references across the codebase and tests
- Publish migration guide for external consumers of removed or renamed
  APIs/config fields

**Out of scope:**

- Any behavior change beyond deletion
- Any new features (nothing new ships in this workstream)
- Renaming or moving the replacement types — they are already in
  their final locations from Workstreams 1–4

## Tasks

### T5.0: Architecture Freeze (RFC)

- [ ] Write Phase 5 RFC covering:
  - deprecation timeline (one prior release with
    `DeprecationWarning` before deletion)
  - removal order (safest first: strangler-fig shims → legacy
    modules → feature flag)
  - external migration paths (for anyone consuming
    `EventTraceLogger`, `Tracer`, `InMemoryMetrics`, `AgentRunner`,
    adapter helpers, top-level imports, or legacy config fields from
    user code)
  - rollback plan if a post-removal regression surfaces
- [ ] Review and merge before any deletions.

### T5.1: Deprecation Release (Prior to Deletion)

- [ ] Add `DeprecationWarning` to every public symbol in
  `event_logging.py`, `tracing.py`, `metrics.py`, `_span_manager.py`,
  `agent_runner.py`
- [ ] Add `DeprecationWarning` or release-note coverage for top-level
  re-exports and `EasyCatConfig` fields slated for removal or rename
- [ ] Warning message names the replacement
  (`session.journal`, `ExecutionJournal`, `Stage`,
  `ExternalAgentBridge`, new debug/runtime config fields, etc.)
- [ ] Publish a release containing only these deprecation warnings
  so external users see them before deletion
- [ ] Ship migration guide in the same release

### T5.2: Remove Strangler-Fig Adapters

- [ ] Flip `EASYCAT_LEGACY_OBS_DUAL_WRITE` default to `0`
- [ ] Run full test suite with flag off; fix any regressions
- [ ] Delete the dual-write code paths entirely
- [ ] Remove the feature flag

### T5.3: Remove EventTraceLogger

- [ ] Delete `src/easycat/event_logging.py`
- [ ] Delete `tests/test_event_logging.py` and related tests that
  test implementation rather than behavior
- [ ] Update any remaining imports across `src/easycat/` and
  `tests/` to use the journal formatter instead
- [ ] Verify no references remain:
  `grep -rn 'EventTraceLogger\|event_logging' src/ tests/` returns
  zero

### T5.4: Remove Tracer / Span / SpanManager

- [ ] Delete `src/easycat/tracing.py`
- [ ] Delete `src/easycat/_span_manager.py`
- [ ] Delete related test files that test implementation
- [ ] Update imports to use journal-derived tracing (peripheral
  OTel exporter in `peripheral-observability-and-cost.md`
  eventually replaces the user-facing OTel projection, but that is
  not part of this workstream)
- [ ] Verify no references remain:
  `grep -rn 'Tracer\|SpanManager' src/ tests/` returns zero (modulo
  occurrences inside comments/docstrings explicitly describing the
  removal)

### T5.5: Remove InMemoryMetrics

- [ ] Delete `src/easycat/metrics.py`
- [ ] Delete related test files
- [ ] Metrics consumers migrate to journal-derived aggregations
- [ ] Verify no references remain:
  `grep -rn 'InMemoryMetrics\|metrics\.' src/ tests/` returns zero
  legacy matches (note: journal-derived metrics may still use the
  word `metrics` — review carefully)

### T5.6: Remove agent_runner.py

- [ ] Delete `src/easycat/agent_runner.py` (465 lines)
- [ ] Any remaining utility functions migrate into
  `src/easycat/stages/agent.py` or
  `src/easycat/integrations/agents/base.py`
- [ ] Verify no references remain:
  `grep -rn 'agent_runner\|AgentRunner' src/ tests/` returns zero

### T5.7: Collapse Duplicate Session State Paths

- [ ] Audit `src/easycat/session/_session.py` for any instance
  variables or methods that were kept only as temporary migration
  shims during Workstream 3
- [ ] Remove them; verify tests still pass
- [ ] Final Session line count target: unchanged from Workstream 3's
  target/ceiling, minus any shims we remove here

### T5.8: Documentation Updates

- [ ] Update `CLAUDE.md` to reference the journal model (remove
  mentions of `EventTraceLogger`, `Tracer`, `InMemoryMetrics`)
- [ ] Update README sections touching observability
- [ ] Update any docstrings in remaining modules that reference
  removed types
- [ ] Verify `uv run ruff check .` is clean
- [ ] Verify `uv run ruff format .` produces no diffs

### T5.9: Migration Guide for External Consumers

- [ ] Write `docs/migration-debug-first-runtime.md` (or similar
  location) with before/after snippets:
  - `EventTraceLogger` subscriber → `session.journal`
  - `Tracer.span(...)` context manager → journal stage operations
  - `InMemoryMetrics.record(...)` → derived query over journal
  - `AgentRunner` / `wrap_agent=True` → bridge/stage-native session setup
  - legacy `debug: bool` / legacy config fields → new debug/runtime
    settings
  - removed top-level imports → replacement module paths or public
    surfaces
- [ ] Link the guide from the main README and from any release notes

### T5.10: Final Cleanup

- [ ] Run full test suite: `uv run pytest`
- [ ] Run linter: `uv run ruff check .`
- [ ] Run formatter: `uv run ruff format .`
- [ ] Run type checker if configured
- [ ] Run `examples/local_chat.py` and `examples/ws_server.py` as
  smoke tests
- [ ] Verify line count of `src/easycat/` has measurably decreased

## Acceptance Criteria

- [ ] **AC5.1** RFC reviewed and merged.
- [ ] **AC5.2** A prior release exists with `DeprecationWarning` or
  explicit release-note coverage on every removed public symbol and
  removed/renamed config field (evidenced by git tag and changelog).
- [ ] **AC5.3** `EASYCAT_LEGACY_OBS_DUAL_WRITE` feature flag no
  longer exists in the codebase.
- [ ] **AC5.4** The following files no longer exist:
  - `src/easycat/event_logging.py`
  - `src/easycat/tracing.py`
  - `src/easycat/_span_manager.py`
  - `src/easycat/metrics.py`
  - `src/easycat/agent_runner.py`
- [ ] **AC5.5** Zero grep hits for the removed class names in
  `src/easycat/` and `tests/` (comments and docstrings explicitly
  describing the removal are allowed but must be rare and justified).
- [ ] **AC5.6** Full test suite passes: `uv run pytest` exits 0.
- [ ] **AC5.7** `uv run ruff check .` exits 0.
- [ ] **AC5.8** `uv run ruff format .` produces no diff.
- [ ] **AC5.9** `examples/local_chat.py` and `examples/ws_server.py`
  run end-to-end without errors.
- [ ] **AC5.10** Line count of `src/easycat/` is materially lower than
  the pre-workstream baseline, with at least 1,000 lines removed as a
  target rather than a gate (baseline: `event_logging.py` 269 +
  `tracing.py` 232 + `metrics.py` 184 + `_span_manager.py` 97 +
  `agent_runner.py` 465 = 1,247 lines, minus any replacements added).
- [ ] **AC5.11** Migration guide exists and is linked from README.
- [ ] **AC5.12** `CLAUDE.md` no longer references removed types.
- [ ] **AC5.13** Supported runtime behavior has no unexpected
  regressions, and the migration guide covers the intended public API
  removals/renames.
- [ ] **AC5.14** `easycat.__all__`, remaining top-level imports, and
  `EasyCatConfig` no longer expose legacy exports/fields that this
  redesign intentionally removed or renamed.

## Verification

| AC | Verification |
|---|---|
| AC5.1 | Git log shows RFC merge commit. |
| AC5.2 | Release tag on a prior commit with `DeprecationWarning` imports present and/or release notes explicitly covering removed symbols and config fields; changelog entry names the deprecations. |
| AC5.3 | `grep -rn 'EASYCAT_LEGACY_OBS_DUAL_WRITE\|legacy_obs_dual_write' src/ tests/` returns zero. |
| AC5.4 | `test -f src/easycat/event_logging.py` and the other four files all return non-zero (files absent). CI test `test_legacy_modules_removed` asserts these paths do not exist. |
| AC5.5 | CI test `test_no_legacy_observability_symbols` runs `grep -rn 'EventTraceLogger\|InMemoryMetrics\|SpanManager\|AgentRunner' src/easycat/ tests/` and asserts zero matches outside an allow-list of comment-only lines. |
| AC5.6 | `uv run pytest` exits 0 in CI. |
| AC5.7 | `uv run ruff check .` exits 0 in CI. |
| AC5.8 | `uv run ruff format --check .` exits 0 in CI. |
| AC5.9 | CI smoke job runs both example scripts for one turn each; both exit 0. |
| AC5.10 | CI lint computes `find src/easycat -name '*.py' | xargs wc -l`, reports the delta against the pre-workstream baseline, and verifies the codebase is materially smaller. |
| AC5.11 | Check that `docs/migration-debug-first-runtime.md` exists and that README links to it. |
| AC5.12 | Grep `CLAUDE.md` for removed symbol names; must return zero. |
| AC5.13 | Behavioral regression suite passes for supported runtime behavior, and the migration guide covers the intended public removals/renames with concrete before/after examples. |
| AC5.14 | CI test `test_public_surface_cleanup` imports `easycat`, inspects `easycat.__all__` and `EasyCatConfig`, and asserts removed legacy exports/config fields are gone or intentionally renamed per the migration guide. |

## Risks and Mitigations

- **External users depend on removed APIs**: mitigation — the prior
  deprecation release (AC5.2) gives them one version to migrate.
  Migration guide (AC5.11) names the replacements. For very common
  use cases (`EventTraceLogger` subscription is the most likely),
  include a ready-to-copy snippet using `session.journal`.
- **Tests were testing implementation rather than behavior**:
  mitigation — during T5.3/T5.4/T5.5, audit each deleted test and
  either rewrite as a behavior test against the journal or delete it
  if the behavior is covered elsewhere. Do not leave behavior
  coverage gaps.
- **Ruff or mypy catches cascading issues**: mitigation — these are
  wins, not blockers. Fix them and proceed. If the fix grows beyond
  trivial, open a pre-removal cleanup task and complete it before
  continuing.
- **Line-count target is not met because Workstreams 1–4 added more
  lines than they replaced**: mitigation — this is a soft target. The
  hard gate is "the five files are deleted and tests pass." The
  1,000-line reduction is a sanity check, not a hard budget. If the
  workstream lands the files but the net line count is lower than
  baseline by only 500 lines, that is still a successful workstream.
- **Post-removal regression is discovered after deletion**:
  mitigation — the rollback plan in the RFC names which commits to
  revert. Because the strangler-fig shims are removed before the
  legacy modules, a revert of the deletion commits restores the
  shims too. Keep each deletion in its own commit to make bisect and
  revert cheap.

## Handoff

This is the final workstream of the essential debug-first runtime
redesign. When it completes:

- The essential plan in `essential-debug-first-runtime.md` is fully
  delivered.
- Peripheral follow-ups in the four `peripheral-*.md` files can
  proceed with a clean journal-only model to build on.
- External consumers have a migration guide for the removed and renamed
  public surface.
- The codebase is measurably smaller, with a single debugging model.

No further essential workstreams follow. Any additional work is
either a peripheral follow-up (new file) or a new initiative that
starts with its own essential plan.

## Completeness Gate

Workstream 5 is done when:

1. All 14 acceptance criteria above are ticked.
2. The five listed files are gone.
3. CI is green.
4. A reviewer has walked through the CLAUDE.md architecture section
   and confirmed nothing references the removed types.
5. An external consumer (even a synthetic one) has successfully
   followed the migration guide from the deprecation release to the
   current release.

When the gate closes, close the essential-plan milestone and announce
that EasyCat has one debugging model.
