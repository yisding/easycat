# Debug-First Runtime Redesign — Essential Plan

> **This is the load-bearing plan.** Everything in this file is required
> to deliver the debug-first thesis. If an item is not here, it is not
> essential to that thesis — it lives in one of the four peripheral
> follow-up files, which capture valuable but separable work.
>
> **In scope (essential):** execution journal, artifact store, external
> agent bridge, Session decomposition, stage model, replay, debug
> bundle export, redaction, MCP pass-through (as a bridge correctness
> test).
>
> **Peripheral follow-up files** (each is a sibling initiative, not a
> dependency of this plan):
>
> - `peripheral-dx-onboarding.md` — line budgets, `easycat.run()` /
>   `async with session`, string-keyed providers, env autodetect,
>   `easycat` CLI (`init`, `doctor`, `run`, `dev`, `explain`, `cost`,
>   `test`, `bundle export`), templates, config factory presets, offline
>   preset, error diagnostics, `EasyCatConfig` flattening.
> - `peripheral-provider-ecosystem.md` — Deepgram Flux STT adapter,
>   Gemini 3.1 Flash Live bridge, Smart Turn v3.1 promotion (Pipecat
>   wrapper), backchannel filter, cache-friendly realtime defaults
>   (`retention_ratio=0.8`, `CacheBust`).
> - `peripheral-observability-and-cost.md` — `CostRecord` with pricing
>   source, budget alerts, `JournalToOTelExporter` with `gen_ai.*`
>   semconv, Latency Budget targets, `WarmupStage`.
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`,
>   persona-driven Simulator + Judge, simulation-first mode,
>   `forked_replay` fidelity class, LangGraph checkpoint vocabulary,
>   interactive web debugger UI, dev waterfall terminal output.

## Summary

Redesign EasyCat's runtime around a single execution journal so every voice
agent failure can be answered with the same five questions:

1. What happened?
2. Where did it happen?
3. What did that stage receive?
4. What did it produce?
5. Can I replay only that part?

Today those answers are split across `EventTraceLogger`, `Tracer`/`Span`, and
`InMemoryMetrics`, each with different payload shapes and correlation rules.
`Session` is a 1,500-line monolith that embeds orchestration, observability,
and interruption logic. Adapter-specific history handling hides framework
execution state (handoffs, tool calls, node transitions) from observability.
A production debug flow has to reverse-engineer the pipeline.

This plan replaces those three systems with one journal, decomposes Session
into stage + context + controller types, and makes the adapter layer an
explicit bridge that exposes framework execution state as structured records.

## Constraints

- EasyCat can change its internal API shape.
- Backwards compatibility with the current public config/import surface is
  not a goal of this redesign. The plan may change `EasyCatConfig`,
  top-level exports, and agent/debug entry points when that reduces
  complexity, provided each breaking change ships with migration notes,
  before/after examples, and release-note coverage.
- EasyCat must continue to wrap OpenAI Agents SDK and PydanticAI cleanly
  without owning agent semantics.
- The runtime must support both chained pipelines (STT → agent → TTS) and
  realtime sessions (speech-to-speech, multimodal) without forcing either
  into the other's shape.
- Debuggability is on by default in a lightweight mode; full capture is
  opt-in.

## Why Debug-First Is the Bet

Self-hosted, framework-agnostic voice debugging does not exist today.
LiveKit's Agent Observability requires LiveKit Cloud. Pipecat ships nothing
comparable. Vocode, Bland, Vapi, and Retell all optimize for time-to-first-
call at the cost of debugging depth. The debug-first runtime is EasyCat's
single biggest differentiator opportunity, and it is a pure software bet: no
provider partnerships, no hosted backend, no proprietary model.

Everything that is *not* debug-first — CLI ergonomics, provider additions,
eval harness, onboarding budgets, OTel export, realtime cost tuning — is
valuable but separable. Those live in `runtime-followups.md` so this plan
can stay focused.

## Non-Goals

Out of scope for this plan (and some also out of scope for EasyCat entirely):

- New providers (Deepgram Flux, Gemini Live, Kyutai, etc.)
- CLI tooling (`easycat init`, `doctor`, `explain`, `cost`, `dev --reload`)
- Line-count budget enforcement on examples
- `run()`, `async with session`, string-keyed provider selection, env autodetect
- OTel export
- `easycat.testing` with Simulator + Judge
- Interactive web debugger UI
- `--for=claude-code` bundle export
- Forked replay / time-travel
- Realtime cache-friendly defaults
- Latency budget CI enforcement, warmup stage
- Smart Turn v3.1 promotion, backchannel filter
- Offline preset, template ecosystem

Each depends on the journal or bridge landing first. They are not competing
with this plan; they are downstream of it. See `runtime-followups.md`.

Also permanently out of bounds for EasyCat (guardrails at the bottom of this
doc): EasyCat-native tool API, EasyCat-native MCP client or tool registry,
EasyCat-native planner/router, EasyCat-native memory or prompt compiler,
EasyCat-native multi-agent abstraction beyond compatibility bridges, hosted
observability backend.

Preserving the current public surface verbatim is also out of scope. This
plan optimizes for a coherent runtime and coherent debugging model first;
compatibility shims are optional and must justify their maintenance cost.

## Principles

### One Source of Truth

Logs, spans, counters, and debug views derive from one execution journal. No
parallel observability systems with different payload shapes. The journal is
the debugging system; it is not a telemetry pipeline.

### Bring Your Own Agent

EasyCat owns the voice runtime around the agent call. OpenAI Agents and
PydanticAI own reasoning, tools, and workflow semantics. The bridge
translates framework-native events into journal records. It does not define
an EasyCat-native agent model.

### Debuggability Requires Replay

A record is not enough. Every major boundary must be replayable from
captured inputs or normalized artifacts.

### Normalize Conservatively

EasyCat normalizes only the cross-framework concepts needed for runtime
debugging: generation, tool call, handoff, workflow node or specialist
transition, interruption, state commit. Framework-specific meaning stays
attached as metadata rather than being collapsed into a new EasyCat-native
agent model.

### Progressive Disclosure

Quickstart users never see journal, stage, or bridge concepts. Advanced
users reach them through explicit debug APIs such as `session.journal` and
bundle export/load, never through required config. This plan is allowed to
change the public config and import surface where that reduces complexity;
the requirement is clear migration documentation, not signature stability.
Any extra ergonomic polish beyond the core debug surface belongs in the
follow-up plan so this one stays focused on runtime correctness.

## Core Runtime Types

The plan introduces the following types incrementally. Most are internal
plumbing; the exceptions are the read-only journal/debug surfaces that
advanced users need in order to replace the legacy observability APIs.

### `RunContext`

Shared context for a session/runtime instance:

- `run_id`
- `session_id`
- redacted config snapshot
- runtime mode
- redaction policy
- artifact store handle
- journal handle

### `TurnContext` (extended)

Per-turn runtime state extracted from Session instance variables:

- `turn_id`
- turn timings
- interruption metadata
- cancel token
- playback state
- telephony state hooks

Currently much per-turn state lives on Session as instance variables
(`_agent_response_parts`, `_tts_chunks`, `_playback_mark_to_bytes`). These
must migrate into `TurnContext` so state can be snapshotted at stage
boundaries.

### `VoiceDeliveryLedger`

The runtime-owned record of what actually happened in the voice channel:

- user transcript inputs
- raw agent text
- post-processed spoken text
- playback acknowledgements
- estimated delivered assistant text at interruption time
- interruption cut points and confidence

This ledger is the source of truth for barge-in behavior. It is distinct
from any framework conversation history.

### `InterruptionController`

Runtime-owned controller for voice-specific interruption policy:

- detect interruption boundaries
- determine what text was likely delivered
- choose cancellation policy
- decide whether to drain or stop in-flight work
- apply interruption updates through the bridge

### `ExecutionJournal`

Append-only structured record store. Informed by Temporal's Event History
and Restate's operation-level journaling.

Responsibilities:

- record stage operations
- correlate artifacts by `run_id`, `session_id`, `turn_id`, `op_id`
- index with a monotonic sequence number per session (not just `op_id`)
  for deterministic ordering during replay
- store large payloads via indirection (`input_ref`, `output_ref` pointing
  into `ArtifactStore`, not inline blobs)
- synchronous write guarantee: journal writes complete before stage output
  is forwarded, so the journal is never behind reality (Restate principle)
- crash-durability with a durable backend: if the Python process segfaults
  or is OOM-killed mid-turn, the partial journal must be loadable
  afterward and exportable as a bundle. Voice sessions crash in the field
  (telephony disconnects, mic drivers, audio buffer underruns) and the
  crash itself is often the bug worth debugging. In-memory backends waive
  this and must log a single startup line making the tradeoff explicit.
- configurable backend: in-memory ring buffer default for dev, SQLite or
  file-based for production persistence

### `JournalView`

Read-only public journal surface exposed on `Session` as `session.journal`.

Responsibilities:

- iterate or slice records without exposing append/mutation methods
- resolve artifact references through the artifact store
- support the migration path from `EventTraceLogger` subscriptions to
  journal reads
- remain stable enough to support bundle export and offline regression
  tests

### `ArtifactStore`

Stores larger payloads and sensitive blobs separately from inline records:

- audio snippets
- provider payload excerpts
- transcripts
- tool arguments and results
- serialized config snapshots
- redacted request/response payloads

No artifact entry may contain raw secrets that were not already permitted by
the redaction policy. Large or sensitive snapshots are stored by reference;
small inline fields remain JSON-safe and secret-safe.

### `SafeConfigSnapshot` and `SafeEnvironmentSnapshot`

Typed, allowlisted snapshots used by the journal and bundle exporter.

Rules:

- never serialize raw `EasyCatConfig.__dict__`
- never serialize raw environment variables wholesale
- include only fields explicitly marked safe for persistence
- hash, redact, or drop secrets such as API keys, bearer tokens, and
  auth headers
- encode large values or provider payloads via artifact refs rather than
  arbitrary nested objects

### `RunBundle`

Portable export unit for debugging and regression testing.

Contains:

- journal records
- artifact index with checksums (SHA-256 manifest for integrity verification)
- safe config snapshot
- allowlisted environment metadata
- provider version strings (Deepgram API version, ElevenLabs model ID, etc.)
  — voice provider behavior changes silently and this is critical for
  understanding why a replay diverges from production
- redaction metadata
- replay entry points
- bundle format version for forward compatibility

### `FrameworkStateSnapshot`

Bridge-produced snapshot of the external framework state at a committable
boundary.

Requirements:

- JSON-serializable and stable across bundle export/load
- secret-safe by construction; no raw credentials or auth material
- no raw framework objects or live handles
- large/sensitive payloads stored via artifact refs

Examples:

- OpenAI Agents: current agent, response IDs, local history mirror
- PydanticAI: message history, deps/model settings, workflow active node

### `StageStateSnapshot`

Stage-produced snapshot of runtime state immediately before or after a stage
operation.

Requirements:

- JSON-serializable and stable across bundle export/load
- secret-safe by construction
- no raw provider client objects, transports, sockets, or cancellation
  primitives
- large payloads referenced indirectly through the artifact store

### `Stage` Protocol

```python
class Stage(Protocol):
    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any: ...
    def snapshot_state(self) -> StageStateSnapshot: ...
    def replay(self, spec: ReplaySpec) -> Any: ...
    async def handle_upstream(self, signal: ControlSignal) -> None: ...
```

`handle_upstream` enables bidirectional flow (Pipecat pattern). Control
signals — interruption, backpressure, cancel, pause — flow upstream through
stages, not just downstream data. Without this, every stage reads a shared
cancel token and the journal cannot record *which stage* observed a signal
in *what state*. With explicit upstream signals, each stage's reaction to
cancellation becomes a first-class journal event.

Initial stage set:

- `TransportStage`
- `AudioStage` (noise reduction + echo cancellation)
- `VADStage`
- `STTStage`
- `TurnStage`
- `AgentStage`
- `TTSStage`
- `TelephonyStage`

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
- `sequence_number`

This uniformity is what makes debugging and replay coherent.

### Runtime Modes

Two first-class modes. The journal and bridge must support both without
forcing realtime sessions into a transcript-first abstraction.

`chained_pipeline`:

- transport → audio → VAD → STT → agent → TTS → transport
- discrete, well-defined stage boundaries

`realtime_session`:

- transport → realtime model → transport
- transcript artifacts may be partial, delayed, or absent
- speech output may not map cleanly to a discrete TTS stage
- interruption acts on a live multimodal session, not a queued TTS buffer
- tool calling happens mid-conversation while the audio stream stays open
- some stage boundaries are fused or absent

## Agent Compatibility Boundary

Replace the current runner-centric adapter flow with a bridge-centric model:

- EasyCat owns the voice runtime
- OpenAI Agents and PydanticAI own agent behavior
- EasyCat bridges their native events into journal records

Currently, adapters inherit from `BaseAgentAdapter` and implement the
`StreamingAgent` protocol. They are tightly coupled to `AgentRunner`
expectations. The bridge model makes the boundary explicit and gives the
runtime structured access to framework execution state (handoffs, tool
calls, node transitions) as first-class observability records.

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

The bridge does not define tools, prompts, memory models, or routing rules.
It only translates framework-native runs into EasyCat runtime records.

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

This avoids pretending there is one generic conversation history across
OpenAI Agents and PydanticAI while still allowing shared voice behavior. It
also avoids coupling the voice model to a single runtime architecture — in
realtime sessions, framework state may be richer than transcript state and
must still be representable when STT/TTS boundaries are soft or absent.

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
7. journal records both the voice event and the framework-state mutation

Cancellation supports three boundary modes:

- `immediate_stop`: stop streaming now and discard future non-essential events
- `drain_current_unit`: allow the current tool call or framework node to finish
- `drain_to_commit_point`: finish until the next safe framework state boundary,
  then stop before entering the next unit

The bridge must expose enough execution-state information for the runtime to
choose among those policies safely.

### Bridge Execution Cursor

The bridge maintains a typed cursor describing the active framework
execution unit:

- `unit_id`
- `unit_kind`: `agent | specialist | workflow_node | model_node | tool_call`
- `display_name`
- `parent_unit_id`
- `sequence`
- `entered_at`
- `committable`: whether state can be safely snapshotted here

This lets the journal and debugger show transitions inside a single user
turn without EasyCat inventing its own agent semantics.

### Transition Records

Agent and workflow transitions are first-class journal records, not hidden
inside generic text/tool events:

- `FrameworkUnitEntered`
- `FrameworkUnitExited`
- `FrameworkStateCommitted`
- `FrameworkHandoff`
- `FrameworkToolPhaseChanged`
- `FrameworkCancellationBoundaryReached`

Each includes `from_unit`, `to_unit`, `transition_kind`, `reason`,
`framework_metadata`, `state_snapshot_ref`.

The important distinction:

- a **handoff** changes who owns the next model step
- a **node transition** changes the current execution substep within one framework
- a **tool phase transition** changes what work is in-flight but not who is in control

Normalization stops there. Deeper framework semantics stay in
framework-specific metadata.

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
- record workflow-managed specialist/node state separately from raw message history
- allow workflow adapters to report committed `active_agent_id` or node
  changes as explicit transition records

### Workflow Support

PydanticAI workflow objects remain valid. EasyCat treats them as external
workflow code wrapped by a compatibility bridge. EasyCat records workflow
state transitions; it does not define workflow semantics.

Workflow transition support covers:

- current specialist or node ID
- previous specialist or node ID
- transition reason if available
- private-history ownership remaining inside workflow code
- commit points where the workflow considers the new active node authoritative

### MCP Pass-Through

MCP pass-through is part of this plan — not because adding an external tool
standard is a debugging feature, but because it is the load-bearing
correctness test for the bridge boundary. If the bridge cannot forward MCP
server registration through to the underlying framework cleanly, the bridge
design is wrong and must be fixed before moving on.

```python
EasyCatConfig(agent=..., mcp_servers=[...])
```

Semantics:

- `mcp_servers` is a list of MCP URIs (stdio, SSE, HTTP)
- the bridge registers them with the underlying framework's MCP client at
  session construction
- connection lifecycle, auth, and tool discovery all live in the framework;
  EasyCat does not proxy calls
- MCP tool invocations appear in the journal through the bridge's existing
  tool-call events — no new record type is needed
- the OpenAI Agents bridge forwards to `Agent(mcp_servers=...)`; the
  PydanticAI bridge forwards to the agent's MCP toolset adapter

**Guardrail**: no EasyCat-native tool abstraction, registry, or decorator.
If the bridge cannot forward a tool cleanly, that is a bridge bug, not a
reason to grow EasyCat's surface.

## Debugger Surface (Core Only)

The interactive web debugger, dev waterfall, `--for=claude-code` export,
and live CLI all live in the follow-up plan. What this plan ships is the
data-plane surface they all build on.

### Default Behavior

Debugging is on by default in a lightweight mode:

- IDs and journal records always exist
- small ring buffers are always available
- full artifact capture is opt-in or environment-dependent

### Live Journal Access

The core public debug surface shipped by this plan is:

- `session.journal` for live, read-only journal access
- `session.export_debug_bundle(...)` for portable capture
- `RunBundle.load(path)` / `load_bundle(path)` for offline analysis

This is the supported migration path for users who currently depend on
event logging, tracing, or in-memory metrics exports.

### Debug Bundle Export

Production issues are exportable as a self-contained bundle:

- `session.export_debug_bundle(...)`
- optional redaction
- optional inline artifacts
- stable schema for replay and regression tests
- SHA-256 manifest for integrity
- provider version strings for reproduction fidelity

### Replay

Three replay classes with explicit fidelity labels so users are never
surprised by non-determinism:

- `artifact_replay`: fully deterministic, VCR-style cassette playback from
  captured stage inputs/outputs. Suitable for STT and TTS stages.
- `simulated_replay`: mock-injected re-execution with captured context.
  Best-effort determinism. Suitable for the agent stage — LLM responses are
  inherently non-deterministic, and this must be documented clearly.
- `live_reexecution`: re-run from captured inputs against live providers.
  Non-deterministic. Suitable for reproduction attempts.

Each `ReplaySpec` carries its fidelity class explicitly.

Forked replay (time-travel from a chosen checkpoint) is a follow-up once
the three base classes are stable.

### Redaction

Redaction is a **journal write filter**, not a post-hoc scrub, so sensitive
data never persists unredacted. Debug bundle export applies a second
redaction pass with a potentially stricter policy than the runtime default.

The policy controls per-field sensitivity:

- transcript text capture (redact | hash | retain)
- audio retention (drop | retain)
- tool argument/result retention (redact | hash | retain)
- provider payload retention (redact | retain)
- environment metadata exposure (redact | retain)

## Runtime Changes

### Replace Multiple Observability Systems With One Journal

Today's `EventTraceLogger`, `Tracer`/`Span`/`SpanManager`, and
`InMemoryMetrics` become derived views over journal records.

Effects:

- logging becomes a journal formatter
- tracing becomes journal-derived
- metrics become journal-derived aggregations
- the future debugger UI becomes a journal reader

### Decompose the Session Monolith

`Session` (1,500+ lines, ~45 async methods) decomposes incrementally. A
big-bang rewrite of a 1,500-line class with comprehensive test coverage is
too risky. Each extraction preserves existing test behavior.

1. Extract per-turn state from Session instance variables into `TurnContext`
2. Extract interruption logic into `InterruptionController`
3. Extract voice delivery tracking into `VoiceDeliveryLedger`
4. Wrap existing provider calls with Stage interfaces (facade first, then
   migrate internals)
5. Session becomes a thin facade that wires stages and manages lifecycle

### Package Layout

```text
src/easycat/runtime/
  context.py
  journal.py
  records.py
  artifacts.py
  replay.py
  redaction.py

src/easycat/stages/
  base.py              # Stage protocol + shared helpers
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
```

Rewrite targets:

- `src/easycat/session/` (decompose the monolith)
- `src/easycat/agent_runner.py` (merge into stage + bridge)
- `src/easycat/event_logging.py` (replace with journal formatter)
- `src/easycat/tracing.py` (replace with journal-derived tracing)
- `src/easycat/metrics.py` (replace with journal-derived aggregations)
- `src/easycat/_span_manager.py` (absorbed by stage + journal)

Retained conceptually but rewritten around the new bridge boundary:

- `src/easycat/agents/openai_agents.py`
- `src/easycat/agents/pydantic_ai.py`
- `src/easycat/agents/pydantic_ai_workflow.py`
- `src/easycat/agents/factory.py`

## Migration Strategy

### Test-Driven Migration

The existing ~96-file test suite is the migration safety net. Every
refactoring step keeps integration tests green:

1. Add journal-based assertions alongside existing test infrastructure
2. Extract components behind interfaces that Session delegates to
3. Verify existing tests pass with new delegation (behavior-preserving refactor)
4. Add new journal-specific tests for new capabilities
5. Remove legacy observability code only after journal equivalents are proven

### User Migration

Breaking changes to config, imports, and adapter/debug surfaces are allowed
inside this plan. The requirement is that each workstream explicitly freeze
its public surface in the RFC, document before/after usage, and update the
migration guide and release notes before legacy paths are removed.

## Workstreams

Implementation is split into five sequential workstreams, each with its
own task list, acceptance criteria, and verification procedure in a
dedicated file. This file contains the design rationale and target
architecture; the workstream files contain the operational plans.

Every workstream starts with an RFC review of its Phase N design before
implementation begins, keeps existing tests green throughout, and ends
with a completeness gate that must be closed before the next workstream
starts.

### Workstream 1: Journal Foundation

See `workstream-1-journal-foundation.md`.

**Goal**: replace `EventTraceLogger`, `Tracer`/`Span`/`SpanManager`, and
`InMemoryMetrics` with a single `ExecutionJournal` + `ArtifactStore`
backed by a redaction write filter and an optional crash-durable SQLite
backend. Strangler-fig adapters keep the legacy systems running during
migration.

**Deliverable**: journal-backed observability, a read-only live journal
surface on `Session`, explicit migration notes for the new debug/config
surface, and the full existing test suite passing unmodified.

### Workstream 2: Agent Bridge Layer

See `workstream-2-agent-bridge.md`.

**Depends on**: Workstream 1.

**Goal**: replace the runner-centric adapter flow with an
`ExternalAgentBridge` protocol that exposes framework execution state
(handoffs, tool calls, node transitions) as first-class journal
records. Validate the bridge boundary by making MCP pass-through work
without introducing any EasyCat-native tool code.

**Deliverable**: framework compatibility preserved, committed handoffs
and node transitions visible in the journal, MCP forwarding proven on
both OpenAI Agents and PydanticAI.

### Workstream 3: Stage Refactor and Session Decomposition

See `workstream-3-stage-refactor.md`.

**Depends on**: Workstream 1 and Workstream 2.

**Goal**: decompose the 1,512-line `_session.py` into stage + context +
controller types. Extract `TurnContext`, `InterruptionController`,
`VoiceDeliveryLedger`, and `RunContext`. Port 8 stages (`Transport`,
`Audio`, `VAD`, `STT`, `Turn`, `Agent`, `TTS`, `Telephony`) behind a
common `Stage` protocol with bidirectional control signal flow. Reduce
Session to a thin facade.

**Deliverable**: coherent stage-based runtime with replayable
boundaries, Session reduced substantially toward the < 400-line target,
both `chained_pipeline` and `realtime_session` modes supported without
conceptual distortion.

### Workstream 4: Replay and Bundle Export

See `workstream-4-replay-and-bundle.md`.

**Depends on**: Workstreams 1, 2, and 3.

**Goal**: make production failures local repro artifacts with honest
replay semantics. Ship `ReplaySpec` with three explicit fidelity classes
(`artifact`, `simulated`, `live`), `RunBundle` export with SHA-256
manifest and provider version strings, committable-boundary enforcement
so replay refuses to start at unsafe points, and minimal pytest fixture
helpers. Crashed sessions produce loadable bundles via Workstream 1's
crash-durability contract.

**Deliverable**: production failures become local repro artifacts,
crashed sessions can be turned into bundles after the fact, replay
fidelity is always explicit, and bundles persist only secret-safe
config/environment metadata.

### Workstream 5: Legacy Removal

See `workstream-5-legacy-removal.md`.

**Depends on**: Workstreams 1, 2, 3, and 4.

**Goal**: delete the three legacy observability systems, the
`agent_runner.py` module, the strangler-fig adapters and feature flag,
and any duplicated state paths left on Session. Ship a migration guide
for external consumers.

**Deliverable**: cleaner codebase with one debugging model; a material
line-count reduction with 1,000 removed lines as a target rather than a
gate; `CLAUDE.md` updated; migration coverage for the removed public
surface shipped.

## Realtime Session Architecture

Chained and realtime modes differ in ways that matter for the journal and
bridge:

| Aspect | Chained | Realtime |
|---|---|---|
| Audio flow | Transport → VAD → STT → Agent → TTS → Transport | Transport → Realtime Model → Transport |
| Transcript | Always available (STT output) | Partial, delayed, or optional |
| TTS | Explicit stage with provider | Model-generated speech output |
| Interruption | Cancel TTS queue + patch history | Signal to live multimodal session |
| Tool calling | Between turns | Mid-conversation, audio stream stays open |
| Stage boundaries | Discrete, well-defined | Soft, some fused or absent |

In realtime mode the journal must:

- accept partial or deferred transcript artifacts
- record audio flow events without requiring STT/TTS stage boundaries
- track tool calls that happen within a continuous audio session
- record interruption as a signal to the realtime session, not a TTS queue
  cancellation

The bridge in realtime mode must:

- support bidirectional audio streaming (not just text in → text out)
- emit transition records for tool calls within continuous sessions
- handle interruption as a session-level signal
- provide state snapshots that reflect the multimodal session state

## Success Criteria

- Every turn has a stable journal trail.
- Every major failure can be tied to one stage with concrete inputs and
  outputs.
- Both chained and realtime runtime modes are supported without conceptual
  distortion.
- Replay semantics are explicit about what is deterministic versus
  re-executed.
- Production failures can be exported and replayed locally, including from
  crashed sessions (journal is crash-durable with SQLite backend).
- The same underlying records power logs, metrics, and (eventually) the
  debugger UI.
- EasyCat still clearly presents itself as a runtime around external
  agents, not a new agent framework.
- MCP server pass-through works on both bridges with zero EasyCat-native
  tool code.
- Session is no longer a monolith.
- The three pre-existing observability systems (`EventTraceLogger`,
  `Tracer`, `InMemoryMetrics`) are gone.

## Explicit Guardrails

The following are out of bounds for this redesign and for EasyCat in
general:

- EasyCat-native tool API
- EasyCat-native MCP client or tool registry (pass-through only)
- EasyCat-native planner/router
- EasyCat-native memory layer
- EasyCat-native prompt compiler
- EasyCat-native multi-agent abstraction beyond compatibility bridges
- Hosted observability backend (self-hosted debugging first)
- A fourth progressive disclosure layer (three is the maximum)

If a feature would require owning agent semantics instead of recording or
transporting them, it stays outside EasyCat. If it requires EasyCat to
define a deeper cross-framework ontology for agent behavior than debugging
or replay needs, it also stays out of scope.

## Appendix: Journal Record Schema

Concrete record types for Phase 0 agreement. All records share a base:

```python
@dataclass(frozen=True)
class JournalRecord:
    sequence: int              # monotonic within session
    timestamp: float           # time.monotonic_ns()
    session_id: str
    run_id: str
    turn_id: str | None
    stage: str                 # e.g. "stt", "agent", "tts", "vad"
    operation: str             # e.g. "start", "complete", "error", "cancel"
    input_ref: str | None      # artifact store key
    output_ref: str | None     # artifact store key
    state_before: dict | None  # stage snapshot before operation
    state_after: dict | None   # stage snapshot after operation
    timing: TimingInfo         # wall_ms, cpu_ms, queue_ms
    metrics: dict[str, float]  # stage-specific counters
    status: str                # "ok", "error", "cancelled", "timeout"
    error: ErrorInfo | None
    metadata: dict[str, Any]   # stage-specific, framework-specific extras
```

Framework transition records extend the base with typed fields:

```python
@dataclass(frozen=True)
class FrameworkTransitionRecord(JournalRecord):
    from_unit: str | None
    to_unit: str | None
    transition_kind: str       # "handoff", "node_change", "tool_phase"
    reason: str | None
    framework_metadata: dict[str, Any]
    state_snapshot_ref: str | None  # artifact store key
```

## Immediate Next Step

Start Workstream 1 by executing task T1.0 in
`workstream-1-journal-foundation.md`: write and merge the Phase 1
implementation RFC covering journal record classes, artifact store
interface, backend selection, crash-durability contract, strangler-fig
wiring, and incremental test migration.

Each subsequent workstream opens with its own RFC task (T2.0, T3.0,
T4.0, T5.0), which must be reviewed and merged before implementation in
that workstream begins. Workstream RFCs gate workstream execution; the
essential plan in this file gates the RFCs.
