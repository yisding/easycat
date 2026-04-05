# Workstream 1: Journal Foundation

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan: tasks, acceptance criteria, and verification for
> completing the journal foundation.
>
> **Predecessors**: none (first workstream).
> **Successors**: Workstream 2A (Agent Bridge Protocol and Bridges)
> depends on this. Workstream 2B (Interruption and MCP) depends on
> WS2A. WS2's original single-file form was split into WS2A and WS2B
> to unblock WS3 earlier and let the interruption contract land in
> parallel with WS3 rather than gating it.
>
> **Sibling workstreams:**
>
> - `workstream-2a-agent-bridges.md`
> - `workstream-2b-interruption-and-mcp.md`
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
`ArtifactStore` for large payloads, with a hard-coded Config and
Environment Safety Default and optional crash-durable backend. A
full `RedactionPolicy` write filter lands later in
`peripheral-redaction.md`.

## Scope

**In scope:**

- `ExecutionJournal` protocol and two backends (in-memory ring buffer
  default, SQLite for `debug="full"`)
- `ArtifactStore` with `input_ref`/`output_ref` indirection
- Stable record types as frozen dataclasses (`JournalRecord`,
  `FrameworkTransitionRecord`, `TimingInfo`, `ErrorInfo`)
- Read-only public journal access surface (`Session.journal`)
- Hard-coded Config and Environment Safety Default (essential plan):
  allowlisted config snapshot and env var allowlist, no raw
  `EasyCatConfig.__dict__` or `os.environ` in the journal or artifact
  store
- Monotonic sequence numbers per session
- Synchronous write guarantee
- Crash-durability verified against simulated process death
- Strangler-fig adapters so existing `EventTraceLogger`, `Tracer`, and
  `InMemoryMetrics` write through the journal without breaking tests

**Out of scope** (lands in later workstreams or peripherals):

- Stage model (Workstream 3)
- Bridge records beyond schema definitions (Workstream 2A)
- Replay, `RunBundle` export, SHA-256 manifests (Workstream 4)
- Removal of legacy observability systems (Workstream 5)
- Full `RedactionPolicy` write filter with per-field strategies,
  `SafeConfigSnapshot`/`SafeEnvironmentSnapshot` typed snapshots, and
  export-time redaction pass (peripheral — `peripheral-redaction.md`).
  WS1 ships only the hard-coded safety default; the peripheral work
  layers the full policy onto the same write-filter hook introduced
  here.
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
  - `EasyCatConfig.debug` bool→enum migration plan (current
    `debug: bool = False` becomes
    `debug: Literal["off","light","full"] = "light"`, with
    before/after examples and `debug=True` mapping to `debug="full"`)
  - `.easycat/` journal file-system layout (journals, artifacts,
    crash-dumps, recordings, archive subdirectories)
  - journal retention policy (session count cap, size cap,
    archive vs delete)
  - journal degraded-mode contract (what happens when a write fails)
  - crash-durability contract (what survives process death mid-turn)
  - strangler-fig wiring plan for the three legacy systems
  - test strategy for incremental migration
- [ ] Review with stakeholders; merge RFC before implementation begins.

### T1.0.5: Perf Baseline Capture

- [ ] Build a tiny benchmark harness that runs one turn of
  `examples/local_chat.py` with a stub agent and stub STT that emits
  N partial transcripts per second (target: 50/s sustained for 10s)
- [ ] Measure STT partial-transcript write rate, P50 turn latency,
  and P90 turn latency on the pre-workstream main branch
- [ ] Commit results as `perf/baseline.json` with git SHA, hardware
  notes, and timestamp
- [ ] This baseline is a prerequisite for any AC that references a
  5% regression threshold (AC1.5a, WS3 AC3.15)

### T1.1: Record Types

- [ ] Create `src/easycat/runtime/records.py`
- [ ] Implement `JournalRecord` as a frozen dataclass per the schema in
  `essential-debug-first-runtime.md` appendix
- [ ] Implement `FrameworkTransitionRecord` extending `JournalRecord`
- [ ] Implement `ControlSignalRecord` extending `JournalRecord` with
  the following explicit fields:
  - `signal_kind: Literal["interrupt", "cancel", "pause", "resume",
    "backpressure"]` — frozen enum. These five values are the
    complete set WS3 stages emit; additions require a WS1 RFC
    amendment, not a silent extension.
  - `observed_stage: str` — the stage that handled the signal
    (e.g., `"stt"`, `"tts"`, `"agent"`)
  - `direction: Literal["upstream", "downstream"]` — which way
    the signal is propagating relative to the audio flow
  - `signal_id: str` — a stable identifier unique per originating
    signal within a turn, so downstream records (including WS2's
    `FrameworkCancellationBoundaryReached`) can cite which signal
    caused them
  - `cause: str | None` — human-readable origin (`"barge_in"`,
    `"timeout"`, `"user_cancel"`, `"stt_error"`, etc.)
- [ ] **Composition with WS2 cancellation records.**
  `FrameworkCancellationBoundaryReached` (WS2 T2.2) carries a
  `caused_by_signal_id: str | None` field referencing the
  `ControlSignalRecord.signal_id` that triggered the bridge-side
  cancellation. This makes the voice-side signal flow
  (`ControlSignalRecord`) and the framework-side cancellation flow
  (`FrameworkCancellationBoundaryReached`) explicitly composable:
  the journal always shows which control signal caused which
  framework cancellation boundary, with no inference needed.
  Workstream 3 emits the `ControlSignalRecord` first; Workstream 2B
  bridges read the current signal_id from the runtime context and
  stamp it on the framework record.
- [ ] Implement `RecoveredSessionMarker` extending `JournalRecord`
  with an explicit sequence-number rule: the marker occupies a
  reserved `sequence=0` slot that sits outside the monotonic
  post-open counter. The post-open journal still starts at
  `sequence=1`, so AC1.4's strict monotonicity holds for real
  records while recovery metadata is still addressable.
- [ ] Implement `JournalDegraded` marker record (emitted once per
  session when a backend write fails; see T1.9)
- [ ] Implement `TimingInfo` (`wall_ms`, `cpu_ms`, `queue_ms`)
- [ ] Implement `ErrorInfo` (exception class, message, notes, traceback
  summary, collapsed third-party frames)
- [ ] Expose a `JournalRecordKind` enum to make filtering explicit

### T1.2: Artifact Store

- [ ] Create `src/easycat/runtime/artifacts.py`
- [ ] Implement `ArtifactStore` protocol
- [ ] Implement in-memory artifact store
- [ ] Implement filesystem-backed artifact store at
  `.easycat/artifacts/<session_id>/<sha256>.bin` per T1.2.5 layout
- [ ] Every write returns a stable ref string usable as `input_ref`
  or `output_ref`
- [ ] **Content-addressable by SHA-256** (hard requirement, not
  "where practical"): the ref string is the hex-encoded SHA-256 of
  the artifact payload. Hashing happens once at write time so
  Workstream 4's bundle export can aggregate hashes without
  re-reading every artifact.
- [ ] Reads are idempotent; duplicate writes of the same content
  return the same ref without re-hashing

### T1.2.5: Storage Layout Contract

- [ ] Define and document the full `.easycat/` directory tree used
  across all workstreams. This contract is consumed by Workstream 4
  (`bundles list`, crash recovery) and must be stable.

  ```text
  .easycat/
    journals/
      <session_id>.sqlite       # SQLite backend, one file per session
    artifacts/
      <session_id>/
        <sha256>.bin             # content-addressable artifacts
    crash-dumps/
      <session_id>.sqlite        # journals promoted here on unclean shutdown
    recordings/
      <bundle_name>.easycat-bundle
    archive/
      <session_id>.tar.gz        # retention-archived sessions
  ```

- [ ] Root directory is configurable (`EASYCAT_DATA_DIR` env var,
  defaults to `.easycat/` in CWD)
- [ ] Directories are created lazily on first write
- [ ] Document permissions: files are `0600`, directories are `0700`
  (secret-adjacent data)

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
  - fsync cadence configurable via `EASYCAT_JOURNAL_FSYNC_EVERY`
    (default 1; see AC1.5b)
  - **batched per-turn commits.** The journal accumulates
    records from a single turn in an `asyncio.Queue` and
    flushes them in one transaction at turn boundary. Per-
    record commits blow the latency budget (~1s per turn from
    `fsync` alone); per-turn commits are ~1ms on NVMe. The
    read-after-write guarantee (AC1.5a) still holds: within a
    turn, readers see queued records via the in-memory read
    path; at turn boundary the queue flushes to SQLite
    atomically.
  - **Startup file-open warmup.** The backend opens the SQLite
    file eagerly during session construction so the first
    turn does not pay the ~50ms cold `PRAGMA` roundtrip.
    Documented for serverless adapters (Modal, Cloud Run) that
    run the warmup inside their startup hook.
- [ ] Implement **`LitestreamSqliteJournal`** adapter. Wraps
  `SqliteJournal` and delegates to the `litestream` sidecar (or
  the Litestream Go library embedded via a subprocess) to ship
  WAL segments to S3-compatible object storage. Configured via
  `EASYCAT_JOURNAL_LITESTREAM_REPLICA` pointing at an `s3://`,
  `gs://`, or `file://` URL. Default RPO target: 1 second of
  writes. Restore-on-startup is owned by the deploy platform,
  not the runtime — the adapter documents the
  `litestream restore` incantation in the peripheral deployment
  guide and assumes the file is already present when the
  backend opens it. This is the Tier 1 adapter for Fly
  Machines, EC2/Fargate with a volume, and Railway.
- [ ] Implement **`LibsqlJournal`** adapter. Uses the `libsql`
  Python SDK to open an embedded libSQL replica with a remote
  primary URL (Turso or self-hosted libSQL server). Reads are
  local µs; writes sync asynchronously every
  `EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S` seconds (default 10)
  or on explicit `conn.sync()` calls at turn boundary. This is
  the Tier 1 adapter for Modal and the Tier 2 adapter for Cloud
  Run — any ephemeral-FS host where Litestream's WAL shipping
  would race with container exit. Credentials come from
  `EASYCAT_LIBSQL_URL` and `EASYCAT_LIBSQL_AUTH_TOKEN` and are
  both in the WS1 T1.5 safe-default env var allowlist
  (values are dropped, only presence is recorded).
- [ ] Backend is selected from `EasyCatConfig.debug` and
  `EasyCatConfig.journal_backend`:
  - `debug="off"` → no backend, zero writes.
  - `debug="light"` → in-memory ring buffer (no
    `journal_backend` selection; always in-memory).
  - `debug="full"` → persistent backend selected by
    `journal_backend`:
    - `"sqlite"` (default) → `SqliteJournal` on local disk. For
      Tier 1 VM deployments without Litestream configured.
    - `"sqlite+litestream"` → `LitestreamSqliteJournal`. Tier 1
      default when `EASYCAT_JOURNAL_LITESTREAM_REPLICA` is set.
    - `"libsql"` → `LibsqlJournal`. Default for ephemeral-FS
      hosts (Modal, Cloud Run, etc.).
- [ ] Log a single startup line naming the selected backend
  (including the replica target for `sqlite+litestream` and the
  primary URL host for `libsql` — full URLs are safe-default
  filtered, only the scheme and host appear)

### T1.4.5: Retention Policy

- [ ] SQLite backend honors a retention policy: keep the most
  recent N sessions (default 50) **or** M total bytes
  (default 2 GB), whichever is tighter
- [ ] On session close, retention runs: older sessions beyond the
  cap are either archived to `.easycat/archive/<session_id>.tar.gz`
  or deleted based on `EasyCatConfig.journal_retention` (default
  `"archive"`; alternative: `"delete"`)
- [ ] In-memory ring buffer retention is governed by its capacity
  bound; no separate retention task
- [ ] Document that retention runs opportunistically and never
  blocks a turn

### T1.5: Config and Environment Safety Default

- [ ] Create `src/easycat/runtime/safe_defaults.py`
- [ ] Implement a hard-coded allowlist of `EasyCatConfig` fields safe
  to serialize. The full allowlist for Phase 1 is:

  ```python
  SAFE_CONFIG_FIELDS: frozenset[str] = frozenset({
      # Provider role identifiers (the *kind* of provider, not
      # credentials)
      "stt_provider",
      "tts_provider",
      "transport_provider",
      "telephony_provider",
      "noise_reducer_provider",
      "vad_provider",

      # Model identities
      "stt_model",
      "tts_model",
      "tts_voice",

      # Runtime mode and turn policy
      "runtime_mode",          # "chained_pipeline" | "text_session"
      "turn_mode",             # "vad" | "push_to_talk"
      "debug",                 # "off" | "light" | "full"

      # Timeouts and thresholds (all numeric, no secrets)
      "agent_timeout_seconds",
      "stt_timeout_seconds",
      "tts_timeout_seconds",
      "min_speech_duration_ms",
      "silence_duration_ms",
      "interruption_threshold",

      # Feature toggles (booleans)
      "smart_turn_enabled",
      "backchannel_filter_enabled",
      "echo_cancellation_enabled",

      # Journal config (safe to report)
      "journal_retention",
      "journal_backend",
  })
  ```

  Every other `EasyCatConfig` field — including anything whose name
  contains `key`, `secret`, `token`, `password`, `credential`, or
  `auth` — is dropped. New fields added to `EasyCatConfig` are
  dropped by default; adding one to the allowlist requires an
  explicit RFC note justifying that it carries no secret material.
- [ ] Implement a hard-coded allowlist of environment variables safe
  to serialize. The Phase 1 allowlist is:

  ```python
  SAFE_ENV_VARS: frozenset[str] = frozenset({
      # EasyCat runtime control
      "EASYCAT_DATA_DIR",
      "EASYCAT_JOURNAL_FSYNC_EVERY",
      "EASYCAT_LEGACY_OBS_DUAL_WRITE",
      # Standard runtime identification (non-secret)
      "PYTHONVERSION",  # captured as sys.version, not os.environ
      # Deployment identification (non-secret, useful for bundles)
      "HOSTNAME",
      "REGION",
      "DEPLOY_ENV",
  })
  ```

  Every other environment variable is dropped — including anything
  matching `*_KEY`, `*_SECRET`, `*_TOKEN`, `*_PASSWORD`,
  `*_CREDENTIAL`, `AWS_*`, `OPENAI_*`, `DEEPGRAM_*`, `ELEVENLABS_*`,
  `ANTHROPIC_*`, etc. A lint rule enforces that new `EASYCAT_*`
  variables which handle secrets are excluded from this list by
  default.
- [ ] The journal and artifact store call this helper instead of
  inlining `EasyCatConfig.__dict__` or `os.environ` directly. Both
  raw structures are forbidden from reaching any backend.
- [ ] Expose a single extension point (`apply_write_filter(record)`)
  that is a no-op in this workstream but is the hook
  `peripheral-redaction.md` plugs into later to layer a full
  `RedactionPolicy` on top. Bridges (WS2) and stages (WS3) route
  framework/stage snapshots through this same helper — a full
  `RedactionPolicy` thus covers them automatically once the
  peripheral lands, without changing any bridge or stage code.
- [ ] Stamp a **dev-only banner** on every bundle exported by WS4
  ("Contains raw transcripts, tool args, and provider payloads.
  Safe to share with your own team in dev; do not upload to
  third-party services or attach to public issues until redaction
  policy is configured."). WS4 reads this banner from
  `safe_defaults.py` so the peripheral redaction work can upgrade
  it by swapping the banner text, not by editing WS4.

### T1.6: Crash Durability

- [ ] SQLite backend survives `SIGKILL` mid-write with at most one
  in-flight record lost (bounded by `EASYCAT_JOURNAL_FSYNC_EVERY`)
- [ ] On session open, detect an unclean shutdown marker and emit a
  `RecoveredSessionMarker` record (defined in T1.1) in the reserved
  `sequence=0` slot of the recovered journal. The post-open
  monotonic counter still starts at `sequence=1`.
- [ ] Recovered partial journals are loadable offline (foundation for
  Workstream 4 `bundles list`). On recovery, the SQLite file is
  moved from `.easycat/journals/` to `.easycat/crash-dumps/` per
  the T1.2.5 layout.
- [ ] In-memory backend documents that it waives crash-durability with
  a single startup log line
- [ ] Document the filesystem assumption: crash durability relies on
  ext4/xfs fsync semantics. tmpfs-backed test environments waive
  durability; tests that rely on T1.6 behavior must skip on tmpfs
  with a clear marker.

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

### T1.7.5: Provider `version_info()` Retrofit

- [ ] Every provider subclass in `src/easycat/stt/`, `src/easycat/tts/`,
  `src/easycat/transports/`, `src/easycat/telephony/` grows a
  `version_info() -> dict[str, str]` method returning a stable dict
  with keys: `provider`, `model` (if applicable), `api_version` (if
  applicable), `sdk_version`. Unknown fields are `"unknown"` rather
  than omitted, so shape is stable.
- [ ] Factory helpers in `stt/factory.py` and `tts/factory.py`
  propagate version info into the journal at session start as a
  `ProviderVersions` record
- [ ] This work lands in Workstream 1 because the edits touch every
  provider file — concentrating them here avoids a last-workstream
  retrofit across the whole provider layer. Workstream 4 only
  *aggregates* this info into the bundle manifest.

### T1.8: Test Migration

- [ ] Existing tests pass unmodified with strangler-fig adapters active
- [ ] Add parity tests comparing legacy output to journal-derived views
  for the same inputs
- [ ] Add migration note showing `EventTraceLogger` subscriber →
  `session.journal` read path
- [ ] Add new journal-specific tests (see Verification)

### T1.8.5: Strangler-Fig Parity Harness

- [ ] For each of the three legacy systems (`EventTraceLogger`,
  `Tracer`, `InMemoryMetrics`), implement a parity test that:
  - runs the same session scenario twice: once with
    `EASYCAT_LEGACY_OBS_DUAL_WRITE=1` reading legacy output, once
    reading journal-derived views
  - diffs every event/span/metric produced on both sides
  - asserts zero diff for every event type currently covered by the
    test suite
- [ ] Parity tests run on every CI build and must pass before WS5's
  legacy flip
- [ ] Record any legitimate divergences (e.g., timestamps) in an
  explicit allowlist; the allowlist itself is reviewed in the WS5
  RFC before legacy deletion

### T1.9: Journal Degraded-Mode Contract

- [ ] When a backend write fails (disk full, lock contention, SQLite
  corruption, safe-default helper crash), the journal:
  - emits a single `JournalDegraded` marker record to stderr (not
    the backend — the backend just failed)
  - sets a session-level degraded flag
  - returns from `append` without raising
- [ ] Subsequent writes in the same session become best-effort:
  attempts continue but failures are silently dropped beyond the
  first marker. The degraded flag surfaces on `JournalView`.
- [ ] **Voice turns never block on journal writes**, even in
  degraded mode. This is the invariant that justifies the
  synchronous-write guarantee: correctness without a liveness
  hazard.
- [ ] Recovery: a new session starts clean. A degraded session's
  partial data is still exportable as a crash-dump bundle via
  Workstream 4's partial-journal loader.

## Acceptance Criteria

A checked item is a testable condition. All must be true before
Workstream 2A starts.

- [ ] **AC1.1** RFC reviewed and merged.
- [ ] **AC1.2** `src/easycat/runtime/records.py`, `journal.py`,
  `artifacts.py`, `safe_defaults.py` exist and are importable.
- [ ] **AC1.3** `ExecutionJournal` supports in-memory and SQLite
  backends selected via
  `EasyCatConfig.debug ∈ {"off","light","full"}`. `"off"` disables
  the journal entirely (no backend, zero writes); `"light"` selects
  the in-memory ring buffer; `"full"` selects SQLite. Migration
  note: the current `debug: bool = False` becomes
  `debug: Literal["off","light","full"] = "light"`; callers passing
  `debug=True` get `debug="full"` behavior via a one-release
  compatibility shim plus `DeprecationWarning`.
- [ ] **AC1.4** Every post-open record has a monotonic `sequence`
  within its session, strictly increasing from `1`, with no gaps
  under single-writer discipline. The reserved `sequence=0` slot
  is used only for session-open metadata
  (`RecoveredSessionMarker`) and is exempt from the strict-
  monotonic rule.
- [ ] **AC1.5a** (read-after-write) `append` returns only after the
  record is visible to `read` on the same session, in both backends.
- [ ] **AC1.5b** (crash durability) SQLite backend fsyncs within
  `EASYCAT_JOURNAL_FSYNC_EVERY` records (default 1). The risks-
  section async-queue relaxation, if ever needed, applies only to
  AC1.5a visibility latency under load — AC1.5b is non-negotiable
  and independent of any async buffering.
- [ ] **AC1.6** Large payloads are stored via `input_ref`/`output_ref`
  pointing into `ArtifactStore`; inline record size stays bounded
  regardless of artifact size.
- [ ] **AC1.7** Config and Environment Safety Default is enforced.
  A test constructs a session with an `EasyCatConfig` containing a
  synthetic API key and exports the journal: the raw key must not
  appear in any record or artifact. A second test asserts that
  `os.environ`-style dumps are not serialized by any journal write
  path; only the `EASYCAT_*` allowlist is present in the safe
  environment snapshot. Full per-field `RedactionPolicy` coverage is
  out of scope here and lives in `peripheral-redaction.md`.
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
- [ ] **AC1.14** Journal write failures degrade gracefully per T1.9.
  Simulated disk-full or filter-crash scenarios produce a single
  stderr `JournalDegraded` marker, set the session degraded flag,
  and do not raise in-turn. Turn latency is not measurably affected
  by subsequent silent drops.
- [ ] **AC1.15** Every provider subclass in `src/easycat/stt`, `tts`,
  `transports`, `telephony` has a working `version_info()` method
  returning the stable-shape dict. A CI guard test asserts
  completeness via reflection over provider registries.
- [ ] **AC1.16** Strangler-fig parity (T1.8.5): dual-write parity
  tests pass zero-diff for every event type in the pre-workstream
  test suite, modulo a small explicit timestamp allowlist.
- [ ] **AC1.17** Journal backend adapters. Three sub-tests:
  - `test_sqlite_journal_batched_per_turn_commit` — writes 100
    records during a single turn, measures `fsync` count via
    `strace` (or equivalent), asserts one `fsync` per turn
    boundary (not per record).
  - `test_litestream_sqlite_adapter_round_trip` — runs a
    session against a local file-backed Litestream replica
    (`file://./test-replica/`), kills the process, restores
    the journal via `litestream restore`, asserts every record
    prior to the last WAL segment is recovered. Gated on the
    Litestream binary being on `$PATH`; skipped with a log
    line otherwise.
  - `test_libsql_adapter_round_trip` — runs a session against
    a local libSQL server (started by the test fixture via
    `sqld --http-listen-addr`), asserts records sync to the
    remote primary and are readable from a fresh embedded
    replica. Gated on `sqld` being available.
- [ ] **AC1.18** Safe-default env var filtering covers the new
  Litestream and libSQL credentials. A test sets
  `EASYCAT_JOURNAL_LITESTREAM_REPLICA=s3://bucket/path`,
  `AWS_SECRET_ACCESS_KEY=synthetic`,
  `EASYCAT_LIBSQL_URL=libsql://org.turso.io`, and
  `EASYCAT_LIBSQL_AUTH_TOKEN=synthetic`; runs a turn; asserts
  the synthetic values do not appear in any journal record or
  artifact. The presence of the replica target may be recorded
  (scheme + host only).

## Verification

Each acceptance criterion maps to a concrete test or procedure.

| AC | Verification |
|---|---|
| AC1.1 | Git log shows the RFC merge commit on the workstream branch. |
| AC1.2 | `python -c "from easycat.runtime import journal, records, artifacts, safe_defaults"` exits 0. |
| AC1.3 | New test `test_journal_backend_selection` — instantiates with `debug="off"`, `debug="light"`, and `debug="full"`, asserts the correct backend class (or `None` for `"off"`) is used. Separate test `test_debug_bool_compat_shim` asserts `debug=True` emits `DeprecationWarning` and routes to `"full"`. |
| AC1.4 | New test `test_journal_monotonic_sequence` — writes 1,000 records to a single session from a single writer task, asserts strictly increasing from 1 with no gaps; asserts `sequence=0` is reserved and only populated by `RecoveredSessionMarker`. |
| AC1.5a | New test `test_journal_synchronous_append_readback` — after every `append`, immediate `read` returns the record. Applies to both backends. |
| AC1.5b | New test `test_journal_fsync_cadence` — writes N records to SQLite, asserts the WAL file has been fsynced at least `N / EASYCAT_JOURNAL_FSYNC_EVERY` times via `strace` or `fdatasync` counter. |
| AC1.6 | New test `test_artifact_store_indirection` — writes a 1MB synthetic audio blob as an artifact, inspects the journal record and asserts its serialized inline size is < 4KB. Second assertion: two writes of identical bytes return the same SHA-256 ref. |
| AC1.7 | Two new tests. `test_safe_config_default_drops_api_keys` — constructs a config with a synthetic API key, runs one turn, greps the SQLite file and artifact directory for the key, asserts zero hits. `test_safe_env_default_drops_non_easycat_vars` — sets a sensitive env var outside the `EASYCAT_*` allowlist, runs one turn, asserts it does not appear in any journal record or artifact. |
| AC1.8 | New test `test_journal_crash_durability` — subprocess writes records to SQLite, parent sends `SIGKILL`, reopens the journal file, asserts records prior to the last flush are intact and a `RecoveredSessionMarker` in `sequence=0` is present; asserts the file was moved to `.easycat/crash-dumps/`. Skipped on tmpfs. |
| AC1.9 | Grep `src/easycat/event_logging.py`, `tracing.py`, `metrics.py` for journal write calls — each must have one; run with `EASYCAT_LEGACY_OBS_DUAL_WRITE=0` and verify journal-only path produces all previously logged events. |
| AC1.10 | `uv run pytest` exits 0 with no `xfail` or skip additions attributable to this workstream. |
| AC1.11 | Smoke test script runs `examples/local_chat.py` end-to-end for one turn, iterates the resulting journal, asserts records exist for each stage present in the current pipeline. |
| AC1.12 | New test `test_session_exposes_read_only_journal` — obtains `session.journal`, verifies records are readable and append/mutation methods are not exposed. |
| AC1.13 | RFC + migration note include concrete before/after examples for the chosen debug config surface and the new live journal access path. |
| AC1.14 | New test `test_journal_degraded_mode` — patches the backend to raise on `append`, runs a turn, asserts exactly one `JournalDegraded` marker on stderr, session degraded flag set, turn completes without raising, subsequent appends silently drop. |
| AC1.15 | New test `test_all_providers_expose_version_info` — uses the STT/TTS/transport/telephony factory registries to instantiate each provider with stub credentials and asserts `version_info()` returns a dict with the stable key set. |
| AC1.16 | CI job `parity-strangler-fig` runs the T1.8.5 harness on every PR and blocks merge on any diff outside the timestamp allowlist. |
| AC1.17 | Three new tests: `test_sqlite_journal_batched_per_turn_commit` (asserts one `fsync` per turn via `strace`/`fdatasync` counter), `test_litestream_sqlite_adapter_round_trip` (gated on Litestream binary), `test_libsql_adapter_round_trip` (gated on `sqld`). Missing binaries skip with a log line. |
| AC1.18 | New test `test_journal_adapter_credentials_redacted` — sets synthetic credentials for Litestream and libSQL, runs a turn, greps SQLite and artifact dir for the synthetic values, asserts zero hits. |

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
- **Safe default allowlist gaps**: the hard-coded config/env allowlist
  must stay narrow enough that a new `EasyCatConfig` field does not
  accidentally leak. Mitigation — a lint rule forbids adding a new
  config field to the allowlist without explicit justification in the
  RFC, and the default when a new field is introduced is "dropped".
  A richer per-field `RedactionPolicy` lands in
  `peripheral-redaction.md`; WS1's guardrail is intentionally
  minimal.

## Handoff to Next Workstream

When this workstream is complete, Workstream 2A (Agent Bridge
Protocol and Bridges) inherits:

- a stable journal with working backends
- the record schema, so bridge-emitted records slot in without schema
  changes
- the artifact store, so bridge-captured payloads (tool args, framework
  history snapshots) can be stored by reference
- the `apply_write_filter(record)` extension point and hard-coded safe
  defaults, so framework snapshots automatically route through the
  same hook the peripheral `RedactionPolicy` will later extend

Workstream 4 (Replay and Bundle) will also depend on this workstream's
crash-durability behavior. Make sure `test_journal_crash_durability`
exercises enough surface area that Workstream 4 can build on it without
retesting the underlying SQLite recovery path.
