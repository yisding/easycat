# Workstream 4: Replay and Bundle Export

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstreams 1, 2, and 3 must all be complete.
> **Successors**: Workstream 5 (Legacy Removal) is gated on this
> workstream demonstrating that journal-based debugging fully replaces
> the legacy systems.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2-agent-bridge.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may add or rename export/replay
> surfaces if needed, but the public bundle/debug API and migration path
> must be frozen in the RFC.

## Goal

Make production failures local repro artifacts with honest replay
semantics. Every major stage boundary is replayable, and the replay
fidelity class is explicit on every `ReplaySpec` so users are never
surprised by non-determinism.

## Scope

**In scope:**

- `ReplaySpec` with explicit `fidelity` field
- Three replay classes: `artifact_replay`, `simulated_replay`,
  `live_reexecution`
- Per-stage `replay()` implementation (hook defined in Workstream 3)
- `RunBundle` dataclass and on-disk format
- SHA-256 manifest for artifact integrity
- Provider version strings captured in bundles
- `session.export_debug_bundle(...)` API
- Secret-safe, allowlisted config/environment metadata in bundles
- Optional redaction pass on export (stricter than runtime default)
- Optional inline artifacts flag
- Bundle schema versioning
- Loading bundles from disk, including partial journals from crashed
  sessions
- Minimal pytest fixture helpers: `load_bundle(path)` returning a
  queryable `RunBundle`
- Committable boundary definition per bridge (refuse replay at
  non-committable boundaries with a clear error pointing at the
  nearest committable checkpoint)

**Out of scope:**

- `forked_replay` (peripheral — in
  `peripheral-eval-and-debugger-ui.md`)
- Full `easycat.testing` module with Simulator + Judge (peripheral)
- `easycat replay` CLI command wrapper (peripheral —
  `peripheral-dx-onboarding.md`)
- `bundle export --for=claude-code` context packs (peripheral)
- Interactive debugger UI replay controls (peripheral)

## Tasks

### T4.0: Architecture Freeze (RFC)

- [ ] Write Phase 4 RFC covering:
  - `ReplaySpec` signature and fidelity enum
  - Committable boundary semantics per bridge (reference
    `ExecutionCursor.committable` from Workstream 2)
  - `RunBundle` dataclass and serialization format (zip with manifest
    JSON, journal NDJSON, artifact directory)
  - SHA-256 manifest schema
  - safe snapshot schema for persisted config/environment metadata
  - Export-time redaction pass vs runtime redaction policy
  - Bundle schema version field and forward-compatibility contract
  - Partial-journal loading for crash recovery
- [ ] Review and merge before implementation.

### T4.1: ReplaySpec and Fidelity Classes

- [ ] Create `src/easycat/runtime/replay.py`
- [ ] Define `ReplayFidelity` enum: `ARTIFACT`, `SIMULATED`, `LIVE`
- [ ] Define `ReplaySpec` dataclass: `fidelity`, `from_sequence`,
  `to_sequence`, `stage_filter`, `overrides`
- [ ] Every `ReplaySpec` must have a fidelity value — no default

### T4.2: Stage Replay Implementations

- [ ] Implement `STTStage.replay()` for `ARTIFACT` — cassette playback
  of captured audio and partial/final transcripts
- [ ] Implement `TTSStage.replay()` for `ARTIFACT` — cassette playback
  of captured audio frames
- [ ] Implement `AgentStage.replay()` for `SIMULATED` — injects
  captured bridge events into the downstream pipeline, bypasses live
  LLM call. Fidelity label on every record: "LLM responses are
  inherently non-deterministic; this replay is best-effort."
- [ ] Implement `replay()` for remaining stages at `ARTIFACT` level
  where possible (`Transport`, `Audio`, `VAD`, `Turn`, `Telephony`)
- [ ] Implement `LIVE` replay for all stages by reusing the current
  `execute()` path with captured inputs

### T4.3: Determinism Guarantees

- [ ] `ARTIFACT` replay of STT must produce byte-identical transcripts
  given the same cassette
- [ ] `ARTIFACT` replay of TTS must produce byte-identical audio
  frames given the same cassette
- [ ] `SIMULATED` replay of agent stage must be deterministic modulo
  the documented LLM non-determinism caveat
- [ ] `LIVE` replay is not expected to be deterministic and is
  labeled so

### T4.4: RunBundle Format

- [ ] Create `src/easycat/debug/bundle.py`
- [ ] Define `RunBundle` dataclass:
  - `format_version: int`
  - `manifest: Manifest` (SHA-256 per artifact, provider version
    strings, safe config snapshot, allowlisted env metadata,
    redaction metadata)
  - `journal_ndjson: bytes`
  - `artifact_index: dict[str, ArtifactEntry]`
  - `replay_entry_points: list[CommittableCheckpoint]`
- [ ] Define on-disk format: `.easycat-bundle` zip with:

  ```
  manifest.json
  journal.ndjson
  artifacts/
    <sha256>.bin
    <sha256>.bin
    ...
  ```

### T4.5: Export API

- [ ] Create `src/easycat/debug/export.py`
- [ ] Implement `Session.export_debug_bundle(path, *, redaction=None, inline_artifacts=False)`:
  - snapshots the current journal
  - applies export-time redaction (if stricter than runtime default)
  - persists only allowlisted config/environment metadata; raw secrets
    never land in the bundle
  - bundles artifacts by reference (default) or inline (if
    `inline_artifacts=True`)
  - computes SHA-256 per artifact
  - captures provider version strings from every provider the
    session touched
  - writes the zip
- [ ] Export is valid even on a partially-complete journal (e.g.,
  from a crashed session opened via the SQLite recovery path in
  Workstream 1)

### T4.6: Provider Version Strings

- [ ] Every provider adapter exposes a `version_info()` method
  returning a stable dict: `{"provider": "deepgram", "api_version":
  "v1", "model": "nova-3", "sdk_version": "..."}`
- [ ] Session collects version info from all active providers at
  export time
- [ ] Bundle manifest includes the full set

### T4.7: Bundle Loading

- [ ] Implement `RunBundle.load(path)` → `RunBundle`
- [ ] Reads manifest, verifies SHA-256 checksums, raises on mismatch
- [ ] Exposes queryable journal records (iterator, filter by stage,
  filter by turn, lookup by sequence)
- [ ] Loads successfully from bundles exported from partial journals
  (crash recovery)

### T4.8: Committable Boundary Enforcement

- [ ] Replay entry points must be `committable` checkpoints per the
  bridge execution cursor from Workstream 2
- [ ] Attempting to start replay at a non-committable sequence
  returns `ReplayError` with fields `requested_sequence`,
  `nearest_committable_before`, `nearest_committable_after`
- [ ] Error message is human-readable and names the stage that was
  mid-operation

### T4.9: Pytest Fixture Helpers

- [ ] Add `easycat.debug.testing.load_bundle(path)` helper for pytest
  users (not the full `easycat.testing` module, which is peripheral)
- [ ] Support bundles from partial journals for regression testing
  around crash scenarios
- [ ] Add one regression test in this workstream that uses the fixture
  to prove the loop closes

### T4.10: Crash Recovery End-to-End

- [ ] `bundles list` functionality — discover bundles in a default
  directory (`.easycat/recordings/` and `.easycat/crash-dumps/`)
- [ ] A crashed session (from the SQLite backend surviving SIGKILL in
  Workstream 1) produces a valid bundle on the next startup, without
  needing a live `Session` object

## Acceptance Criteria

- [ ] **AC4.1** RFC reviewed and merged.
- [ ] **AC4.2** `src/easycat/runtime/replay.py` defines `ReplayFidelity`
  and `ReplaySpec`. Every `ReplaySpec` has a non-default fidelity.
- [ ] **AC4.3** All 8 stages implement `replay(spec)` for at least the
  `LIVE` fidelity class. STT and TTS additionally support `ARTIFACT`.
  Agent stage additionally supports `SIMULATED`.
- [ ] **AC4.4** `ARTIFACT` replay of STT is byte-deterministic given
  the same cassette.
- [ ] **AC4.5** `ARTIFACT` replay of TTS is byte-deterministic given
  the same cassette.
- [ ] **AC4.6** `src/easycat/debug/bundle.py` and
  `src/easycat/debug/export.py` exist and export a valid bundle.
- [ ] **AC4.7** `Session.export_debug_bundle(path)` produces a
  loadable bundle from a running session.
- [ ] **AC4.8** Bundle manifest includes SHA-256 per artifact.
- [ ] **AC4.9** Bundle manifest includes provider version strings for
  every provider touched during the session.
- [ ] **AC4.10** Bundle manifest includes `format_version`.
- [ ] **AC4.11** Export-time redaction and safe snapshot rules ensure the
  bundle contains only allowlisted config/environment metadata and no raw
  secrets, without mutating the original journal.
- [ ] **AC4.12** Bundles load correctly from partial journals produced
  by simulated process death (inherits Workstream 1 infrastructure).
- [ ] **AC4.13** `load_bundle()` verifies SHA-256 checksums and raises
  on mismatch.
- [ ] **AC4.14** Replay at a non-committable sequence returns
  `ReplayError` with nearest committable checkpoint references.
- [ ] **AC4.15** `load_bundle()` pytest helper is usable and covered by
  at least one in-workstream regression test.
- [ ] **AC4.16** `bundles list` discovers crash-dumped bundles on disk.
- [ ] **AC4.17** The public export/load/replay surface is frozen in the
  RFC and covered by migration notes if this workstream changes config or
  debug APIs.

## Verification

| AC | Verification |
|---|---|
| AC4.1 | Git log shows RFC merge commit. |
| AC4.2 | `python -c "from easycat.runtime.replay import ReplayFidelity, ReplaySpec; ReplaySpec()"` fails without explicit fidelity; `ReplaySpec(fidelity=ReplayFidelity.ARTIFACT)` succeeds. |
| AC4.3 | New test `test_all_stages_support_live_replay` — parametrized over 8 stages, calls `replay(ReplaySpec(fidelity=LIVE))` and asserts no `NotImplementedError`. Sub-tests assert STT and TTS support `ARTIFACT` and Agent supports `SIMULATED`. |
| AC4.4 | New test `test_stt_artifact_replay_bit_deterministic` — captures STT cassette from a real session, replays against the same stage instance twice, asserts byte-identical transcript output. |
| AC4.5 | New test `test_tts_artifact_replay_bit_deterministic` — same for TTS. |
| AC4.6 | `python -c "from easycat.debug.bundle import RunBundle; from easycat.debug.export import export_debug_bundle"` exits 0. |
| AC4.7 | New test `test_export_and_load_roundtrip` — runs a one-turn session, exports to a temp file, loads, asserts journal records round-trip. |
| AC4.8 | New test `test_bundle_manifest_sha256` — exports a bundle, parses `manifest.json`, asserts every artifact entry has a `sha256` field that matches the file content hash. |
| AC4.9 | New test `test_bundle_provider_versions` — runs with Deepgram + ElevenLabs, exports, asserts manifest contains version info for both. |
| AC4.10 | Same test as AC4.9 asserts `format_version` is present and > 0. |
| AC4.11 | New test `test_export_redaction_pass` — runs with runtime redaction `retain`, exports with a stricter `redact` policy, loads the bundle, greps for sensitive strings and banned secret-bearing fields (API keys, auth headers, raw env dumps), asserts zero matches. |
| AC4.12 | New test `test_partial_journal_bundle_export` — uses the subprocess-SIGKILL pattern from Workstream 1, reopens the SQLite journal, exports to a bundle, loads, asserts the records prior to the crash are present. |
| AC4.13 | New test `test_bundle_manifest_tamper_detection` — exports a bundle, manually corrupts one artifact byte, attempts to load, asserts `BundleIntegrityError` is raised. |
| AC4.14 | New test `test_replay_refuses_non_committable` — captures a mid-LLM-stream sequence, constructs `ReplaySpec` at that sequence, asserts `ReplayError` with populated `nearest_committable_before`/`after`. |
| AC4.15 | Demonstration regression test `test_bundle_as_fixture` — loads a committed fixture bundle via `load_bundle()`, asserts a journal property. The test itself is the proof that the fixture helper works. |
| AC4.16 | New test `test_bundles_list_discovery` — writes two bundles to a temp directory, calls the discovery function, asserts both are found. |
| AC4.17 | RFC + migration note include the frozen export/load/replay surface and before/after examples for any config or debug-surface changes introduced here. |

## Risks and Mitigations

- **Cassette format drifts across provider version bumps**:
  mitigation — capture provider version strings in the cassette and
  refuse replay when versions don't match, with an explicit override
  flag (`force=True`) that logs a warning and tags the resulting
  replay as `LIVE` rather than `ARTIFACT`.
- **Bundle size explodes with audio artifacts**: mitigation — default
  to reference-based artifact storage; only inline on explicit flag.
  Document that bundles with inline audio can be 10–50MB per turn.
- **SQLite partial-journal recovery fails on some crash patterns**:
  mitigation — WAL mode + periodic checkpoint (already set up in
  Workstream 1), document recoverable vs unrecoverable cases in the
  bundle loader, raise `BundleRecoveryError` with a clear message
  when the journal file is too corrupted to load.
- **SHA-256 checksums slow export**: mitigation — hashes are computed
  once at write time into the artifact store (content-addressable
  naming), so export-time only aggregates pre-computed hashes.
- **`SIMULATED` replay of agent stage confuses users who expect
  determinism**: mitigation — `SIMULATED` fidelity includes a
  docstring and a runtime warning on first use in a process:
  `"SIMULATED replay is non-deterministic for LLM calls. Use ARTIFACT for STT/TTS stages or LIVE for end-to-end reproduction."`
- **Committable boundary checks may reject valid replay points** the
  bridge authors didn't anticipate: mitigation — the
  `ExecutionCursor.committable` flag is the single source of truth;
  if a bridge marks too few boundaries as committable, fix the
  bridge in Workstream 2 rather than bypassing the check.

## Handoff to Next Workstream

When this workstream is complete, Workstream 5 (Legacy Removal)
inherits:

- proof that journal-based debugging fully replaces the three legacy
  observability systems — because bundle export and replay exercise
  the full journal surface
- the pytest fixture helper path, so any tests currently relying on
  `EventTraceLogger` or `InMemoryMetrics` for assertions can migrate
  to journal-based fixtures before the legacy code is deleted
- the crash-recovery code path, which is the last remaining consumer
  of the durable journal backend and the proof that Workstream 1's
  crash-durability contract holds

Peripheral follow-ups also depend on this workstream:

- `peripheral-eval-and-debugger-ui.md` adds `forked_replay` as a
  fourth fidelity class and builds the Simulator + Judge on top of
  the `load_bundle()` fixture helper
- `peripheral-dx-onboarding.md` wraps `export_debug_bundle` and the
  bundle discovery functions as `easycat bundle export` and
  `easycat bundles list` CLI commands
- `peripheral-observability-and-cost.md`'s `CostRecord` is included
  in bundles through the existing journal record path — nothing extra
  to do here
