# Debug-First Runtime Redesign

## Summary

This plan redesigns EasyCat around a debugger-first runtime while preserving the
main product promise:

- bring your own agent framework
- get a working voice agent quickly
- debug any failure without reverse-engineering the pipeline

The constraint is explicit: EasyCat can change internally without preserving its
current API shape, but it must continue to integrate cleanly with OpenAI Agents
SDK and PydanticAI. EasyCat should become the best voice runtime and debugger
for those frameworks, not a competing agent framework.

The redesign must also support both major voice architectures:

- chained pipelines: STT -> agent -> TTS
- realtime sessions: speech-to-speech or mixed-mode sessions where transcript
  artifacts may be partial, delayed, or optional

## Problem

Today, EasyCat has useful observability primitives, but they are split across
multiple systems:

- event logs
- tracing spans
- metrics
- adapter-specific history handling
- example-only observability UI

That makes debugging possible, but not easy. A real production debugging flow
should answer the same questions every time:

1. What happened?
2. Where did it happen?
3. What did that stage receive?
4. What did it produce?
5. Can I replay only that part?

The current architecture does not enforce those answers uniformly.

## Goals

- Make debugging a first-class runtime feature, not a set of optional helpers.
- Preserve a simple quickstart for the majority of users.
- Keep OpenAI Agents and PydanticAI as external agent frameworks.
- Support both chained and realtime voice runtime modes cleanly.
- Support deterministic replay at stage boundaries.
- Export production failures as portable debug bundles.
- Unify logs, traces, metrics, and debug artifacts under one model.

## Non-Goals

- Do not create an EasyCat-native agent framework.
- Do not create EasyCat tool decorators, planner abstractions, or prompt DSLs.
- Do not replace OpenAI Agents or PydanticAI workflow logic.
- Do not expose the full internal stage graph as the default user API.

## Product Principles

### 1. Progressive Disclosure

The internal system can become much more rigorous, but the top-level UX must
stay simple.

There should be three layers:

- `Quickstart`: one config, one session factory, auto-adapt the external agent.
- `Config`: explicit provider/runtime choices without exposing internal stages.
- `Core`: stage graph, execution journal, replay, and debug bundle APIs.

Most users should live entirely in `Quickstart`.

### 2. One Source of Truth

Logs, spans, counters, and debug views should derive from a single execution
journal for EasyCat-specific runtime truth. We should not maintain multiple
internal observability systems with different payload shapes and different
correlation rules.

This does not mean replacing standard telemetry. EasyCat should align with
OpenTelemetry for traces, metrics, and logs, while the execution journal stores
voice/runtime-specific artifacts and replay metadata that generic telemetry does
not model well.

### 3. Bring Your Own Agent

EasyCat owns the voice runtime around the agent call. It does not own the
agent's reasoning model, tool system, or workflow semantics.

### 4. Debuggability Requires Replay

A record is not enough. Every major boundary must be replayable from captured
inputs or normalized artifacts.

### 5. Normalize Conservatively

EasyCat should normalize only the cross-framework concepts needed for runtime
debugging:

- generation
- tool call
- handoff
- workflow node or specialist transition
- interruption
- state commit

Framework-specific meaning must remain attached as metadata rather than being
collapsed into a new EasyCat-native agent model.

## User-Facing API Shape

### Quickstart API

Preserve the spirit of:

- `EasyCatConfig`
- `create_session(...)`
- `auto_adapt_agent(...)`

That means the simplest user path should still look like:

```python
from easycat import EasyCatConfig, create_session
from agents import Agent

agent = Agent(name="Support", instructions="Help the user.")
session = create_session(EasyCatConfig(openai_api_key="...", agent=agent))
```

The user should not need to know that the runtime is now stage-based or
journal-backed.

### Quickstart Guardrails

The redesign should be rejected if it violates these constraints:

- the simplest OpenAI Agents or PydanticAI example stops fitting in roughly
  10-15 lines
- users must wire stages directly to get started
- debugging requires custom subscription code or a separate example app
- users must learn new EasyCat-native agent concepts before shipping

### Advanced API

Expose richer toggles through config, not low-level internals:

- `debug=True`
- `debug_mode="light" | "full"`
- `export_debug_bundle=True`
- `redaction_policy=...`
- `mode="local" | "webrtc" | "telephony"`

### Core API

Advanced users and internal code can access:

- `ExecutionJournal`
- `RunBundle`
- `ReplaySpec`
- `Stage`
- `ExternalAgentBridge`

These are power-user and implementation interfaces, not the main onboarding
surface.

## Target Architecture

### Core Runtime Types

### `RunContext`

Shared context for a single session/runtime instance:

- `run_id`
- `session_id`
- config snapshot
- runtime mode
- redaction policy
- artifact store handle
- journal handle

### `TurnContext`

Per-turn runtime state:

- `turn_id`
- turn timings
- interruption metadata
- cancel token
- playback state
- telephony state hooks

### `VoiceDeliveryLedger`

The runtime-owned record of what actually happened in the voice channel.

Tracks:

- user transcript inputs
- raw agent text
- post-processed spoken text
- playback acknowledgements
- estimated delivered assistant text at interruption time
- interruption cut points and confidence

This ledger is the source of truth for barge-in behavior. It is distinct from
the external framework's own conversation history.

### `InterruptionController`

Runtime-owned controller for voice-specific interruption policy.

Responsibilities:

- detect interruption boundaries
- determine what text was likely delivered
- choose cancellation policy
- decide whether to drain or stop in-flight work
- apply interruption updates through the bridge

### `ExecutionJournal`

Append-only structured record store. Everything emits records into this.

Responsibilities:

- record stage operations
- correlate artifacts
- index by `run_id`, `session_id`, `turn_id`, `op_id`
- export to bundle
- feed debugger UI
- feed metrics derivation
- feed trace views

The journal is the EasyCat-specific runtime record, not a replacement for
OpenTelemetry spans/logs/metrics.

### `ArtifactStore`

Stores larger payloads and sensitive blobs separately from inline records:

- audio snippets
- provider payload excerpts
- transcripts
- tool arguments/results
- serialized config snapshots
- redacted request/response payloads

### `RunBundle`

Portable export unit for debugging and regression testing.

Contains:

- journal records
- artifact index
- config snapshot
- environment metadata
- redaction metadata
- replay entry points

### `FrameworkStateSnapshot`

Bridge-produced snapshot of the external framework state at a meaningful
boundary.

Examples:

- OpenAI Agents current agent, response IDs, and local history mirror
- PydanticAI message history, deps/model settings, and workflow active node

### Stage Model

Each runtime boundary becomes a standard stage with the same debugging
contract.

```python
class Stage(Protocol):
    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any: ...
    def snapshot_state(self) -> dict[str, Any]: ...
    def replay(self, spec: ReplaySpec) -> Any: ...
```

Initial stage set:

- `TransportStage`
- `AudioStage`
- `VADStage`
- `STTStage`
- `TurnStage`
- `AgentStage`
- `TTSStage`
- `TelephonyStage`

These stages apply differently depending on runtime mode.

### Runtime Modes

The runtime should model two first-class modes:

- `chained_pipeline`
- `realtime_session`

`chained_pipeline` mode uses the classic staged path:

- transport/audio
- VAD/turning
- STT
- agent
- TTS
- playback

`realtime_session` mode may skip, fuse, or defer some of those boundaries:

- audio may flow directly into a realtime model session
- transcript artifacts may be partial, delayed, or unavailable
- speech output may not map cleanly to a discrete TTS stage
- interruption may act on a live multimodal session rather than on queued text

The debugger and journal must support both modes without forcing realtime
sessions into a transcript-first abstraction.

Every stage writes the same conceptual record shape:

- `stage`
- `operation`
- `input_ref`
- `output_ref`
- `state_before`
- `state_after`
- `timing`
- `metrics`
- `status`
- `error`

This uniformity is what makes debugging and replay coherent.

## Agent Compatibility Boundary

### Decision

Replace the current runner-centric adapter flow with a bridge-centric model:

- EasyCat owns the voice runtime
- OpenAI Agents and PydanticAI own agent behavior
- EasyCat bridges their native events into journal records

### `ExternalAgentBridge`

```python
class ExternalAgentBridge(Protocol):
    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]: ...

    def snapshot_state(self) -> dict[str, Any]: ...
    def apply_interruption(self, delivered_text: str, mode: str) -> None: ...
    def reset(self) -> None: ...
```

The bridge does not define tools, prompts, memory models, or routing rules. It
only translates framework-native runs into EasyCat runtime records.

### Voice State vs Framework State

This split is mandatory for voice correctness.

EasyCat owns:

- the voice delivery ledger
- playback-aware interruption decisions
- cancellation timing
- turn lifecycle
- TTS/STT/telephony coordination

The bridge owns:

- framework-native history representation
- framework-native interruption patching
- framework-native cancellation mapping
- framework-native state snapshots

This avoids pretending there is one generic conversation history model across
OpenAI Agents and PydanticAI while still allowing shared voice behavior.

It also avoids coupling EasyCat's voice model to a single runtime architecture.
In realtime sessions, framework state may be richer than transcript state and
must still be representable even when STT/TTS boundaries are soft or absent.

### Interruption and Cancellation Contract

The runtime decides when interruption happens. The bridge decides how that
interruption is represented inside the external framework.

Turn flow:

1. runtime detects interruption
2. runtime computes delivered assistant text from the voice delivery ledger
3. runtime selects a cancellation boundary
4. runtime requests bridge cancellation/drain behavior
5. runtime calls `apply_interruption(delivered_text, mode=...)`
6. bridge updates framework-native state
7. journal records both the voice event and framework-state mutation

Cancellation should support three boundary modes:

- `immediate_stop`: stop streaming now and discard future non-essential events
- `drain_current_unit`: allow the current tool call or framework node to finish
- `drain_to_commit_point`: finish until the next safe framework state boundary,
  then stop before entering the next unit

The bridge must expose enough execution-state information for the runtime to
choose among those policies safely.

### Bridge Execution Cursor

The bridge should maintain a typed cursor describing the currently active
framework execution unit.

Suggested fields:

- `unit_id`
- `unit_kind`: `agent | specialist | workflow_node | model_node | tool_call`
- `display_name`
- `parent_unit_id`
- `sequence`
- `entered_at`
- `committable`: whether state can be safely snapshotted here

This lets the journal and debugger show transitions inside a single user turn
without EasyCat inventing its own agent semantics.

### Transition Records

Agent and workflow transitions must be first-class journal records, not hidden
inside generic text/tool events.

Suggested transition record types:

- `FrameworkUnitEntered`
- `FrameworkUnitExited`
- `FrameworkStateCommitted`
- `FrameworkHandoff`
- `FrameworkToolPhaseChanged`
- `FrameworkCancellationBoundaryReached`

Each should include:

- `from_unit`
- `to_unit`
- `transition_kind`
- `reason`
- `framework_metadata`
- `state_snapshot_ref`

The important distinction is:

- a handoff changes who now owns the next model step
- a node transition changes the current execution substep within one framework
- a tool phase transition changes what work is in-flight but not who is in
  control

The debugger should visualize those differently.

Normalization should stop there. Any deeper framework semantics should stay in
framework-specific metadata rather than becoming new EasyCat abstractions.

### OpenAI Agents Bridge

Capture when available:

- rendered instructions
- model settings and run config
- response IDs and `previous_response_id`
- tool start/delta/result events
- handoff transitions
- framework-managed history snapshots
- provider request IDs and error payloads

Special handling:

- treat `last_agent` changes as committed handoffs
- record response-chain continuity separately from agent transitions
- support server-managed history cases where spoken-text patches must be
  represented as deferred notes rather than direct mutation
- distinguish text streaming cancellation from tool-call drain behavior

### PydanticAI Bridge

Capture when available:

- input text and message history passed to the framework
- `deps` and `model_settings`
- streaming node events from `iter()`
- tool call/result events
- final output object
- `new_messages()` history updates

Special handling:

- treat `iter()` node changes as framework unit transitions
- distinguish model-request nodes from tool-call nodes for cancellation safety
- record workflow-managed specialist/node state separately from raw message
  history
- allow workflow adapters to report committed `active_agent_id` or node changes
  as explicit transition records

### Workflow Support

PydanticAI workflow objects remain valid, but EasyCat treats them as external
workflow code wrapped by a compatibility bridge. EasyCat records workflow state
transitions; it does not define workflow semantics itself.

Workflow transition support should include:

- current specialist or node ID
- previous specialist or node ID
- transition reason if available
- private-history ownership remaining inside workflow code
- commit points where the workflow considers the new active node authoritative

## Debugger Product Surface

### Default Behavior

Debugging should be on by default in a lightweight mode:

- IDs and journal records always exist
- small ring buffers are always available
- full artifact capture is opt-in or environment-dependent

That preserves debuggability without overwhelming quickstart users.

### Built-In Debugger

Ship a first-class debugger instead of leaving observability in examples.

Capabilities:

- live timeline by turn
- stage-by-stage drill-down
- raw vs normalized payload view
- latency visualization
- tool and handoff inspection
- framework unit transition timeline
- interruption visualization
- replay from selected boundary

This can initially grow out of the current observability demo, but it should be
driven by the journal, not ad hoc event subscriptions.

### Debug Bundle Export

Production issues should be exportable as a self-contained bundle:

- `session.export_debug_bundle(...)`
- optional redaction
- optional inline artifacts
- stable schema for replay and tests

### Redaction

Redaction is part of the design, not an afterthought.

Define a configurable policy that controls:

- transcript text capture
- audio retention
- tool argument/result retention
- provider payload retention
- environment metadata exposure

## Runtime Changes

### Replace Multiple Observability Systems With One Journal

The current event logging, trace exporter, and metrics collector should become
derived views over journal records where possible.

The journal becomes the primary EasyCat runtime substrate.

Effects:

- logging becomes a journal formatter
- tracing becomes a journal-to-OTel projection where applicable
- metrics become journal-derived aggregations and/or OTel metrics
- debugger UI becomes a journal reader

### OpenTelemetry Alignment

EasyCat should not invent a private alternative to standard telemetry when a
standard mapping exists.

Recommended split:

- OpenTelemetry for standard traces, logs, and metrics
- execution journal for voice-specific artifacts, playback truth, replay
  handles, redaction-aware payload snapshots, and framework transition records

Benefits:

- easier ecosystem integration
- cleaner compatibility with Pydantic Logfire and other OTel backends
- less duplicate telemetry logic
- cleaner export to vendor tooling when users already have an observability
  backend

### Rebuild Session Internals Around Stages

The current session package should be restructured so stage execution is the
main orchestration model. `Session` can remain as a façade, but it should stop
being the place where observability logic is embedded directly.

### Keep Quickstart as a Facade

`EasyCatConfig + create_session(...)` should remain the top-level path. It
should build:

- a stage graph
- the external agent bridge
- the journal
- the artifact store
- debug presets

without exposing those concepts unless the user opts in.

The façade must work for both runtime modes:

- explicit provider-based chaining
- realtime-capable sessions when a backing provider/framework supports them

### File/Module Direction

Proposed package layout:

```text
src/easycat/runtime/
  app.py
  context.py
  journal.py
  records.py
  artifacts.py
  replay.py
  redaction.py

src/easycat/stages/
  transport.py
  audio.py
  vad.py
  stt.py
  turn.py
  agent.py
  tts.py
  telephony.py

src/easycat/integrations/agents/
  base.py
  openai_agents.py
  pydantic_ai.py
  workflows.py

src/easycat/debug/
  bundle.py
  export.py
  server.py
  ui/
```

Likely rewrite targets:

- `src/easycat/session/`
- `src/easycat/agent_runner.py`
- `src/easycat/event_logging.py`
- `src/easycat/tracing.py`

Likely retained conceptually but rewritten around the new bridge boundary:

- `src/easycat/agents/openai_agents.py`
- `src/easycat/agents/pydantic_ai.py`
- `src/easycat/agents/pydantic_ai_workflow.py`
- `src/easycat/agents/factory.py`

## Phased Implementation Plan

### Phase 0: Architecture Freeze

- agree on the boundary: runtime/debugger, not agent framework
- agree on quickstart as a hard API requirement
- agree on the journal schema and bundle schema

Deliverable:

- approved design doc

### Phase 1: Execution Journal Foundation

- implement `ExecutionJournal`
- implement `ArtifactStore`
- define stable record types
- build adapters that let existing logs/spans/metrics write through the journal
- define OTel mapping points instead of replacing telemetry end-to-end

Deliverable:

- journal-backed observability with minimal runtime behavior changes

### Phase 2: Agent Bridge Layer

- implement `ExternalAgentBridge`
- port OpenAI Agents integration
- port PydanticAI integration
- port workflow wrapper support
- define interruption/cancellation boundary semantics
- define framework transition records and execution cursor model
- keep transition normalization shallow and attach framework-specific metadata

Deliverable:

- framework compatibility preserved with richer structured recording
- committed handoffs and node transitions visible in the journal

### Phase 3: Replay Taxonomy + Bundle Export

- define replay classes explicitly:
  - `artifact_replay`
  - `simulated_replay`
  - `live_reexecution`
- define which stages and runtimes support which replay classes
- export `RunBundle`
- support replay entry points with clear guarantees and caveats
- add pytest fixtures that load bundles as repro cases

Deliverable:

- production failures become local repro artifacts with honest replay semantics

### Phase 4: Evaluation and Regression Surface

- expose journal-based assertions and fixtures for pytest
- support behavioral assertions over transitions, tool usage, interruptions,
  and latency
- align eval output with OTel traces where available
- ensure failures can be promoted from debug bundles into regression tests

Deliverable:

- evals arrive early enough to shape runtime behavior instead of landing after
  the major refactor

### Phase 5: Stage Refactor

- introduce stage interfaces
- port STT, agent, and TTS first
- then port transport, VAD, turn logic, and telephony
- support both `chained_pipeline` and `realtime_session` mode without forcing
  identical stage boundaries

Deliverable:

- coherent stage-based runtime with replayable boundaries

### Phase 6: Built-In Debugger

- replace example-only observability flow with a first-class debugger server/UI
- render journal timelines and artifacts
- support replay from UI

Deliverable:

- one-command live debugging experience

### Phase 7: API Simplification

- tighten `EasyCatConfig`
- add runtime/debug presets
- preserve or improve quickstart ergonomics
- document the advanced/core APIs separately

Deliverable:

- simpler getting-started story on top of stronger internals

### Phase 8: Legacy Removal

- remove obsolete event/tracing plumbing
- collapse compatibility shims
- remove duplicated state handling paths

Deliverable:

- cleaner codebase with one debugging model

## Success Criteria

The redesign is successful if all of these are true:

- a minimal OpenAI Agents or PydanticAI example is still easy to write
- both chained and realtime runtime modes are supported without conceptual
  distortion
- every turn has a stable journal trail
- every major failure can be tied to one stage with concrete inputs/outputs
- replay semantics are explicit about what is deterministic vs what is
  re-executed
- production failures can be exported and replayed locally
- the debugger UI is powered by the same underlying records as logs and metrics
- EasyCat still clearly presents itself as a runtime around external agents, not
  a new agent framework
- EasyCat telemetry can integrate cleanly with OpenTelemetry-compatible systems

## Explicit Guardrails

To avoid scope drift, the following are out of bounds for this redesign:

- EasyCat-native tool API
- EasyCat-native planner/router
- EasyCat-native memory layer
- EasyCat-native prompt compiler
- EasyCat-native multi-agent abstraction beyond compatibility bridges

If a feature would require owning agent semantics instead of recording or
transporting them, it should stay outside EasyCat.

If a feature requires EasyCat to define a deeper cross-framework ontology for
agent behavior than is needed for debugging or replay, it should also stay out
of scope.

## Immediate Next Step

Write an implementation RFC for Phase 1 and Phase 2 with:

- proposed journal record classes
- proposed artifact store interface
- proposed bridge interfaces
- exact compatibility expectations for OpenAI Agents and PydanticAI
