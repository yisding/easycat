# Workstream 1: Journal Foundation

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan: tasks, acceptance criteria, and verification for
> completing the journal foundation.
>
> **Predecessors**: none (first workstream).
> **Successors**: Workstream 2 (Agent Bridge) depends on this.
>
> **Sibling workstreams:**
>
> - `workstream-2-agent-bridge.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-4-replay-and-bundle.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may change the public debug/config
> surface if that produces a cleaner runtime, but any such change must be
> frozen in the RFC and covered by migration notes.

## Goal

Replace `EventTraceLogger`, `Tracer`/`Span`/`SpanManager`, and
`InMemoryMetrics` with a single `ExecutionJournal` that captures
structured records of every stage operation, backed by an
`ArtifactStore` for large payloads, with a redaction write filter and
optional crash-durable backend.

## Scope

**In scope:**

- `ExecutionJournal` protocol and two backends (in-memory ring buffer
  default, SQLite for `debug="full"`)
- `ArtifactStore` with `input_ref`/`output_ref` indirection
- Stable record types as frozen dataclasses (`JournalRecord`,
  `FrameworkTransitionRecord`, `TimingInfo`, `ErrorInfo`)
- Read-only public journal access surface (`Session.journal`)
- Redaction as a journal write filter (not post-hoc scrub)
- Monotonic sequence numbers per session
- Synchronous write guarantee
- Crash-durability verified against simulated process death
- Strangler-fig adapters so existing `EventTraceLogger`, `Tracer`, and
  `InMemoryMetrics` write through the journal without breaking tests

**Out of scope** (lands in later workstreams):

- Stage model (Workstream 3)
- Bridge records beyond schema definitions (Workstream 2)
- Replay, `RunBundle` export, SHA-256 manifests (Workstream 4)
- Removal of legacy observability systems (Workstream 5)
- Broader ergonomic API cleanup beyond the core debug surface introduced
  here

## Tasks

### T1.0: Architecture Freeze (RFC)

- [ ] Write Phase 1 implementation RFC covering:
  - concrete journal record classes building on the appendix schema
  - `ArtifactStore` interface
  - backend selection policy (in-memory default, SQLite for
    `debug="full"`)
  - public debug surface frozen for this phase (`Session.journal`,
    `EasyCatConfig.debug` semantics)
  - crash-durability contract (what survives process death mid-turn)
  - strangler-fig wiring plan for the three legacy systems
  - test strategy for incremental migration
- [ ] Review with stakeholders; merge RFC before implementation begins.

### T1.1: Record Types

- [ ] Create `src/easycat/runtime/records.py`
- [ ] Implement `JournalRecord` as a frozen dataclass per the schema in
  `essential-debug-first-runtime.md` appendix
- [ ] Implement `FrameworkTransitionRecord` extending `JournalRecord`
- [ ] Implement `TimingInfo` (`wall_ms`, `cpu_ms`, `queue_ms`)
- [ ] Implement `ErrorInfo` (exception class, message, notes, traceback
  summary, collapsed third-party frames)
- [ ] Expose a `JournalRecordKind` enum to make filtering explicit

### T1.2: Artifact Store

- [ ] Create `src/easycat/runtime/artifacts.py`
- [ ] Implement `ArtifactStore` protocol
- [ ] Implement in-memory artifact store
- [ ] Implement filesystem-backed artifact store for durable journal
  runs (writes into `.easycat/artifacts/<session_id>/`)
- [ ] Every write returns a stable ref string usable as `input_ref`
  or `output_ref`
- [ ] Reads are idempotent; refs are content-addressable where
  practical

### T1.3: Journal Core

- [ ] Create `src/easycat/runtime/journal.py`
- [ ] Define `ExecutionJournal` protocol: `append`, `read`, `slice`,
  `close`, `flush`
- [ ] Define read-only `JournalView` surface used by `Session.journal`
- [ ] Implement monotonic per-session sequence counter
- [ ] Implement synchronous write guarantee — `append` must not return
  until the record is durable in the selected backend

### T1.4: Backends

- [ ] Implement `InMemoryRingBuffer` backend (default for dev)
  - configurable capacity
  - drop-oldest on overflow with a `BufferOverflow` record marker
- [ ] Implement `SqliteJournal` backend
  - WAL mode for concurrent readers during live debug
  - single-writer discipline
  - schema versioning table for forward compatibility
  - `PRAGMA synchronous=NORMAL` + periodic `wal_checkpoint`
- [ ] Backend is selected from `EasyCatConfig.debug` (in-memory for
  `light`, SQLite for `full`)
- [ ] Log a single startup line naming the selected backend

### T1.5: Redaction Write Filter

- [ ] Create `src/easycat/runtime/redaction.py`
- [ ] Implement `RedactionPolicy` dataclass with per-field strategies
  (`redact` | `hash` | `drop` | `retain`) for: transcript text, audio,
  tool args/results, provider payloads, environment metadata
- [ ] Redaction runs **inside** `journal.append` — no raw field value
  ever persists to a backend
- [ ] Artifact store writes go through the same filter

### T1.6: Crash Durability

- [ ] SQLite backend survives `SIGKILL` mid-write with at most one
  in-flight record lost
- [ ] On session open, detect an unclean shutdown marker and emit a
  `RecoveredSession` record at the head of the recovered journal
- [ ] Recovered partial journals are loadable offline (foundation for
  Workstream 4 `bundles list`)
- [ ] In-memory backend documents that it waives crash-durability with
  a single startup log line

### T1.7: Strangler Fig Adapters

- [ ] Wire `EventTraceLogger` event emission through a journal adapter
  so every current log event becomes one or more journal records. No
  legacy code deletion yet — that is Workstream 5.
- [ ] Wire `Tracer`/`Span`/`SpanManager` span lifecycle through the
  journal so spans become paired `start`/`complete` records.
- [ ] Wire `InMemoryMetrics` counters and latency stats to derive from
  journal aggregations (journal as source, metrics as view).
- [ ] Add a feature flag (`EASYCAT_LEGACY_OBS_DUAL_WRITE`, default on)
  so we can compare old and new paths during migration.

### T1.8: Test Migration

- [ ] Existing tests pass unmodified with strangler-fig adapters active
- [ ] Add parity tests comparing legacy output to journal-derived views
  for the same inputs
- [ ] Add migration note showing `EventTraceLogger` subscriber →
  `session.journal` read path
- [ ] Add new journal-specific tests (see Verification)

## Acceptance Criteria

A checked item is a testable condition. All must be true before
Workstream 2 starts.

- [ ] **AC1.1** RFC reviewed and merged.
- [ ] **AC1.2** `src/easycat/runtime/records.py`, `journal.py`,
  `artifacts.py`, `redaction.py` exist and are importable.
- [ ] **AC1.3** `ExecutionJournal` supports both in-memory and SQLite
  backends, selected via `EasyCatConfig.debug`.
- [ ] **AC1.4** Every record has a monotonic `sequence` within its
  session, strictly increasing, with no gaps under single-writer
  discipline.
- [ ] **AC1.5** `append` is synchronous with respect to record
  durability — the record is visible to `read` immediately after
  `append` returns, in both backends.
- [ ] **AC1.6** Large payloads are stored via `input_ref`/`output_ref`
  pointing into `ArtifactStore`; inline record size stays bounded
  regardless of artifact size.
- [ ] **AC1.7** Redaction is applied at write time. The backend is
  searchable after a test run for redacted values and returns zero
  hits.
- [ ] **AC1.8** SQLite backend survives simulated `SIGKILL` mid-write
  and is loadable afterwards with at most one in-flight record lost.
- [ ] **AC1.9** Strangler-fig adapters are in place for
  `EventTraceLogger`, `Tracer`/`SpanManager`, and `InMemoryMetrics`.
  Dual-write is enabled by default.
- [ ] **AC1.10** Full pre-existing test suite passes without
  modification (`uv run pytest` green).
- [ ] **AC1.11** Running `examples/local_chat.py` for one turn produces
  a journal whose records can be iterated and show every existing
  observability event.
- [ ] **AC1.12** `Session.journal` exposes a read-only journal surface
  suitable for migrating off `EventTraceLogger`.
- [ ] **AC1.13** Any public surface changes introduced here (for example
  `EasyCatConfig.debug` semantics) are frozen in the RFC and covered by
  migration notes with before/after examples.

## Verification

Each acceptance criterion maps to a concrete test or procedure.

| AC | Verification |
|---|---|
| AC1.1 | Git log shows the RFC merge commit on the workstream branch. |
| AC1.2 | `python -c "from easycat.runtime import journal, records, artifacts, redaction"` exits 0. |
| AC1.3 | New test `test_journal_backend_selection` — instantiates with `debug="light"` and `debug="full"`, asserts the correct backend class is used. |
| AC1.4 | New test `test_journal_monotonic_sequence` — writes 1,000 records to a single session from a single writer task, asserts strictly increasing with no gaps. |
| AC1.5 | New test `test_journal_synchronous_append_readback` — after every `append`, immediate `read` returns the record. Applies to both backends. |
| AC1.6 | New test `test_artifact_store_indirection` — writes a 1MB synthetic audio blob as an artifact, inspects the journal record and asserts its serialized inline size is < 4KB. |
| AC1.7 | New test `test_redaction_write_filter` — configures a policy marking `transcript_text` as `redact`, writes records with sensitive transcripts, greps the SQLite file and the filesystem artifact store for the sensitive string, asserts zero hits. |
| AC1.8 | New test `test_journal_crash_durability` — subprocess writes records to SQLite, parent sends `SIGKILL`, reopens the journal file, asserts records prior to the last flush are intact and a `RecoveredSession` marker is present. |
| AC1.9 | Grep `src/easycat/event_logging.py`, `tracing.py`, `metrics.py` for journal write calls — each must have one; run with `EASYCAT_LEGACY_OBS_DUAL_WRITE=0` and verify journal-only path produces all previously logged events. |
| AC1.10 | `uv run pytest` exits 0 with no `xfail` or skip additions attributable to this workstream. |
| AC1.11 | Smoke test script runs `examples/local_chat.py` end-to-end for one turn, iterates the resulting journal, asserts records exist for each stage present in the current pipeline. |
| AC1.12 | New test `test_session_exposes_read_only_journal` — obtains `session.journal`, verifies records are readable and append/mutation methods are not exposed. |
| AC1.13 | RFC + migration note include concrete before/after examples for the chosen debug config surface and the new live journal access path. |

## Risks and Mitigations

- **Performance overhead of synchronous writes**: synchronous
  guarantee could throttle the hot path. Mitigation: benchmark on the
  STT partial-transcript path (highest-frequency events) before and
  after. If overhead > 5% on P50 turn latency, introduce a per-stage
  async queue that preserves ordering via sequence numbers while
  relaxing wall-clock synchrony — but the read-after-write invariant
  stays.
- **SQLite lock contention under high turn rate**: WAL mode and
  single-writer discipline should handle it; benchmark at 50 turns/sec
  sustained; fall back to file-per-session if contention shows up.
- **Strangler fig dual-write skews metrics**: keep dual-write behind a
  flag, run parity tests, flip the flag off before Workstream 5
  removes legacy code.
- **Redaction regex gaps**: redaction policy must be declarative and
  field-targeted, not regex sweeps. Tests must cover every field listed
  in the essential plan's Redaction section.

## Handoff to Next Workstream

When this workstream is complete, Workstream 2 (Agent Bridge) inherits:

- a stable journal with working backends
- the record schema, so bridge-emitted records slot in without schema
  changes
- the artifact store, so bridge-captured payloads (tool args, framework
  history snapshots) can be stored by reference
- the redaction filter, so sensitive framework payloads are redacted at
  write time

Workstream 4 (Replay and Bundle) will also depend on this workstream's
crash-durability behavior. Make sure `test_journal_crash_durability`
exercises enough surface area that Workstream 4 can build on it without
retesting the underlying SQLite recovery path.
