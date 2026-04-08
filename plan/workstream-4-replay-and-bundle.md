# Workstream 4: Replay and Bundle Export

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstreams 1, 2A, 2B, 2C, and 3 must all be
> complete. WS2C (Remote Agent Bridge) is required because this
> workstream implements SIMULATED and LIVE replay for
> `ResponsesAPIBridge` — without it, the remote bridge replay paths
> cannot be built or tested.
> **Successors**: Workstream 5 (Legacy Removal) is gated on this
> workstream demonstrating that journal-based debugging fully replaces
> the legacy systems.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2a-agent-bridges.md`
> - `workstream-2b-interruption-and-mcp.md`
> - `workstream-2c-remote-bridge.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may add or rename export/replay
> surfaces if needed, but the public bundle/debug API and migration path
> must be documented in the plan.

## Goal

Make production failures local repro artifacts with honest replay
semantics. Every major stage boundary is replayable, and the replay
fidelity class is explicit on every `ReplaySpec` so users are never
surprised by non-determinism.

## Scope

**In scope:**

- `ReplaySpec` with explicit `fidelity` field
- explicit replay tool-safety policy (`deny` / `stub` / `allow`)
- Three replay classes: `artifact_replay`, `simulated_replay`,
  `live_reexecution`
- Per-stage `replay()` implementation (hook defined in Workstream 3)
- `RunBundle` dataclass and on-disk format
- SHA-256 manifest for artifact integrity
- Provider version strings captured in bundles
- `session.export_debug_bundle(...)` API
- Secret-safe, allowlisted config/environment metadata in bundles
- Dev-only banner stamped on every exported bundle (the banner text
  is owned by WS1 `safe_defaults.py` and upgraded by
  `peripheral-redaction.md` later)
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

### T4.0: Architecture Freeze

- [x] Design decisions covering:
  - `ReplaySpec` signature and fidelity enum
  - Committable boundary semantics per bridge — consumed from WS2
    T2.7.5's `COMMITTABLE_BOUNDARIES` mappings by reference, not
    re-declared here
  - `RunBundle` dataclass and serialization format (zip with manifest
    JSON, journal NDJSON, artifact directory)
  - SHA-256 manifest schema (hashes pre-computed by WS1 T1.2
    content-addressable artifacts — no rehashing on export)
  - Safe snapshot schema for persisted config/environment metadata
    (consumed from WS1 `safe_defaults.py`)
  - Dev-only banner text read from WS1 `safe_defaults.py` and the
    hook `peripheral-redaction.md` uses to upgrade the banner and
    add an export-time second pass
  - Bundle schema version field and forward-compatibility contract
  - Forward-compatibility handling: what happens when loading a
    bundle whose `format_version` is newer than the loader
    (explicit rejection with a version-mismatch error, not silent
    downgrade)
  - Partial-journal loading for crash recovery via
    `RunBundle.from_partial_journal()` (T4.5.5)
  - Bundle loader security model (T4.7.5): path-traversal
    prevention, artifact size caps, JSON-safe metadata size caps,
    SHA-256 ref format validation
  - replay side-effect policy for tools and MCP (`ToolReplayPolicy`,
    default-deny behavior, stub override path, explicit unsafe-allow
    path)
  - ARTIFACT replay nondeterministic-field stripping policy
    (`REPLAY_IGNORE_FIELDS` allowlist covering `timing.wall_ms`,
    `timing.cpu_ms`, any `monotonic_ns` fields)
  - Fast-load flag (`fast=True` skips SHA-256 verification for
    large audio-heavy bundles during interactive debugging)
- [x] Review and merge before implementation.

### T4.1: ReplaySpec and Fidelity Classes

- [x] Create `src/easycat/runtime/replay.py`
- [x] Define `ReplayFidelity` enum: `ARTIFACT`, `SIMULATED`, `LIVE`
- [x] Define `ToolReplayPolicy` enum: `DENY`, `STUB`, `ALLOW`
- [x] Define `ReplaySpec` dataclass: `fidelity`, `from_sequence`,
  `to_sequence`, `stage_filter`, `overrides`, `timing`, `force`,
  `tool_policy`
- [x] Every `ReplaySpec` must have a fidelity value — no default
- [x] `tool_policy` defaults to `DENY`

### T4.2: Stage Replay Implementations

- [x] Implement `STTStage.replay()` for `ARTIFACT` — cassette playback
  of captured audio and partial/final transcripts
- [x] Implement `TTSStage.replay()` for `ARTIFACT` — cassette playback
  of captured audio frames
- [x] **Audio-timing fidelity is configurable per-replay, default
  `fast`.** `ReplaySpec` grows a `timing: Literal["fast", "wall"]
  = "fast"` field. `fast` replays cassettes as quickly as the
  replay consumer can accept them, dropping wall-clock timing in
  favor of determinism — the default for CI, regression tests,
  and bulk bundle analysis. `wall` replays cassettes in real time
  using the original inter-event wall-clock deltas from the
  journal, which is what interruption/barge-in debugging needs
  because interruption points are sensitive to when partial
  transcripts and TTS chunks land. STT/TTS/VAD/Turn cassettes
  carry the original timestamps; `fast` mode strips them,
  `wall` mode honors them. The `REPLAY_IGNORE_FIELDS` allowlist
  (see below) controls which timing fields are masked for
  `fast`-mode byte-determinism.
- [x] Implement `VADStage.replay()` for `ARTIFACT` — runs the live
  VAD backend against captured audio using the state snapshot
  from WS3 T3.7 (thresholds, pause-timer state, backend
  identity/version). Builds on `VADStage.replay_decision(snapshot)`
  from WS3; WS4 adds the end-to-end cassette playback that emits
  the same frame-level events and `speech_start`/`speech_end`
  transitions. Byte-deterministic per WS3 AC3.16 in `fast` timing
  mode.
- [x] Implement `TurnStage.replay()` for `ARTIFACT` — runs the
  SmartTurn ONNX model against the captured audio window using the
  state snapshot from WS3 T3.7 (model identity, features,
  threshold). Builds on `TurnStage.replay_decision(snapshot)` from
  WS3; byte-deterministic per WS3 AC3.17 in `fast` timing mode.
- [x] Implement `AgentStage.replay()` for `SIMULATED` — injects
  captured bridge events into the downstream pipeline, bypasses live
  LLM call. Fidelity label on every record: "LLM responses are
  inherently non-deterministic; this replay is best-effort.
  Framework-level state such as `previous_response_id`, `last_agent`,
  and response-chain continuity is *not* reproduced by SIMULATED
  replay — use ARTIFACT for deterministic downstream stages or
  LIVE for end-to-end reproduction of framework state."
- [x] Implement `replay()` for remaining stages at `ARTIFACT` level
  where possible (`Transport`, `Audio`, `Telephony`):
  - `AudioStage` (noise reduction / echo cancellation):
    `ARTIFACT` replay feeds the captured raw input audio through
    the same NR/EC backend at the same version (version-matched
    per the provider version check). Deterministic when backend
    and version match; version mismatch follows the standard
    provider-version-mismatch policy above.
  - `TransportStage`: `ARTIFACT` replay feeds captured inbound
    audio frames into the downstream pipeline and captures
    outbound frames, bypassing the real transport (no WebSocket,
    no microphone). This is effectively a passthrough cassette.
  - `TelephonyStage`: `ARTIFACT` replay feeds captured telephony
    events (call setup, DTMF, `mark` acknowledgements) from the
    journal, bypassing the live telephony provider. Useful for
    replaying call flows without a live SIP/Twilio connection.
- [x] Implement `LIVE` replay for all stages by reusing the current
  `execute()` path with captured inputs
- [x] **Replay side-effect policy is explicit and fail-closed.**
  - `ARTIFACT` replay never re-enters a live agent/tool path.
  - `SIMULATED` replay also never executes live tools; tool phases
    for the agent stage are satisfied only from captured events.
  - `LIVE` replay obeys `ReplaySpec.tool_policy`:
    - `DENY` (default) — any attempted tool or MCP invocation raises
      `ReplaySideEffectBlocked`
    - `STUB` — the invocation may proceed only from captured tool
      results or explicit replay overrides; no live side effects
    - `ALLOW` — explicit unsafe opt-in, logs a prominent warning,
      marks the replay result as side-effecting, and then reuses the
      real execution path
  - The essential plan treats *all* tool and MCP calls as
    side-effecting during replay unless they are satisfied by the
    `STUB` path. There is no "best effort maybe safe" fallback.
- [x] **Nondeterministic-field stripping for ARTIFACT replay.**
  Extend WS3's `NONDETERMINISTIC_FIELDS` (defined in
  `stages/base.py` T3.5) with replay-specific fields and
  re-export as `REPLAY_IGNORE_FIELDS` in the same module:

  ```python
  # Base set defined in WS3 T3.5 (used by signal-vs-token parity)
  # NONDETERMINISTIC_FIELDS includes: timing.wall_ms, timing.cpu_ms,
  # timing.queue_ms, recorded_at_monotonic_ns, recorded_at_utc,
  # cursor.entered_at, cursor.exited_at, *_at_ns, *_monotonic_ns

  # Replay extends with artifact-specific and deadline fields
  REPLAY_IGNORE_FIELDS: frozenset[str] = NONDETERMINISTIC_FIELDS | frozenset({
      "timing.wall_deadline_ns",
      # Artifact-specific monotonic derivations
      "artifact_written_at",
      "artifact_hashed_at",
  })
  ```

  ARTIFACT replay in `fast` timing mode diffs
  `state_before` / `state_after` snapshots with these fields
  masked. `wall` timing mode does not mask them — byte-
  determinism is not a `wall`-mode goal because wall-mode
  replays real-time interactions against live downstream
  consumers. Without this masking in `fast` mode, byte-
  determinism (AC4.4/AC4.5) is unreachable because every
  snapshot embeds a fresh timestamp.
- [x] **Provider version match check.** Every bundle exported by
  `session.export_debug_bundle` carries per-provider
  `version_info()` strings (from WS1 T1.7.5). On replay, the
  loader compares the bundle's captured version strings against
  the currently installed provider versions and applies this
  policy:
  - **Match**: replay proceeds at the requested
    `ReplayFidelity`.
  - **Mismatch with `fidelity=ARTIFACT`** and
    `ReplaySpec.force=False` (default): the loader raises
    `ProviderVersionMismatchError` with a detailed message
    naming each mismatched provider, the bundle version, and
    the installed version. Replay does not start.
  - **Mismatch with `fidelity=ARTIFACT`** and
    `ReplaySpec.force=True`: the loader logs a prominent
    warning, downgrades the replay fidelity label on the
    resulting output to `LIVE` (because determinism is no
    longer guaranteed), and proceeds.
  - **Mismatch with `fidelity=LIVE` or `fidelity=SIMULATED`**:
    warning only; `LIVE` is non-deterministic by definition and
    `SIMULATED` is explicitly documented as best-effort.
  - **Unknown-version edge case**: if either side reports
    `"unknown"` for a version field (per WS1 T1.7.5's
    stable-shape contract), the loader treats it as a mismatch
    with a specific error code
    (`PROVIDER_VERSION_UNKNOWN`) so CI can fail on it
    explicitly without confusing it with a real version skew.
  `ReplaySpec` grows a `force: bool = False` field for this
  path. The version-match check runs after SHA-256 integrity
  verification and before any stage `replay()` method is
  invoked.

### T4.3: Determinism Guarantees

- [x] `ARTIFACT` replay of STT must produce byte-identical transcripts
  given the same cassette
- [x] `ARTIFACT` replay of TTS must produce byte-identical audio
  frames given the same cassette
- [x] `ARTIFACT` replay of VAD must produce byte-identical frame
  events and `speech_start`/`speech_end` transitions given the
  same audio + snapshot (inherits WS3 AC3.16)
- [x] `ARTIFACT` replay of Smart Turn must produce byte-identical
  endpoint decisions and classification outputs within float
  tolerance given the same audio window + snapshot (inherits WS3
  AC3.17)
- [x] `SIMULATED` replay of agent stage must be deterministic modulo
  the documented LLM non-determinism caveat
- [x] `LIVE` replay is not expected to be deterministic and is
  labeled so

### T4.4: RunBundle Format

- [x] Create `src/easycat/debug/bundle.py`
- [x] Define `RunBundle` dataclass:
  - `format_version: int`
  - `manifest: Manifest` (SHA-256 per artifact, provider version
    strings, safe config snapshot from WS1 `safe_defaults.py`,
    allowlisted env metadata, sharing banner text)
  - `journal_ndjson: bytes`
  - `artifact_index: dict[str, ArtifactEntry]`
  - `replay_entry_points: list[CommittableCheckpoint]`
  - `sharing_banner: str` — dev-only banner text read from WS1
    `safe_defaults.py`; `peripheral-redaction.md` later upgrades
    the banner to a per-field policy summary without touching the
    bundle format
- [x] Define on-disk format: `.easycat-bundle` zip with:

  ```
  manifest.json
  journal.ndjson
  artifacts/
    <sha256>.bin
    <sha256>.bin
    ...
  ```

### T4.5: Export API

- [x] Create `src/easycat/debug/export.py`
- [x] Implement `Session.export_debug_bundle(path, *, inline_artifacts=False, overwrite=False)`:
  - snapshots the current journal
  - persists only allowlisted config/environment metadata via
    WS1 `safe_defaults.py`; no raw `EasyCatConfig.__dict__` or
    `os.environ` dumps leave the process
  - stamps the dev-only sharing banner read from
    `safe_defaults.py` onto the manifest
  - bundles artifacts by reference (default) or inline (if
    `inline_artifacts=True`)
  - aggregates SHA-256 per artifact from WS1's content-addressable
    artifact store (no re-hashing)
  - aggregates provider version strings from every provider the
    session touched (providers already expose `version_info()` from
    WS1 T1.7.5)
  - writes the zip
- [x] A future `redaction=` kwarg is reserved for
  `peripheral-redaction.md`, which adds an export-time second pass
  and upgrades the banner. WS4 does not ship that argument, but the
  export API's signature contract allows it to be added without a
  breaking change.
- [x] `path` handling: if `path` exists and `overwrite=False`
  (default), raise `BundleExists`. With `overwrite=True` the
  existing file is replaced atomically (write to temp, rename).
- [x] Export from `debug="off"` raises `DebugCaptureDisabledError`.
  Export from `debug="light"` is supported only while the required
  in-memory artifacts are still retained; if they have been evicted,
  raise `DebugCaptureUnavailableError` naming the missing refs.
- [x] Export is valid even on a partially-complete journal (e.g.,
  from a crashed session opened via the SQLite recovery path in
  Workstream 1). The partial-journal path uses T4.5.5 rather than
  requiring a live `Session`.

### T4.5.5: Partial Journal Static Loader

- [x] Implement `RunBundle.from_partial_journal(journal_path,
  artifact_root)` static method that constructs a bundle without
  a live `Session` object
- [x] Used by the crash-recovery path and `bundles list` discovery
  (T4.10) — a crashed session promoted to `.easycat/crash-dumps/`
  has no running Session to call `export_debug_bundle` on
- [x] Loads the SQLite journal read-only, walks the WS1
  content-addressable artifact directory, assembles a manifest with
  the same SHA-256 aggregation and safe-config-snapshot rules as
  the live path
- [x] Refuses to load a journal file that is currently open for
  writing (detected via the SQLite WAL lock); returns
  `BundleInUseError` with a message pointing at `bundles list`
  for running sessions

### T4.6: Provider Version Strings (Aggregation Only)

- [x] The `version_info()` retrofit across providers lands in
  Workstream 1 T1.7.5, not here. Workstream 4 consumes the method
  without modifying providers.
- [x] Session collects version info from all active providers at
  export time via the existing `version_info()` methods
- [x] Bundle manifest includes the full set keyed by provider role
  (`stt`, `tts`, `transport`, `telephony`, and bridge provider
  versions where applicable)

### T4.7: Bundle Loading

- [x] Implement `RunBundle.load(path, *, fast=False)` → `RunBundle`
- [x] Reads manifest, verifies SHA-256 checksums, raises on mismatch
- [x] `fast=True` skips SHA-256 verification for large audio-heavy
  bundles during interactive debugging. Documented as a trust
  decision: only use for bundles you produced yourself.
- [x] Rejects bundles whose `format_version` is newer than the
  loader supports with `BundleVersionError` naming both versions
- [x] Exposes queryable journal records (iterator, filter by stage,
  filter by turn, lookup by sequence)
- [x] Loads successfully from bundles exported from partial journals
  (crash recovery via T4.5.5)

### T4.7.5: Bundle Loader Input Validation

- [x] Bundle loader validates untrusted input on every load path
  (`RunBundle.load()`, `RunBundle.from_partial_journal()`)
- [x] Validation rules (any violation raises
  `BundleValidationError` with a specific reason code):
  - every artifact ref matches `^[a-f0-9]{64}$` (SHA-256 hex
    only, no path components, no `..`, no absolute paths)
  - every artifact ref referenced by any journal record exists in
    the bundle's artifact index (no dangling refs)
  - total artifact size does not exceed a configurable cap
    (default 500 MB)
  - per-record `metadata` and `framework_metadata` dicts are
    JSON-safe and under 1 MB each
  - manifest entries contain no path traversal sequences
  - manifest `format_version` is within the supported range
- [x] Validation runs before any artifact is extracted or any
  record is deserialized
- [x] Document explicitly: "Bundles are semi-trusted input.
  Validation defends against accidental corruption and basic
  malicious tampering, not against sophisticated attackers with
  filesystem access."

### T4.8: Committable Boundary Enforcement

- [x] Replay entry points must be `committable` checkpoints per the
  bridge execution cursor from Workstream 2A (published via
  `COMMITTABLE_BOUNDARIES` mappings) and validated by Workstream
  2B's drain-to-commit-point tests
- [x] Attempting to start replay at a non-committable sequence
  returns `ReplayError` with fields `requested_sequence`,
  `nearest_committable_before`, `nearest_committable_after`
- [x] Error message is human-readable and names the stage that was
  mid-operation

### T4.9: Pytest Fixture Helpers

- [x] Add `easycat.debug.testing.load_bundle(path)` helper for pytest
  users (not the full `easycat.testing` module, which is peripheral)
- [x] Support bundles from partial journals for regression testing
  around crash scenarios
- [x] Add one regression test in this workstream that uses the fixture
  to prove the loop closes

### T4.10: Crash Recovery End-to-End

- [x] `bundles list` functionality — discover bundles in the
  directories defined by WS1 T1.2.5's storage layout contract:
  `.easycat/recordings/` (exported bundles) and
  `.easycat/crash-dumps/` (crash-promoted SQLite journals)
- [x] A crashed session (from the SQLite backend surviving SIGKILL
  in Workstream 1) produces a valid bundle via
  `RunBundle.from_partial_journal()` (T4.5.5), without needing a
  live `Session` object
- [x] `bundles list` honors the configurable `EASYCAT_DATA_DIR`
  from WS1 T1.2.5

## Acceptance Criteria

- [x] **AC4.2** `src/easycat/runtime/replay.py` defines `ReplayFidelity`
  and `ReplaySpec`. Every `ReplaySpec` has a non-default fidelity.
- [x] **AC4.3** All 8 stages implement `replay(spec)` for at least the
  `LIVE` fidelity class. STT, TTS, VAD, and Turn (SmartTurn)
  additionally support `ARTIFACT`. Agent stage additionally
  supports `SIMULATED`.
- [x] **AC4.4** `ARTIFACT` replay of STT is byte-deterministic given
  the same cassette.
- [x] **AC4.5** `ARTIFACT` replay of TTS is byte-deterministic given
  the same cassette.
- [x] **AC4.5a** `ARTIFACT` replay of VAD is byte-deterministic:
  given a captured audio artifact and VAD snapshot (from WS3 T3.7),
  replay produces the same frame-level events and
  `speech_start`/`speech_end` transitions as the live session.
  Inherits the backend-parametrization from WS3 AC3.16 (Silero
  mandatory, Krisp integration-gated).
- [x] **AC4.5b** `ARTIFACT` replay of Smart Turn is
  byte-deterministic: given a captured audio window and TurnStage
  snapshot, replay produces the same endpoint classification
  (logits within float tolerance) and the same final
  `complete`/`not_complete` decision as the live session.
- [x] **AC4.6** `src/easycat/debug/bundle.py` and
  `src/easycat/debug/export.py` exist and export a valid bundle.
- [x] **AC4.7** `Session.export_debug_bundle(path)` produces a
  loadable bundle from a running session when capture is available,
  raises `DebugCaptureDisabledError` in `debug="off"`, and raises
  `DebugCaptureUnavailableError` when required light-mode artifacts
  have already been evicted.
- [x] **AC4.8** Bundle manifest includes SHA-256 per artifact.
- [x] **AC4.9** Bundle manifest includes provider version strings for
  every provider touched during the session.
- [x] **AC4.10** Bundle manifest includes `format_version`.
- [x] **AC4.11** Bundle safe-snapshot rules ensure the bundle
  contains only allowlisted config/environment metadata via WS1
  `safe_defaults.py` and the manifest includes the dev-only sharing
  banner. No raw `EasyCatConfig.__dict__` or `os.environ` values
  reach the bundle. A follow-up export-time `RedactionPolicy` pass
  is reserved for `peripheral-redaction.md` and not in scope here.
- [x] **AC4.12** Bundles load correctly from partial journals produced
  by simulated process death (inherits Workstream 1 infrastructure).
- [x] **AC4.13** `load_bundle()` verifies SHA-256 checksums and raises
  on mismatch.
- [x] **AC4.14** Replay at a non-committable sequence returns
  `ReplayError` with nearest committable checkpoint references.
- [x] **AC4.15** `load_bundle()` pytest helper is usable and covered by
  at least one in-workstream regression test.
- [x] **AC4.16** `bundles list` discovers crash-dumped bundles on disk.
- [x] **AC4.17** The public export/load/replay surface is frozen in the
  plan and covered by migration notes if this workstream changes config or
  debug APIs.
- [x] **AC4.18** ARTIFACT replay of a captured session produces
  byte-identical stage outputs after applying the
  `REPLAY_IGNORE_FIELDS` allowlist. A test captures a session,
  replays it twice, and diffs the resulting outputs with masked
  nondeterministic fields; the diff must be empty.
- [x] **AC4.19** Bundle loader input validation (T4.7.5) is
  covered by a malformed-bundle test corpus containing path-
  traversal attempts, oversized artifacts, non-JSON metadata,
  malformed SHA-256 refs, dangling artifact refs, and a
  `format_version` newer than the loader. Each case raises
  `BundleValidationError` (or
  `BundleVersionError` for version mismatch) with the expected
  reason code.
- [x] **AC4.20** `RunBundle.from_partial_journal()` loads a
  crashed session's journal and artifacts without requiring a
  running `Session`. The test uses the same SIGKILL pattern as
  Workstream 1 T1.6, promotes the SQLite file to
  `.easycat/crash-dumps/`, and asserts the resulting bundle round-
  trips through `RunBundle.load()`.
- [x] **AC4.21** Provider version match check. Four sub-tests
  exercise every branch of the T4.2 policy:
  1. **Match case**: replay a bundle whose captured versions
     match installed versions at `fidelity=ARTIFACT`; replay
     proceeds, output is labeled `ARTIFACT`.
  2. **Mismatch without force**: replay a bundle whose captured
     `stt` provider version differs from the installed one at
     `fidelity=ARTIFACT`, `force=False`; loader raises
     `ProviderVersionMismatchError` naming the mismatched
     provider and both version strings.
  3. **Mismatch with force**: same as (2) but `force=True`; a
     warning is logged, replay proceeds, the resulting output's
     fidelity label is downgraded to `LIVE`.
  4. **Unknown version edge**: replay a bundle whose captured
     version field is `"unknown"`; loader raises with error
     code `PROVIDER_VERSION_UNKNOWN`, distinguishable from the
     plain mismatch error.
- [x] **AC4.22** Audio-timing fidelity modes. Two sub-tests:
  1. `ReplaySpec(fidelity=ARTIFACT, timing="fast")` replays an
     STT cassette in as-fast-as-possible mode and asserts
     byte-identical transcript output with
     `REPLAY_IGNORE_FIELDS` masked.
  2. `ReplaySpec(fidelity=ARTIFACT, timing="wall")` replays the
     same cassette in real time and asserts the inter-event
     wall-clock deltas match the original within a 20ms
     tolerance. This is the mode interruption/barge-in
     debugging uses.
- [x] **AC4.23** Cross-workstream round-trip. End-to-end
  integration test covers the full loop with no mocking:
  1. Construct a real voice session via `create_session` with
     the WS3 stage stack, WS2A bridges, WS2B atomic interruption.
  2. Drive one complete turn through the chained pipeline with
     a real STT cassette (recorded fixture) and stub agent.
  3. Call `session.export_debug_bundle(tmp_path)` — exercises
     WS4 export API with WS1 artifact store and safe-default
     allowlist.
  4. Call `RunBundle.load(tmp_path)` in a fresh Python process
     to prove no hidden Session state leaks into the bundle.
  5. Call `bundle.replay(ReplaySpec(fidelity=ARTIFACT,
     timing="fast"))` and compare outputs stage-by-stage against
     the original journal with `REPLAY_IGNORE_FIELDS` masked.
  6. Assert the diff is empty — every stage's output round-
     trips through export, load, and replay with byte-
     identical fidelity (modulo masked fields).
  This is the load-bearing test that the five workstreams hold
  together. If it fails, a workstream boundary is broken and the
  failing workstream owns the fix.
- [x] **AC4.24** Replay tool safety. Three sub-tests:
  1. `ReplaySpec(tool_policy=DENY)` blocks the first tool or MCP
     invocation with `ReplaySideEffectBlocked`.
  2. `ReplaySpec(tool_policy=STUB)` satisfies the same invocation
     from a captured tool result or explicit stub override and
     produces no live side effect.
  3. `ReplaySpec(tool_policy=ALLOW)` logs a warning, marks the
     replay result as unsafe/side-effecting, and then permits the
     live tool path.

## Verification

| AC | Verification |
|---|---|
| AC4.2 | `python -c "from easycat.runtime.replay import ReplayFidelity, ReplaySpec; ReplaySpec()"` fails without explicit fidelity; `ReplaySpec(fidelity=ReplayFidelity.ARTIFACT)` succeeds. |
| AC4.3 | New test `test_all_stages_support_live_replay` — parametrized over 8 stages, calls `replay(ReplaySpec(fidelity=LIVE))` and asserts no `NotImplementedError`. Sub-tests assert STT and TTS support `ARTIFACT` and Agent supports `SIMULATED`. |
| AC4.4 | New test `test_stt_artifact_replay_bit_deterministic` — captures STT cassette from a real session, replays against the same stage instance twice, asserts byte-identical transcript output. |
| AC4.5 | New test `test_tts_artifact_replay_bit_deterministic` — same for TTS. |
| AC4.5a | New test `test_vad_artifact_replay_bit_deterministic` — captures a VAD cassette (audio artifact + `VADStage` snapshot) from a live session, replays against a fresh stage instance via `VADStage.replay(ReplaySpec(fidelity=ARTIFACT))`, asserts byte-identical frame events and `speech_start`/`speech_end` transitions. Parametrized over Silero (mandatory) and Krisp (integration-gated). Cross-references WS3 AC3.16. |
| AC4.5b | New test `test_smart_turn_artifact_replay_bit_deterministic` — captures a Smart Turn cassette (audio window + `TurnStage` snapshot), replays via `TurnStage.replay(ReplaySpec(fidelity=ARTIFACT))`, asserts byte-identical classification (logits within float tolerance) and endpoint decision. Cross-references WS3 AC3.17. |
| AC4.6 | `python -c "from easycat.debug.bundle import RunBundle; from easycat.debug.export import export_debug_bundle"` exits 0. |
| AC4.7 | New test `test_export_and_load_roundtrip` — runs a one-turn session, exports to a temp file, loads, asserts journal records round-trip. Sub-tests cover `debug="off"` raising `DebugCaptureDisabledError` and a light-mode retention-expiry case raising `DebugCaptureUnavailableError`. |
| AC4.8 | New test `test_bundle_manifest_sha256` — exports a bundle, parses `manifest.json`, asserts every artifact entry has a `sha256` field that matches the file content hash. |
| AC4.9 | New test `test_bundle_provider_versions` — runs with Deepgram + ElevenLabs, exports, asserts manifest contains version info for both. |
| AC4.10 | Same test as AC4.9 asserts `format_version` is present and > 0. |
| AC4.11 | New test `test_export_safe_defaults_and_banner` — runs a session with a config containing a synthetic API key and a non-`EASYCAT_*` env var, exports a bundle, loads it, asserts: (a) the API key does not appear anywhere in the bundle, (b) the non-allowlisted env var does not appear anywhere, (c) the manifest contains the dev-only sharing banner string read from `safe_defaults.py`, (d) the `redaction=` kwarg is not yet exposed on the export signature (reserved for `peripheral-redaction.md`). |
| AC4.12 | New test `test_partial_journal_bundle_export` — uses the subprocess-SIGKILL pattern from Workstream 1, reopens the SQLite journal, exports to a bundle, loads, asserts the records prior to the crash are present. |
| AC4.13 | New test `test_bundle_manifest_tamper_detection` — exports a bundle, manually corrupts one artifact byte, attempts to load, asserts `BundleIntegrityError` is raised. |
| AC4.14 | New test `test_replay_refuses_non_committable` — captures a mid-LLM-stream sequence, constructs `ReplaySpec` at that sequence, asserts `ReplayError` with populated `nearest_committable_before`/`after`. |
| AC4.15 | Demonstration regression test `test_bundle_as_fixture` — loads a committed fixture bundle via `load_bundle()`, asserts a journal property. The test itself is the proof that the fixture helper works. |
| AC4.16 | New test `test_bundles_list_discovery` — writes two bundles to a temp directory, calls the discovery function, asserts both are found. |
| AC4.17 | Migration note include the frozen export/load/replay surface and before/after examples for any config or debug-surface changes introduced here. |
| AC4.18 | New test `test_replay_nondeterministic_field_stripping` — captures a session, runs ARTIFACT replay twice, diffs outputs with `REPLAY_IGNORE_FIELDS` masked, asserts empty diff. |
| AC4.19 | New test `test_bundle_loader_validation_corpus` — a fixture directory of malformed bundles (path-traversal, oversized artifact, non-JSON metadata, bad SHA-256 ref, dangling artifact ref, newer format_version). Loader raises `BundleValidationError` or `BundleVersionError` with the expected reason code for each. |
| AC4.20 | New test `test_partial_journal_loader_after_sigkill` — reuses the WS1 T1.6 SIGKILL harness, promotes the SQLite file to `.easycat/crash-dumps/`, calls `RunBundle.from_partial_journal()`, asserts the bundle round-trips through `RunBundle.load()` and contains records prior to the crash. |
| AC4.21 | Parametrized test `test_provider_version_match` over four cases: match (passes), mismatch-no-force (raises `ProviderVersionMismatchError`), mismatch-force (warns, downgrades to `LIVE`), unknown-version (raises with `PROVIDER_VERSION_UNKNOWN`). Each case patches `provider.version_info()` to return controlled values. |
| AC4.22 | Two new tests: `test_replay_fast_timing_deterministic` asserts byte-identical output with `REPLAY_IGNORE_FIELDS` masked; `test_replay_wall_timing_real_time` records start/stop wall-clock, asserts total replay duration is within 20ms of the sum of original inter-event deltas. |
| AC4.23 | New integration test `test_cross_workstream_round_trip` in `tests/integration/` — constructs a real session, runs a turn with a committed STT cassette fixture, exports a bundle, loads it in a fresh subprocess, replays with `ARTIFACT` fidelity and `fast` timing, diffs against the original journal. Diff must be empty modulo `REPLAY_IGNORE_FIELDS`. Gated on the fixture cassette existing; first run generates it and subsequent runs replay. |
| AC4.24 | New test `test_replay_tool_policy_deny_stub_allow` — captures a turn with a tool call, replays it three times: `DENY` raises `ReplaySideEffectBlocked`, `STUB` uses captured result or explicit override without touching the live tool, `ALLOW` logs a warning and marks the replay output as unsafe/side-effecting. |

## Risks and Mitigations

- **Cassette format drifts across provider version bumps**: covered
  by T4.2's "Provider version match check" task. Implementation
  captures provider version strings in the cassette (via WS1 T1.7.5)
  and refuses `ARTIFACT` replay on mismatch unless
  `ReplaySpec.force=True`, in which case the resulting replay is
  re-labeled as `LIVE` rather than `ARTIFACT`. AC4.21 exercises all
  paths including the unknown-version edge case.
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
- **Users accidentally replay live side effects**: mitigation —
  `ToolReplayPolicy.DENY` is the default, `STUB` is the recommended
  override, and `ALLOW` requires an explicit opt-in that emits a
  warning and marks the replay result as unsafe/side-effecting.
- **Committable boundary checks may reject valid replay points** the
  bridge authors didn't anticipate: mitigation — the
  `ExecutionCursor.committable` flag is the single source of truth;
  if a bridge marks too few boundaries as committable, fix the
  bridge in Workstream 2A rather than bypassing the check.

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
