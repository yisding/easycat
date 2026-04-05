# Workstream 3: Stage Refactor and Session Decomposition

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstream 1 (Journal Foundation) and Workstream 2
> (Agent Bridge) must both be complete.
> **Successors**: Workstream 4 (Replay and Bundle) depends on the
> `Stage.replay()` hook introduced here.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2-agent-bridge.md`
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
- Support both `chained_pipeline` and `realtime_session` modes

**Out of scope:**

- `replay` hook implementation details beyond the stubbed protocol
  method (Workstream 4 fleshes it out per stage)
- `WarmupStage` (peripheral — in `peripheral-observability-and-cost.md`)
- Removal of Session instance-variable fields that are now duplicated
  (Workstream 5)

## Tasks

### T3.0: Architecture Freeze (RFC)

- [ ] Write Phase 3 RFC covering:
  - extraction order (`TurnContext` → `InterruptionController` →
    `VoiceDeliveryLedger` → stages)
  - `Stage` protocol signature and `ControlSignal` types
  - `RunContext` field list
  - public runtime-mode/config surface for `chained_pipeline` vs
    `realtime_session`
  - `chained_pipeline` vs `realtime_session` mode wiring differences
  - rollback strategy if a stage port regresses behavior
- [ ] Review and merge before any Session changes.

### Step 1 — Extract from Session

### T3.1: Extend TurnContext

- [ ] Current `src/easycat/session/_turn_context.py` is 64 lines —
  extend it to hold per-turn state currently scattered across
  `_session.py` instance variables:
  - `_agent_response_parts`
  - `_tts_chunks`
  - `_playback_mark_to_bytes`
  - turn timings
  - interruption metadata
  - cancel token
  - playback state
  - telephony state hooks
- [ ] Session delegates to `TurnContext` via a single
  `self._current_turn: TurnContext` attribute
- [ ] Verify all Session tests pass after each field is extracted
  (extract one at a time, run tests, commit, repeat)

### T3.2: Extract InterruptionController

- [ ] Create `src/easycat/session/_interruption_controller.py`
- [ ] Move interruption detection, delivered-text computation, policy
  selection, and bridge interaction out of `_session.py`
- [ ] The controller owns the seven-step interruption flow defined in
  Workstream 2
- [ ] Session delegates to the controller
- [ ] All barge-in tests in `tests/session/` pass unmodified

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
  snapshot, runtime mode (`chained_pipeline` | `realtime_session`),
  redaction policy, artifact store handle, journal handle
- [ ] `RunContext` stores a redacted/safe config snapshot, not a raw
  `EasyCatConfig.__dict__`
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
- [ ] Stub `ReplaySpec` (filled out in Workstream 4)
- [ ] Helpers in `base.py` for journal record emission from stage
  operations

### T3.6: Port STT, Agent, TTS (highest debugging value)

- [ ] Create `src/easycat/stages/stt.py` — wrap existing STT provider
  calls, emit `state_before`/`state_after` via `snapshot_state()`,
  write journal records
- [ ] Create `src/easycat/stages/agent.py` — wrap the
  `ExternalAgentBridge` from Workstream 2
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

### T3.8: Upstream Control Signals

- [ ] Each stage implements `handle_upstream(signal)` — at minimum,
  records the signal in its own journal records so we can see which
  stage observed the signal and in what state
- [ ] Plumb control signal flow through the session: upstream signals
  walk from late stages (TTS, Transport) back toward early stages
  (VAD, STT) rather than reading a shared cancel token
- [ ] Verify interruption tests still pass with the new signal flow

### Step 3 — Wire journal and reduce Session

### T3.9: Journal Wiring

- [ ] Each stage writes records through the journal via helpers in
  `stages/base.py`
- [ ] Remove direct observability calls from `_session.py` (the
  strangler-fig adapters from Workstream 1 still cover legacy call
  sites; this step removes the direct journal writes that Session was
  doing)
- [ ] Every stage operation produces at minimum a `start` and
  `complete` (or `error`/`cancel`) record pair with non-null
  `state_before` and `state_after` snapshots

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
- [ ] In `realtime_session` mode, stage boundaries are soft:
  `STTStage`, `TurnStage`, `TTSStage` may become no-op or fused when
  the realtime provider handles them internally
- [ ] Stages gracefully accept partial/deferred transcript artifacts
  in realtime mode
- [ ] Journal records are still emitted for realtime mode's fused
  operations, just with different `stage` and `operation` values

## Acceptance Criteria

- [ ] **AC3.1** RFC reviewed and merged.
- [ ] **AC3.2** `TurnContext` holds all per-turn state.
  Introspection of `Session` instance after a turn shows zero
  leftover private attributes matching `_agent_response_*`,
  `_tts_*`, `_playback_*`, `_turn_*` patterns.
- [ ] **AC3.3** `InterruptionController` exists in
  `src/easycat/session/_interruption_controller.py` and owns all
  interruption logic.
- [ ] **AC3.4** `VoiceDeliveryLedger` exists in
  `src/easycat/session/_voice_delivery_ledger.py` and is the single
  source of truth for voice channel delivery state.
- [ ] **AC3.5** `RunContext` exists in `src/easycat/runtime/context.py`
  with all required fields, including a safe/redacted config snapshot.
- [ ] **AC3.6** `Stage` protocol exists in `src/easycat/stages/base.py`
  with all four methods and typed `StageStateSnapshot`.
- [ ] **AC3.7** All 8 stages exist as files under `src/easycat/stages/`
  and each implements the `Stage` protocol.
- [ ] **AC3.8** Every stage writes journal records with non-null
  `state_before` and `state_after` on every invocation.
- [ ] **AC3.9** Upstream control signals (`Interrupt`, `Cancel`,
  `Pause`, `Resume`, `Backpressure`) are recorded per-stage in the
  journal. Each stage that observes a signal writes its own record.
- [ ] **AC3.10** `src/easycat/session/_session.py` is a thin facade and
  stays under the 500-line hard ceiling; `< 400` remains the target.
- [ ] **AC3.11** `chained_pipeline` mode works end-to-end with OpenAI
  Agents + Deepgram STT + ElevenLabs TTS.
- [ ] **AC3.12** `realtime_session` mode works end-to-end with OpenAI
  Realtime API (at minimum; Gemini Live is peripheral).
- [ ] **AC3.13** All existing tests pass.
- [ ] **AC3.14** Any public runtime-mode/config changes introduced here
  are frozen in the RFC and covered by migration notes with before/after
  examples.

## Verification

| AC | Verification |
|---|---|
| AC3.1 | Git log shows RFC merge commit. |
| AC3.2 | New test `test_no_per_turn_state_on_session` — after completing a turn, inspects `session.__dict__` and asserts zero entries matching the private-attribute patterns. |
| AC3.3 | `python -c "from easycat.session._interruption_controller import InterruptionController"` exits 0; `grep -rn 'InterruptionController(' src/easycat/session/_session.py` shows delegation. |
| AC3.4 | Same as AC3.3 for `VoiceDeliveryLedger`. |
| AC3.5 | `python -c "from easycat.runtime.context import RunContext"` exits 0; new test asserts required fields are present. |
| AC3.6 | `python -c "from easycat.stages.base import Stage"` exits 0; new test instantiates a trivial `Stage` implementation and asserts it passes `isinstance(..., Stage)`. |
| AC3.7 | New test `test_all_stages_implement_protocol` — imports each of the 8 stage classes and asserts protocol conformance. |
| AC3.8 | New test `test_stage_records_state_snapshots` — runs a full turn, iterates journal records, asserts every stage operation record has non-null `state_before` and `state_after`. |
| AC3.9 | New test `test_upstream_cancel_recorded_per_stage` — triggers a mid-turn cancel, asserts the journal contains at least one control-signal record per stage the signal passed through, each with a different `stage` field. |
| AC3.10 | CI guard: `test_session_facade_line_budget` asserts `wc -l src/easycat/session/_session.py` returns < 500 and reports progress toward the < 400 target. |
| AC3.11 | New integration test `test_chained_pipeline_end_to_end` — runs one turn with the full chained stack, asserts the expected journal record sequence for all 8 stages. |
| AC3.12 | New integration test `test_realtime_session_end_to_end` — runs one turn against OpenAI Realtime API (gated on `OPENAI_API_KEY`; skipped in CI without creds but required locally), asserts journal contains realtime-mode records. |
| AC3.13 | `uv run pytest` exits 0. |
| AC3.14 | RFC + migration note include the frozen runtime-mode/config surface and before/after usage examples. |

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
- **`realtime_session` mode forces awkward abstractions**:
  mitigation — the `Stage` protocol must allow no-op stages. Realtime
  mode does not have to fit every stage boundary. Document which
  stages are active per mode.
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
- the bridge execution cursor's `committable` flag (from Workstream 2)
  wired through `AgentStage`, which defines valid fork boundaries
- extracted components (`TurnContext`, `VoiceDeliveryLedger`,
  `InterruptionController`) that can be reconstructed from a journal
  slice during `artifact_replay`

Workstream 5 (Legacy Removal) inherits the confirmation that Session
no longer calls the legacy observability systems directly — only
through the strangler-fig adapters from Workstream 1, which are
themselves ready to be deleted.
