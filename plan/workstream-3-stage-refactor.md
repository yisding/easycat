# Workstream 3: Stage Refactor and Session Decomposition

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstream 1 (Journal Foundation) and Workstream 2A
> (Agent Bridge Protocol and Bridges) must both be complete. Runs in
> parallel with **Workstream 2B (Interruption and MCP)**: WS3 T3.2
> (`InterruptionController` extraction) is the runtime-side consumer
> of WS2B's bridge-side interruption contract, so the two workstreams
> land together rather than sequentially.
>
> **Merge ordering**: WS3 merges first (controller extraction,
> stage ports, text mode). WS2B merges second (exercises the
> controller through the bridges with the full cancellation-mode
> matrix). WS2B's CI runs against the WS3 branch during
> development; the final merge is sequenced WS3 → WS2B within
> the same release.
> **Successors**: Workstream 4 (Replay and Bundle) depends on the
> `Stage.replay()` hook introduced here.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2a-agent-bridges.md`
> - `workstream-2b-interruption-and-mcp.md`
> - `workstream-4-replay-and-bundle.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may introduce a new runtime-mode or
> stage-facing public surface if needed; the requirement is explicit
> migration documentation, not signature stability.

## Goal

Decompose `src/easycat/session/_session.py` (currently 1,512 lines)
into stage + context + controller types so stage execution is the
main orchestration model, each stage boundary is journaled with
`state_before`/`state_after` snapshots, and upstream control signals
(interruption, cancel, pause, backpressure) are recorded per-stage.

## Scope

**In scope:**

- Extend `TurnContext` to hold per-turn state currently on Session
  instance variables
- Extract `InterruptionController` from Session
- Extract `VoiceDeliveryLedger` from Session
- Define `RunContext`
- Define `Stage` protocol with `execute`, `snapshot_state`, `replay`,
  `handle_upstream`
- Define typed, secret-safe `StageStateSnapshot`
- Port 8 stages: `Transport`, `Audio`, `VAD`, `STT`, `Turn`, `Agent`,
  `TTS`, `Telephony`
- Wire journal into stages (`state_before`/`state_after` snapshots)
- Upstream control signal flow (Pipecat-style bidirectional)
- Reduce Session to a thin facade (target `< 400` lines, hard ceiling
  `500`)
- Support both runtime modes: `chained_pipeline` and `text_session`.
  Voice-to-voice / realtime speech-to-speech is explicitly out of
  scope (see Explicit Guardrails in the essential plan).
- `Session.send_text()` public API and `create_text_session()`
  factory helper — the text-mode entry point that exercises the same
  journal, bridge, and interruption contract as the voice runtime

**Out of scope:**

- `replay` hook implementation details beyond the stubbed protocol
  method (Workstream 4 fleshes it out per stage)
- `WarmupStage` (peripheral — in `peripheral-observability-and-cost.md`)
- Removal of Session instance-variable fields that are now duplicated
  (Workstream 5)

## Tasks

### T3.0: Architecture Freeze (RFC)

- [ ] Write Phase 3 RFC covering:
  - extraction order: `TurnContext` (as three atomic groupings,
    see T3.1) → `InterruptionController` → `VoiceDeliveryLedger`
    → stages
  - `Stage` protocol signature and `ControlSignal` types, with
    cross-reference to WS1 T1.1's `ControlSignalRecord` shape
  - `RunContext` field list
  - public runtime-mode/config surface for `chained_pipeline` vs
    `text_session`
  - `chained_pipeline` vs `text_session` mode wiring differences
    (which stages are active, how `Session.send_text()` drives the
    `TurnStage` → `AgentStage` path without the audio stages)
  - perf baseline reference and regression gate thresholds
    (consumes T1.0.5 baseline)
  - dual-path signal/token coexistence window: shared cancel token
    and upstream signal flow run side-by-side through WS3 and WS4;
    the token is removed in WS5 T5.2.5 after parity is proven
  - rollback strategy if a stage port regresses behavior
- [ ] Review and merge before any Session changes.

### Step 1 — Extract from Session

### T3.1: Extend TurnContext

- [ ] Current `src/easycat/session/_turn_context.py` is 64 lines —
  extend it to hold per-turn state currently scattered across
  `_session.py` instance variables
- [ ] Session delegates to `TurnContext` via a single
  `self._current_turn: TurnContext` attribute
- [ ] **Extract in three atomic groupings, not one field at a
  time.** Several fields are entangled via the interruption
  estimator in `_interruption.py` / `_tts_helpers.py` and must
  move together or tests break mid-sequence. The three groupings:
  - **Group A — playback tracking (atomic):**
    `_agent_response_parts`, `_tts_chunks`,
    `_playback_mark_to_bytes`. Extract as one commit.
    Interruption estimator reads across all three.
  - **Group B — turn timing and cancellation:** turn timings
    (turn start, agent start, first TTS byte, etc.), interruption
    metadata, cancel token. Extract as one commit.
  - **Group C — telephony hooks:** telephony state hooks and
    playback state not already in Group A. Extract as one commit.
- [ ] Verify the full `tests/session/` suite passes after each
  grouping is extracted. Commit each grouping separately so bisect
  remains useful within the workstream.

### T3.2: Extract InterruptionController

- [ ] Create `src/easycat/session/_interruption_controller.py`
- [ ] Move interruption detection, delivered-text computation, policy
  selection, and bridge interaction out of `_session.py`
- [ ] The controller owns the seven-step interruption flow defined in
  Workstream 2B (`workstream-2b-interruption-and-mcp.md` T2B.1)
- [ ] The controller handles the `ShallowModeInterruptionError`
  downgrade path (WS2B T2B.2): when a shallow-mode
  `GenericWorkflowBridge` raises the exception, the controller
  emits a `ControlSignalRecord(cause="shallow_mode_downgrade")`
  and downgrades the turn to end-of-turn interruption.
- [ ] Session delegates to the controller
- [ ] All barge-in tests in `tests/session/` pass unmodified. The
  WS2B three-cancellation-mode test matrix exercises this
  controller through the bridges in parallel — if WS3 lands
  before WS2B the full matrix lives under a WS2B feature flag
  that is flipped on when WS2B merges.

### T3.3: Extract VoiceDeliveryLedger

- [ ] Create `src/easycat/session/_voice_delivery_ledger.py`
- [ ] Move user transcript tracking, raw agent text, post-processed
  spoken text, playback acknowledgements, estimated delivered text at
  interruption, interruption cut points and confidence
- [ ] Ledger writes through the journal via `AgentRecorder`
- [ ] Verify voice delivery tests pass

### T3.4: Define RunContext

- [ ] Create `src/easycat/runtime/context.py`
- [ ] Define `RunContext` dataclass: `run_id`, `session_id`, config
  snapshot, runtime mode (`chained_pipeline` | `text_session`),
  artifact store handle, journal handle
- [ ] `RunContext` stores a safe config snapshot via the WS1 hard-coded
  allowlist in `safe_defaults.py`, not a raw `EasyCatConfig.__dict__`.
  A full `RedactionPolicy` lands in `peripheral-redaction.md` and
  plugs into the existing `apply_write_filter` hook without
  changing the `RunContext` shape.
- [ ] Construct once per session, pass to every stage

### Step 2 — Stage interfaces

### T3.5: Stage Protocol

- [ ] Create `src/easycat/stages/base.py`
- [ ] Define `StageStateSnapshot` dataclass/protocol for JSON-safe,
  secret-safe stage state capture
- [ ] Define `Stage` Protocol:

  ```python
  class Stage(Protocol):
      async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any: ...
      def snapshot_state(self) -> StageStateSnapshot: ...
      def replay(self, spec: ReplaySpec) -> Any: ...
      async def handle_upstream(self, signal: ControlSignal) -> None: ...
  ```

- [ ] Define `ControlSignal` sum type: `Interrupt`, `Cancel`, `Pause`,
  `Resume`, `Backpressure`
- [ ] Stages emit `ControlSignalRecord` (defined in WS1 T1.1) when
  they observe a control signal via `handle_upstream`. Stages MUST
  NOT invent a new record shape — the cross-framework shape lives
  in WS1's records module.
- [ ] Stub `ReplaySpec` (filled out in Workstream 4)
- [ ] Helpers in `base.py` for journal record emission from stage
  operations

### T3.6: Port STT, Agent, TTS (highest debugging value)

- [ ] Create `src/easycat/stages/stt.py` — wrap existing STT provider
  calls, emit `state_before`/`state_after` via `snapshot_state()`,
  write journal records
- [ ] Create `src/easycat/stages/agent.py` — wrap the
  `ExternalAgentBridge` from Workstream 2A
- [ ] Create `src/easycat/stages/tts.py` — wrap existing TTS provider
  calls
- [ ] Verify `tests/stt/`, `tests/session/` agent tests, and
  `tests/tts/` pass unmodified

### T3.7: Port Transport, Audio, VAD, Turn, Telephony

- [ ] Create `src/easycat/stages/transport.py`
- [ ] Create `src/easycat/stages/audio.py` (noise reduction + echo
  cancellation)
- [ ] Create `src/easycat/stages/vad.py`
- [ ] Create `src/easycat/stages/turn.py` (including SmartTurn
  endpoint detection)
- [ ] Create `src/easycat/stages/telephony.py`
- [ ] Verify corresponding test directories pass unmodified
- [ ] **VAD decision reproducibility (hard requirement).**
  `VADStage.snapshot_state()` must capture everything needed to
  re-derive the same VAD decision offline:
  - the input audio frame(s) by artifact ref (content-addressable
    SHA-256 from WS1 T1.2), never inline
  - per-frame probability/energy values emitted by the VAD backend
  - active threshold and any dynamic-threshold state (adaptive VAD
    floor, ambient noise estimate)
  - in-speech flag before and after the frame
  - pause-timer deadline(s) (`silence_duration_ms`,
    `min_speech_ms`, `hangover_ms`) and their current countdown
  - backend identity and version (Silero model version hash,
    Krisp SDK version) so cross-version drift is visible
  - the decision the stage emitted (`speech_start`, `speech_end`,
    `no_change`) so replay can diff live vs replay outputs
- [ ] **Smart Turn decision reproducibility (hard requirement).**
  `TurnStage.snapshot_state()` must capture everything needed to
  re-derive the same endpoint classification offline:
  - the input audio window by artifact ref
  - ONNX model identity, file hash, and version
  - feature inputs fed to the model (whatever tensor or feature
    vector Smart Turn consumes)
  - raw classification output (logits or probability)
  - decision threshold currently in effect
  - the final endpoint decision (`complete` / `not_complete`) and
    any fallback behavior that fired (e.g., timeout override)
- [ ] Both stages expose a `replay_decision(snapshot)` helper that
  returns the same decision the live session made given a captured
  snapshot. This helper is what WS4 ARTIFACT replay calls; it has
  no side effects and no provider calls beyond running the local
  model against captured inputs.

### T3.8: Upstream Control Signals

- [ ] Each stage implements `handle_upstream(signal)` — at minimum,
  emits a `ControlSignalRecord` (WS1 T1.1) so we can see which
  stage observed the signal and in what state
- [ ] Plumb control signal flow through the session: upstream signals
  walk from late stages (TTS, Transport) back toward early stages
  (VAD, STT)
- [ ] **Dual-path coexistence.** The shared cancel token is NOT
  removed in this workstream. Signal-based upstream flow runs
  side-by-side with the existing shared cancel token through WS3
  and WS4. Both paths must stay behavior-equivalent until WS5
  T5.2.5 removes the token. This is deliberate: the interruption
  tests are the safety net and they must not regress during the
  signal plumbing work.
- [ ] Verify interruption tests still pass with the new signal flow
  in place and the shared token still live

### Step 3 — Wire journal and reduce Session

### T3.9: Journal Wiring

- [ ] Each stage writes records through the journal via helpers in
  `stages/base.py`
- [ ] Stages still emit legacy events through WS1's strangler-fig
  adapters during this workstream; dual-write stays on until
  WS5's flip. WS3 must not break the T1.8.5 parity tests — if a
  stage port causes parity drift, fix the drift before merging.
- [ ] Remove direct observability calls from `_session.py` (the
  strangler-fig adapters from Workstream 1 still cover legacy call
  sites; this step removes the direct journal writes that Session was
  doing)
- [ ] Every stage operation produces at minimum a `start` and
  `complete` (or `error`/`cancel`) record pair with non-null
  `state_before` and `state_after` snapshots
- [ ] `StageStateSnapshot` values flow through the WS1 T1.5
  `apply_write_filter` hook and honor the hard-coded safe default
  on every write — stages must not bypass the hook. The hook is a
  no-op for non-sensitive fields in WS1; `peripheral-redaction.md`
  layers a full `RedactionPolicy` on top without changing stage code.

### T3.10: Session as Facade

- [ ] Session retains: lifecycle (start/stop/close), stage wiring,
  `TurnContext` creation, and orchestration loops
- [ ] Session delegates everything else to the extracted components
- [ ] Target: `wc -l src/easycat/session/_session.py` < 400 lines, with
  a hard ceiling of 500 lines
- [ ] No per-turn state on Session instance variables (verified by
  introspection test)

### T3.11: Runtime Mode Support

- [ ] `RunContext.runtime_mode` drives which stages are active
- [ ] In `chained_pipeline` mode, all 8 stages are active
- [ ] In `text_session` mode, the stage set is:
  - `TurnStage` (active as an explicit-boundary driver — each
    `send_text` call begins and ends exactly one turn; SmartTurn
    endpointing is not engaged)
  - `AgentStage` (active, same WS2 bridge invocation as the voice
    path)
  - all audio stages (`Transport`, `Audio`, `VAD`, `STT`, `TTS`,
    `Telephony`) are inactive
- [ ] In `text_session` mode, `VoiceDeliveryLedger` operates in
  **text-delivery mode**: every text delta yielded to the caller's
  `send_text` iterator is immediately marked as delivered (no
  playback acknowledgement needed, no estimator fallback). The
  `InterruptionController` reads delivered text from the ledger
  the same way it does in voice mode — the only difference is
  that delivery is instantaneous rather than bounded by TTS
  playback latency. This means interruption cut-point computation
  in text mode is exact (delivered = yielded), not estimated.
- [ ] Voice-to-voice / realtime speech-to-speech is not a supported
  runtime mode. `RunContext.runtime_mode` only accepts
  `chained_pipeline` or `text_session`; any other value raises a
  `ValueError` at construction. See Explicit Guardrails in the
  essential plan.

### T3.11.5: Text Mode Public API

- [ ] Add `Session.send_text(text: str, *, context=None) ->
  AsyncIterator[AgentBridgeEvent]` method available in
  `text_session` mode. Raises `RuntimeError` with a clear message
  if called from `chained_pipeline` mode.
- [ ] Implementation path: `send_text` opens a fresh `TurnContext`,
  constructs an `AgentTurnInput` via the WS2 T2.1
  `AgentTurnInput.from_text()` helper, runs it through the same
  `TurnStage` → `AgentStage` wiring used by the voice runtime
  (minus the audio stages), forwards `AgentBridgeEvent`s to the
  caller, and closes the turn. No code duplication between text
  and voice paths — same stage invocation, same journal records,
  same framework transition records, same interruption contract.
- [ ] Expose `create_text_session(...)` as a factory helper
  alongside `create_session(...)` in `config.py` / `__init__.py`.
  It returns a `Session` pre-configured with
  `runtime_mode="text_session"` and no audio provider wiring.
  `create_session` with an explicit `runtime_mode="text_session"`
  also works; the factory is a thin convenience.
- [ ] `send_text` routes interruption the same way voice mode does:
  if a concurrent `send_text` call races with an in-flight agent
  turn, the `InterruptionController` observes the new text input
  as an interrupt signal and applies the configured cancellation
  mode to the bridge. **Detection mechanism:** `send_text`
  checks `session._current_turn` before opening a new turn. If a
  turn is already active (the previous `send_text`'s agent stage
  is still streaming), `send_text` calls
  `InterruptionController.signal_text_interrupt(new_text)` which
  emits a `ControlSignalRecord(signal_kind="interrupt",
  cause="concurrent_send_text")` and applies the configured
  cancellation mode to the bridge — the same path voice mode's
  barge-in takes after VAD detection, minus the delivered-text
  estimation (in text mode, delivered text = all text yielded to
  the caller's iterator so far, since there is no audio playback
  latency). The controller then waits for the in-flight turn to
  drain or stop per the cancellation mode before the new turn
  proceeds. This keeps interruption semantics uniform across
  runtime modes and makes text mode a valid repro path for
  interruption bugs.
- [ ] Text mode journal records use `stage="turn"` and
  `stage="agent"` with a `runtime_mode="text_session"` field on
  `RunContext` so the debugger UI, replay, and bundle export can
  distinguish text-mode turns from voice-mode turns without
  inventing a new record type.

### T3.12: Perf Regression Gate

- [ ] Re-run the T1.0.5 baseline harness after each stage port
- [ ] Fail the workstream if P50 turn latency regresses > 5% or
  P90 turn latency regresses > 10% against the baseline
- [ ] A regression halts further stage work; profile and fix
  before continuing
- [ ] Capture post-workstream results in `perf/ws3-final.json` for
  future comparison
- [ ] **CI variance mitigation.** The perf gate runs on a
  fixed-spec CI runner (dedicated or tagged instance type, not a
  shared pool). Each measurement is the median of 5 consecutive
  harness runs within the same CI job to dampen single-run noise.
  The baseline (`perf/baseline.json`) records the runner spec; if
  the runner spec changes, re-capture the baseline before
  comparing. If a fixed runner is unavailable, widen the gate
  thresholds to P50 >10% / P90 >15% and document the wider
  margin in the RFC.

## Acceptance Criteria

- [ ] **AC3.1** RFC reviewed and merged.
- [ ] **AC3.2** `TurnContext` holds all per-turn state. Behavior
  check: after a completed turn, `session._current_turn is None`,
  and any attempt to read turn-specific payloads off `Session`
  (e.g., last agent response parts, last playback byte map)
  either raises `AttributeError` or returns `None`. Structural
  check: at rest (no active turn) `Session.__dict__` contains no
  entries whose values are turn-specific payload types
  (`list[AgentResponsePart]`, `dict[PlaybackMark, int]`, etc.).
  The check walks values by type rather than attribute-name
  regex to avoid brittleness.
- [ ] **AC3.3** `InterruptionController` exists in
  `src/easycat/session/_interruption_controller.py` and owns all
  interruption logic.
- [ ] **AC3.4** `VoiceDeliveryLedger` exists in
  `src/easycat/session/_voice_delivery_ledger.py` and is the single
  source of truth for voice channel delivery state.
- [ ] **AC3.5** `RunContext` exists in `src/easycat/runtime/context.py`
  with all required fields, including a safe config snapshot via the
  WS1 `safe_defaults.py` allowlist.
- [ ] **AC3.6** `Stage` protocol exists in `src/easycat/stages/base.py`
  with all four methods and typed `StageStateSnapshot`.
- [ ] **AC3.7** All 8 stages exist as files under `src/easycat/stages/`
  and each implements the `Stage` protocol.
- [ ] **AC3.8** Every stage writes journal records with non-null
  `state_before` and `state_after` on every invocation.
- [ ] **AC3.8a** Mid-stage crash durability. Stages emit
  `state_before` *before* running their critical-path work and
  `state_after` *after* it. If a crash (SIGKILL or segfault)
  occurs between the two, the reloaded SQLite journal shows a
  `state_before` record with no matching `state_after` within
  that turn, and a subsequent `RecoveredSessionMarker` at
  `sequence=0` per WS1 T1.6. A test using the WS1 SIGKILL
  harness asserts: stages that crashed mid-execution leave an
  unmatched `state_before` record, the journal is loadable
  offline, and `RunBundle.from_partial_journal()` (WS4 T4.5.5)
  produces a bundle that shows the stage hang-point in the
  debugger. This is the foundation for debugging field crashes
  without re-running the turn.
- [ ] **AC3.9** Upstream control signals (`Interrupt`, `Cancel`,
  `Pause`, `Resume`, `Backpressure`) are recorded per-stage in the
  journal. Each stage that observes a signal writes its own record.
- [ ] **AC3.9a** Signal-vs-cancel-token parity. The existing
  shared cancel token path and the new signal-based upstream
  flow are behavior-equivalent. A test runs each pre-existing
  `tests/session/` barge-in scenario twice — once with
  `EASYCAT_SIGNAL_CANCEL_MODE=shared_token_only` and once with
  `EASYCAT_SIGNAL_CANCEL_MODE=signal_only` — and diffs the
  journal records (modulo `REPLAY_IGNORE_FIELDS` from WS4) to
  assert identical behavior. Any divergence blocks the
  workstream and must be fixed before WS5 T5.2.5 can remove
  the shared token. The dual-mode harness ships in this
  workstream and runs on every PR that touches
  `src/easycat/stages/` or `src/easycat/session/` until WS5
  flips it off.
- [ ] **AC3.10a** `src/easycat/session/_session.py` is a thin facade
  and stays under the 500-line hard ceiling; `< 400` remains the
  target.
- [ ] **AC3.10b** Every public method on the `Session` facade is at
  most 30 statements long (configurable via a lint rule). This
  structural gate prevents a mega-method from defeating the line
  budget. Exceptions must be explicitly marked and justified in
  the RFC.
- [ ] **AC3.11** `chained_pipeline` mode works end-to-end with OpenAI
  Agents + Deepgram STT + ElevenLabs TTS.
- [ ] **AC3.12** Voice-to-voice / realtime guardrail. A test
  asserts that `RunContext(runtime_mode="realtime_session")`
  raises `ValueError`, and a grep-based test asserts zero
  matches in `src/easycat/stages/` or `src/easycat/session/`
  for `RealtimeStage` or `realtime_session`.
- [ ] **AC3.13** All existing tests pass.
- [ ] **AC3.14** Any public runtime-mode/config changes introduced here
  are frozen in the RFC and covered by migration notes with before/after
  examples.
- [ ] **AC3.15** Perf regression gate (T3.12) passes: P50 turn
  latency stays within 5% and P90 within 10% of the T1.0.5
  baseline. The gate runs on every PR that touches
  `src/easycat/stages/` or `src/easycat/session/` and blocks merge
  on regression.
- [ ] **AC3.16** VAD decision reproducibility. A test captures a
  `VADStage` snapshot from a live session with a known audio
  fixture, calls `VADStage.replay_decision(snapshot)` on a fresh
  stage instance using only the journal snapshot + captured audio
  artifact (no live provider), and asserts the replayed decision
  is byte-identical to the original (same frame-level emissions,
  same `speech_start` / `speech_end` events, same timing offsets).
  Parametrized over the Silero backend (mandatory) and Krisp
  backend if credentials are available (otherwise skipped with a
  log line).
- [ ] **AC3.17** Smart Turn decision reproducibility. A test
  captures a `TurnStage` snapshot for an endpointing decision,
  calls `TurnStage.replay_decision(snapshot)` on a fresh stage
  instance using only the journal snapshot + captured audio window,
  and asserts the replayed classification and decision are
  byte-identical (same logits within float tolerance, same final
  `complete` / `not_complete` output, same fallback behavior if
  any).
- [ ] **AC3.18** Text mode end-to-end. A test creates a
  `text_session` Session (via `create_text_session` or
  `create_session(runtime_mode="text_session")`), calls
  `session.send_text("hello")`, iterates the resulting
  `AgentBridgeEvent` stream, and asserts:
  - the journal contains `stage="turn"` and `stage="agent"` records
    for the turn
  - the journal contains zero records with `stage in {"transport",
    "audio", "vad", "stt", "tts", "telephony"}`
  - the `AgentStage` invoked the same WS2 bridge path the voice
    runtime uses (verified by inspecting the bridge recorder calls
    on the journal)
  - `session.send_text` raises `RuntimeError` when called on a
    Session created with `runtime_mode="chained_pipeline"`
- [ ] **AC3.19** Text mode interruption parity. A test fires a
  second `send_text` while a first `send_text` call is still
  streaming, asserts the `InterruptionController` observes the
  second input as an interrupt, the journal records a
  `CancellationMode` matching the configured policy, and the
  bridge receives the same interruption signal it would from a
  voice-mode barge-in.

## Verification

| AC | Verification |
|---|---|
| AC3.1 | Git log shows RFC merge commit. |
| AC3.2 | New test `test_no_per_turn_state_on_session` — after completing a turn, asserts `session._current_turn is None` and walks `session.__dict__` values by type, asserting none are turn-specific payload types (`list[AgentResponsePart]`, `dict[PlaybackMark, int]`, etc.). |
| AC3.3 | `python -c "from easycat.session._interruption_controller import InterruptionController"` exits 0; `grep -rn 'InterruptionController(' src/easycat/session/_session.py` shows delegation. |
| AC3.4 | Same as AC3.3 for `VoiceDeliveryLedger`. |
| AC3.5 | `python -c "from easycat.runtime.context import RunContext"` exits 0; new test asserts required fields are present. |
| AC3.6 | `python -c "from easycat.stages.base import Stage"` exits 0; new test instantiates a trivial `Stage` implementation and asserts it passes `isinstance(..., Stage)`. |
| AC3.7 | New test `test_all_stages_implement_protocol` — imports each of the 8 stage classes and asserts protocol conformance. |
| AC3.8 | New test `test_stage_records_state_snapshots` — runs a full turn, iterates journal records, asserts every stage operation record has non-null `state_before` and `state_after`. |
| AC3.9 | New test `test_upstream_cancel_recorded_per_stage` — triggers a mid-turn cancel, asserts the journal contains at least one `ControlSignalRecord` per stage the signal passed through, each with a different `observed_stage` field. |
| AC3.10a | CI guard: `test_session_facade_line_budget` asserts `wc -l src/easycat/session/_session.py` returns < 500 and reports progress toward the < 400 target. |
| AC3.10b | CI guard: `test_session_method_size` walks `Session` methods via AST and asserts every public method has ≤ 30 statements unless annotated with an explicit exception marker. |
| AC3.11 | New integration test `test_chained_pipeline_end_to_end` — runs one turn with the full chained stack, asserts the expected journal record sequence for all 8 stages. |
| AC3.12 | New test `test_realtime_mode_rejected` — asserts `RunContext(runtime_mode="realtime_session")` raises `ValueError`; second grep-based sub-test asserts zero matches for `RealtimeStage` or `realtime_session` in `src/easycat/stages/` and `src/easycat/session/`. |
| AC3.13 | `uv run pytest` exits 0. |
| AC3.14 | RFC + migration note include the frozen runtime-mode/config surface and before/after usage examples. |
| AC3.15 | CI job `perf-ws3-regression` runs the T1.0.5 harness on every PR touching `src/easycat/stages/` or `src/easycat/session/`, compares against `perf/baseline.json`, fails on P50 > +5% or P90 > +10%. |
| AC3.16 | New test `test_vad_decision_reproducibility` — drives `VADStage` with a committed audio fixture, captures `snapshot_state()` for every decision, calls `VADStage.replay_decision(snapshot)` on a fresh stage instance loading only the snapshot + captured audio artifact, asserts byte-identical decision outputs. Parametrized over the Silero backend (always runs) and Krisp backend (integration, gated on Krisp credentials). |
| AC3.17 | New test `test_smart_turn_decision_reproducibility` — drives `TurnStage` with a committed audio window fixture, captures `snapshot_state()` for the endpointing decision, calls `TurnStage.replay_decision(snapshot)` on a fresh stage instance, asserts the replayed classification (logits within float tolerance) and the final `complete` / `not_complete` output match the live decision exactly. |
| AC3.18 | New test `test_text_session_end_to_end` — creates a `text_session` Session, calls `session.send_text("hello")`, iterates events, inspects the journal, asserts the stage-record shape described in AC3.18 (turn + agent records present, audio stage records absent). Second sub-test asserts `send_text` raises `RuntimeError` in chained mode. |
| AC3.19 | New test `test_text_session_interruption_parity` — fires a second `send_text` during a streaming first one, asserts the `InterruptionController` emits an interrupt signal, the journal records a matching `CancellationMode`, and the bridge sees the same interruption path as voice-mode barge-in. |

## Risks and Mitigations

- **Extraction breaks subtle timing in interruption**: this is the
  single biggest risk in the workstream. Mitigation: extract
  incrementally — one field at a time for `TurnContext`, then the
  `InterruptionController` as a single atomic step, then
  `VoiceDeliveryLedger`. Run the full `tests/session/` suite after
  each extraction and commit in small increments so bisect is useful.
  Do not modify any existing barge-in test to make it pass; if a test
  fails, stop and debug.
- **Stage port regresses hot-path performance**: mitigation —
  benchmark P50 and P90 turn latency on `examples/local_chat.py`
  before any stage work. After each stage port, re-benchmark. Halt
  the workstream if regression exceeds 5% on either metric; profile
  and fix before continuing.
- **Text mode might get hacked into a realtime escape hatch**:
  mitigation — `text_session` and `chained_pipeline` are the only
  two runtime modes; any pull request that tries to reintroduce a
  realtime mode or a fused multimodal stage is rejected against
  the Explicit Guardrails in the essential plan. If a realtime
  feature is valuable, users should use the provider SDK directly.
- **Session reduction below 400 lines proves impossible without
  absorbing logic into other files**: mitigation — 400 is the target,
  500 is the hard ceiling. If the final count is 420 lines and every
  remaining line is pure orchestration, that's acceptable. The gate is
  "no domain logic left on Session", and the line count is a proxy for
  that.
- **Upstream control signal plumbing changes behavior**: mitigation —
  the signal flow is added *alongside* the existing shared-cancel-
  token path for this workstream. Do not remove the shared token
  until all tests pass with signals. Workstream 5 removes the old
  path.

## Handoff to Next Workstream

When this workstream is complete, Workstream 4 (Replay and Bundle)
inherits:

- the `Stage.replay(spec)` method stub on every stage, ready to be
  fleshed out
- journal records with `state_before`/`state_after` snapshots that
  become the replay payload
- the bridge execution cursor's `committable` flag (from Workstream
  2A's `COMMITTABLE_BOUNDARIES` mappings, validated by Workstream
  2B's drain-to-commit-point tests) wired through `AgentStage`,
  which defines valid fork boundaries
- extracted components (`TurnContext`, `VoiceDeliveryLedger`,
  `InterruptionController`) that can be reconstructed from a journal
  slice during `artifact_replay`

Workstream 5 (Legacy Removal) inherits the confirmation that Session
no longer calls the legacy observability systems directly — only
through the strangler-fig adapters from Workstream 1, which are
themselves ready to be deleted.
