# Workstream 2A: Agent Bridge Protocol and Bridges

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstream 1 (Journal Foundation) must be complete.
> **Successors**: Workstream 2B (Interruption and MCP) adds the full
> interruption contract, the journal-atomicity clause, and MCP
> pass-through on top of the bridges shipped here. Workstream 3 (Stage
> Refactor) wraps the bridge as `AgentStage`.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2b-interruption-and-mcp.md`
> - `workstream-2c-remote-bridge.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-4-replay-and-bundle.md`
> - `workstream-5-legacy-removal.md`
>
> **Why the WS2 split.** The original WS2 bundled two orthogonal
> concerns: defining the bridge protocol (types, three concrete
> bridges, committable enumeration, facade) and defining the
> interruption + MCP contract (seven-step flow, journal atomicity
> clause, per-bridge cancellation-mode tests, MCP forwarding). The
> first concern blocks WS3; the second requires WS3's
> `InterruptionController` to exist before it can be fully tested.
> Splitting unblocks WS3 and lets WS2B land in parallel with WS3
> rather than gating it.

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may change agent-facing config and
> construction APIs if needed, but every such change must be frozen in the
> RFC and included in the migration guide.

## Goal

Replace the runner-centric adapter flow with an `ExternalAgentBridge`
protocol that exposes framework execution state (handoffs, tool calls,
node transitions) as first-class journal records. Ship three concrete
bridges — `OpenAIAgentsBridge`, `PydanticAIBridge` (Agent + Graph
modes), `GenericWorkflowBridge` (shallow + deep modes) — each capable
of running turns end-to-end with full event capture. The full
interruption contract and MCP pass-through land in **Workstream 2B**,
which builds on the bridges shipped here.

## Scope

**In scope:**

- `ExternalAgentBridge` protocol and shared types
- `AgentRecorder` protocol (the journal write-side shim bridges use),
  including the `RecorderContext` side-channel (consumed by WS2B MCP
  pass-through), the `unit()` context manager, and the
  `RecorderInvariantError` enforcement
- `OpenAIAgentsBridge` port (from `OpenAIAgentsAdapter`)
- **`PydanticAIBridge`** — unified bridge for both
  `pydantic_ai.Agent` and `pydantic_graph.Graph`. Duck-types at
  construction and routes to one of two internal modes. Agent
  mode captures the full PydanticAI event taxonomy
  (`PartStartEvent`, `PartDeltaEvent`, `FunctionToolCallEvent`,
  `FunctionToolResultEvent`, `FinalResultEvent`). Graph mode
  layers `workflow_node` cursor entries on top of the same inner
  event stream and forwards per-agent events from inside graph
  nodes via PydanticAI's `event_stream_handler` convention (with
  construction-time validation — no silent fallback). Both modes
  share one event-translator module so event mapping is
  implemented once.
- **`GenericWorkflowBridge`** — bridge for user-defined
  orchestration code that does not use `pydantic_ai.Agent` or
  `pydantic_graph.Graph`. Two opt-in modes: shallow (user
  implements `on_user_turn(text)`, bridge sees only text output)
  and deep (user's `on_user_turn(text, *, recorder)` receives
  the `AgentRecorder` and calls `recorder.record_*` methods
  manually from inside their own code to emit unit entries,
  tool calls, and handoffs). Signature inspection picks the
  mode. This is the supported path for custom multi-agent
  orchestration patterns that PydanticAI docs recommend (app
  loops, programmatic hand-offs, custom inference backends).
- Execution cursor model
- Committable-boundary enumeration per bridge (bridges publish
  `COMMITTABLE_BOUNDARIES`; WS2B and WS4 consume it)
- Framework transition records (all seven, including
  `InterruptionApplyFailed` emitted by WS2B)
- Framework state snapshots routed through the WS1 safe-default
  `apply_write_filter` hook (upgradable to a full `RedactionPolicy`
  in `peripheral-redaction.md` without bridge changes)
- **Basic** `apply_interruption(delivered_text, mode)`
  implementation on each bridge: the framework-state mutation
  logic only. Bridges produce a single-phase apply that updates
  framework state directly, without the journal-atomicity clause.
  WS2B replaces this with the four-step atomic write ordering.
- Voice state vs framework state separation
- `AgentTurnInput.from_text()` constructor so bridges can be
  invoked directly with text (prerequisite for WS3's `text_session`
  runtime mode and `Session.send_text()`)
- `auto_adapt_agent()` preserved as a facade and survives WS5

**Deferred to Workstream 2B:**

- Seven-step interruption turn flow (T2.6 moves to WS2B)
- Journal-atomicity clause for `apply_interruption` (four-step
  write ordering: plan → write `FrameworkStateCommitted` → apply
  → paired record)
- All three cancellation modes tested across every bridge/mode
- `InterruptionApplyFailed` emission path
- `ShallowModeInterruptionError` integration with the
  `InterruptionController` (raising it is WS2A; handling the
  downgrade is WS2B since it needs the controller to exist)
- MCP pass-through per bridge (T2.7 moves to WS2B)
- `EasyCatConfig.mcp_servers` field

**Permanently out of scope (guardrail):**

- Voice-to-voice / realtime speech-to-speech bridges (OpenAI
  Realtime, Gemini Live, Kyutai, etc.). EasyCat is a chained voice
  runtime; the three bridges above are the complete set. See the
  "Chained Only" rationale and Explicit Guardrails in
  `essential-debug-first-runtime.md`. No `RealtimeBridge`, no
  fused multimodal stage, no "we'll add it later".

**Out of scope:**

- Stage model — bridge is still invoked from Session (Workstream 3)
- Replay semantics for agent stage (Workstream 4)
- Any EasyCat-native tool abstraction, registry, or decorator
  (permanent guardrail)

## Tasks

### T2A.0: Architecture Freeze (RFC)

- [ ] Write Phase 2A RFC covering:
  - `ExternalAgentBridge` protocol signature
  - `AgentRecorder` protocol: exact method list, forwarding
    semantics through the WS1 `apply_write_filter` hook, lifetime
    (bridges get one per `invoke()` call; stages hold one for their
    lifetime)
  - `RecorderContext` side-channel shape (`run_id`, `session_id`,
    `turn_id`, `mcp_servers`; WS2B consumes `mcp_servers`)
  - `unit()` context manager contract and invariant enforcement
    (`RecorderInvariantError`)
  - `AgentTurnInput`, `AgentBridgeEvent`, `FrameworkStateSnapshot`
    type definitions, including the `state_ref` / inline-vs-artifact
    policy (4 KB ceiling, SHA-256 ref format)
  - public migration path from `AgentRunner`/adapter-centric usage to the
    bridge-centric runtime
  - Execution cursor fields and `committable` semantics per framework
    (referencing the operational definition in
    `essential-debug-first-runtime.md` — do not redefine)
  - Committable-boundary enumeration per bridge (which `unit_kind`
    values are committable in which states; see T2.7.5)
  - Transition record catalog and field shapes (all seven records,
    including the `InterruptionApplyFailed` record that WS2B emits
    but which is defined here in T2.2), including the handoff vs
    unit-entered/exited convention
  - **Basic** `apply_interruption` contract for WS2A: single-phase
    framework mutation only. The four-step atomic write ordering
    (plan → write `FrameworkStateCommitted` → apply → paired
    record) is owned by WS2B and is explicitly out of scope for
    the WS2A RFC.
  - PydanticAI Graph mode convention validation at construction
    time (no silent runtime fallback)
  - `ShallowModeInterruptionError` as a WS2A-raised exception;
    WS2B owns the controller-side downgrade path
  - `PydanticAIBridge` unified design: one class, two input
    modes (Agent, Graph); the shared event translator module
    (`_pydantic_ai_events.py`) used by both modes; the Graph
    mode's `Graph.iter()` walker that emits `workflow_node`
    cursor entries; the per-agent event forwarding convention
    for Graph mode via `ctx.state._easycat_event_handler`
    validated at construction time (not runtime); how nested
    cursors with `parent_unit_id` are populated for agents
    running inside graph nodes; how `run.result.history` is
    serialized as a per-turn artifact
  - `PydanticAIBridge` Graph mode constructor arguments
    (`graph`, `state_factory`, `initial_node_factory`, optional
    `agents` argument reserved for WS2B MCP forwarding) and the
    mutually-exclusive validation between Agent mode and Graph
    mode
  - `PydanticAIBridge` Graph mode state snapshot strategy:
    artifact-ref storage for the user-defined state dataclass,
    JSON-safety filter that skips non-serializable fields with
    a warning record, interaction with the `apply_write_filter`
    hook
  - `GenericWorkflowBridge` design: shallow mode vs deep mode
    protocol definitions, signature inspection to pick the
    mode, how deep-mode workflows receive the `AgentRecorder`
    and call its methods from user code, the `unit()` context
    manager as the mandatory enter/exit idiom, the fallback
    behavior when a workflow object exposes neither protocol
    cleanly, `ShallowModeInterruptionError` semantics
  - `GenericWorkflowBridge` recommended examples (see WS2A
    appendix) for the PydanticAI-documented custom
    orchestration patterns: programmatic hand-off app loop,
    output-type hand-off functions, custom inference backend
    with tool dispatch
- [ ] Review and merge RFC before implementation.

### T2.1: Bridge Protocol, AgentRecorder, and Shared Types

- [ ] Create `src/easycat/integrations/agents/base.py`
- [ ] Define `ExternalAgentBridge` Protocol with
  `invoke`, `snapshot_state`, `apply_interruption`, `reset`
- [ ] Define `AgentTurnInput`, `AgentBridgeEvent`
- [ ] `AgentTurnInput` must be constructible from a raw text string
  plus optional context, independent of any surrounding voice
  pipeline. This is the seam that the WS3 `text_session` runtime
  mode and `Session.send_text()` rely on: call
  `AgentTurnInput.from_text(text, context=...)` and hand it to
  `bridge.invoke()` without needing STT output or a voice turn.
  The existing voice path continues to construct `AgentTurnInput`
  from STT output as it does today.
- [ ] Define `FrameworkStateSnapshot` dataclass with explicit field
  shape:

  ```python
  @dataclass(frozen=True)
  class FrameworkStateSnapshot:
      # Small, JSON-safe fields inlined directly. Bridges MUST only
      # put primitive, serializable values here (strings, ints,
      # floats, bools, short lists/dicts of those). Total inline
      # size target: < 4 KB per snapshot. Anything larger goes via
      # `state_ref`.
      fields: dict[str, Any]

      # Artifact reference for large or non-trivially-serializable
      # state. When set, `fields` carries only a summary (kind,
      # class name, key identifiers) and the full state lives in
      # the artifact store addressed by this SHA-256 ref.
      # Mandatory for PydanticAI Graph mode (user-defined State
      # dataclasses can be arbitrarily large) and for any bridge
      # whose snapshot would exceed 4 KB inline.
      state_ref: str | None = None

      # Bridge-scoped kind tag so downstream tools can route
      # snapshots without sniffing field names. Example values:
      # "openai_agents", "pydantic_ai_agent", "pydantic_ai_graph",
      # "generic_workflow".
      kind: str = ""
  ```

- [ ] `FrameworkStateSnapshot` must be JSON-safe, honor the WS1
  Config and Environment Safety Default (no raw API keys, auth
  headers, or env dumps), and use artifact refs for large payloads
  rather than raw framework objects. The rule is: any snapshot
  whose serialized `fields` dict exceeds **4 KB** or contains
  any non-JSON-safe value MUST set `state_ref` and move the heavy
  payload to the artifact store via `AgentRecorder.record_state_
  snapshot(ref)`. Snapshots pass through the WS1 `apply_write_
  filter` hook on every write — bridges must not bypass the hook.
  The hook is a no-op in WS1 beyond the hard-coded allowlist;
  `peripheral-redaction.md` later layers a full `RedactionPolicy`
  on top without changing bridge code.
- [ ] Define `ExecutionCursor` dataclass: `unit_id`, `unit_kind` (enum:
  `agent`, `specialist`, `workflow_node`, `model_node`, `tool_call`),
  `display_name`, `parent_unit_id`, `sequence`, `entered_at`,
  `committable`
- [ ] Define `CancellationMode` enum: `immediate_stop`,
  `drain_current_unit`, `drain_to_commit_point`

### T2.1.5: AgentRecorder Protocol

- [ ] Define `AgentRecorder` Protocol in
  `src/easycat/integrations/agents/base.py` with explicit methods
  and a typed `context` attribute:

  ```python
  @dataclass(frozen=True)
  class RecorderContext:
      """Bridge-readable side-channel exposed by the recorder.

      Used by WS2B T2B.6 MCP pass-through to surface the
      configured `mcp_servers` list to deep-mode workflows and by
      future peripherals that need to pass runtime configuration
      into bridges without threading it through every method.
      """

      run_id: str
      session_id: str
      turn_id: str | None
      mcp_servers: tuple[str, ...] = ()
      # Additional runtime-configured hooks can be added here in
      # peripheral follow-ups without changing the AgentRecorder
      # protocol. `mcp_servers` is the only field WS2B consumes.


  class AgentRecorder(Protocol):
      @property
      def context(self) -> RecorderContext:
          """Read-only runtime context exposed to bridges.

          Deep-mode `GenericWorkflowBridge` workflows read
          `recorder.context.mcp_servers` to discover the
          configured MCP server list; other bridges inspect
          `context.run_id`/`turn_id` for diagnostic records.
          """
          ...

      def record_unit_entered(self, cursor: ExecutionCursor) -> None: ...
      def record_unit_exited(
          self, cursor: ExecutionCursor, reason: str | None
      ) -> None: ...

      @contextmanager
      def unit(
          self, cursor: ExecutionCursor, *, commit_on_exit: bool = True
      ) -> Iterator[ExecutionCursor]:
          """Context manager wrapping `record_unit_entered` /
          `record_unit_exited` with guaranteed exit on exception.

          Usage (mandatory in `GenericWorkflowBridge` deep mode
          examples; recommended everywhere else):

              with recorder.unit(cursor) as c:
                  ...  # user code
                  # cursor.with_committable(True) yielded if
                  # commit_on_exit=True and no exception raised

          On exception, `record_unit_exited` is called with
          reason=f"exception:{type(exc).__name__}" and the
          exception is re-raised. Unmatched enter/exit pairs in
          deep mode are impossible when this is used.
          """
          ...

      def record_tool_call(
          self,
          phase: Literal["start", "delta", "result", "error"],
          name: str,
          args_ref: str | None,
          result_ref: str | None,
      ) -> None: ...
      def record_state_snapshot(self, ref: str) -> None: ...
      def record_framework_handoff(
          self,
          from_unit: str | None,
          to_unit: str,
          reason: str | None,
      ) -> None: ...
      def record_cancellation_boundary(
          self,
          mode: CancellationMode,
          reason: str | None,
          caused_by_signal_id: str | None = None,
      ) -> None: ...
      def record_framework_error(self, error: ErrorInfo) -> None: ...
  ```

- [ ] Implementations forward each call into the WS1 journal via
  `journal.append`, passing through the T1.5 `apply_write_filter`
  hook and the hard-coded safe-default allowlist
- [ ] `record_cancellation_boundary` forwards `caused_by_signal_id`
  onto `FrameworkCancellationBoundaryReached` so WS1
  `ControlSignalRecord`s compose with WS2 framework cancellation
  records (see WS1 T1.1 composition note)
- [ ] Lifetime contract: bridges receive a fresh `AgentRecorder`
  per `invoke()` call, bound to the current `run_id`/`session_id`/
  `turn_id`; stages hold one for their lifetime bound to
  `run_id`/`session_id`
- [ ] `AgentRecorder.context.mcp_servers` is populated by the
  runtime when constructing the recorder, reading from
  `EasyCatConfig.mcp_servers`. Bridges read it but never write
  to it — the context is frozen.
- [ ] `AgentRecorder` is an internal type (not in `easycat.__all__`);
  only bridges, stages, and the journal touch it
- [ ] **Invariant enforcement at the recorder boundary.** The
  recorder implementation tracks open cursors per turn; calling
  `record_unit_exited` without a matching `record_unit_entered`,
  or exiting cursors in a different order than entered, raises
  `RecorderInvariantError` at call time. This catches deep-mode
  `GenericWorkflowBridge` misuse immediately rather than letting
  it corrupt the journal. The `unit()` context manager makes it
  impossible to hit this invariant.

### T2.2: Transition Records

- [ ] Add transition record types to
  `src/easycat/runtime/records.py`. The **complete** list for WS2
  is these seven records; any additional record type requires a
  WS2 RFC amendment, not a silent extension:
  - `FrameworkUnitEntered`
  - `FrameworkUnitExited`
  - `FrameworkStateCommitted` — bridge emits this *before*
    mutating framework state in `apply_interruption` (see WS2B T2B.1
    journal-atomicity clause)
  - `FrameworkHandoff`
  - `FrameworkToolPhaseChanged`
  - `FrameworkCancellationBoundaryReached`
  - `InterruptionApplyFailed` — emitted by `apply_interruption`
    when the pre-mutation `FrameworkStateCommitted` journal
    write succeeded but the subsequent framework mutation raised
    or returned an error. Carries the same cursor/snapshot refs
    as the `FrameworkStateCommitted` it is paired with, plus an
    `ErrorInfo`. The runtime treats this as a hard failure and
    falls back to `CancellationMode.immediate_stop` on the
    corresponding turn.
- [ ] Each extends `FrameworkTransitionRecord` from Workstream 1
- [ ] Include `from_unit`, `to_unit`, `transition_kind`, `reason`,
  `framework_metadata`, `state_snapshot_ref` fields. Additionally:
  - `FrameworkCancellationBoundaryReached` includes
    `caused_by_signal_id: str | None` linking back to the WS1
    `ControlSignalRecord` that triggered the boundary (see WS1
    T1.1 composition note).
  - `FrameworkStateCommitted` and `InterruptionApplyFailed`
    include `mutation_kind: str` naming the kind of mutation
    (`interrupt_truncate`, `interrupt_drain`, etc.) and
    `pre_state_ref` / `post_state_ref` artifact references.
- [ ] **Handoff record convention.** A handoff emits a triple on
  the journal timeline in strictly increasing sequence: one
  `FrameworkUnitExited(unit=from_unit)` record, one
  `FrameworkHandoff(from_unit, to_unit, reason)` record, and one
  `FrameworkUnitEntered(unit=to_unit)` record. Downstream tools
  rely on this triple being atomic — bridges MUST emit all three
  before yielding control, and MUST NOT interleave other records
  between them within the same turn.

### T2.3: OpenAI Agents Bridge

- [ ] Create `src/easycat/integrations/agents/openai_agents.py`
- [ ] Port `src/easycat/agents/openai_agents.py` contents to a
  bridge-shaped class that implements `ExternalAgentBridge`
- [ ] Capture and record via the `AgentRecorder`:
  - rendered instructions
  - model settings and run config
  - response IDs and `previous_response_id`
  - tool `start`/`delta`/`result` events
  - handoff transitions (treat `last_agent` changes as committed
    handoffs)
  - framework-managed history snapshots
  - provider request IDs and error payloads
- [ ] Distinguish text streaming cancellation from tool-call drain
- [ ] Support server-managed history cases where spoken-text patches
  are represented as deferred notes (not direct mutation)
- [ ] Record response-chain continuity separately from agent
  transitions
- [ ] `snapshot_state()` returns current agent, response IDs, local
  history mirror
- [ ] `apply_interruption(delivered_text, mode)` mutates framework
  state appropriately for each mode

### T2.4: PydanticAI Bridge (Agent and pydantic_graph)

**One bridge, two input modes.** `PydanticAIBridge` accepts either
a plain `pydantic_ai.Agent` or a `pydantic_graph.Graph`,
duck-types which at construction, and provides deep event capture
in both cases. PydanticAI users building a single-agent app and
PydanticAI users building a multi-agent `pydantic_graph` workflow
use the same bridge class; the Graph path additionally emits
`workflow_node` cursor entries layered on top of the same inner
event stream.

- [ ] Create `src/easycat/integrations/agents/pydantic_ai.py`
- [ ] Create shared event-translator module
  `src/easycat/integrations/agents/_pydantic_ai_events.py`
  containing one canonical `translate_event(event, recorder)`
  helper that maps every PydanticAI `AgentStreamEvent` subtype
  (`PartStartEvent`, `PartDeltaEvent` with `TextPartDelta` /
  `ThinkingPartDelta` / `ToolCallPartDelta`, `FunctionToolCallEvent`,
  `FunctionToolResultEvent`, `FinalResultEvent`) into the
  appropriate `AgentBridgeEvent` yield and/or `AgentRecorder`
  call. Used identically by the Agent path and the Graph path
  below. **Convention (forward-compatibility note):** event
  translators are named `_<framework>_events.py` and live
  alongside their bridges. Any future framework bridge
  (LangChain, LangGraph, etc. — see
  `peripheral-langchain-langgraph-bridge.md`) follows the same
  convention: one translator module per framework, sibling to
  the bridge file, with a single `translate_event(event,
  recorder)` entry point. This keeps event-mapping churn from
  one upstream framework localized to one file and lets bridges
  that wrap multiple constructs in the same framework (e.g.
  `PydanticAIBridge` Agent+Graph modes) share translation code
  cleanly.
- [ ] `PydanticAIBridge.__init__` accepts **one** of:
  - `agent: pydantic_ai.Agent` — single-agent mode. Optional
    `deps`, `model_settings`.
  - `graph: pydantic_graph.Graph` plus
    `state_factory: Callable[[], StateT]`,
    `initial_node_factory: Callable[[str, StateT], BaseNode]`,
    and optional `agents: list[Agent]` (for MCP forwarding;
    auto-discovered from graph nodes if omitted) — graph mode.
  - Passing both raises `BridgeInputError`. Passing neither
    raises `BridgeInputError`. Auto-adapt dispatch (T2.8) picks
    the right mode based on the object type.

#### Agent mode

- [ ] Capture and record via `agent.iter()` (preferred) or
  `agent.run_stream_events()`:
  - input text and message history passed to the framework
  - `deps` and `model_settings` (via safe-default allowlist, not
    raw)
  - `UserPromptNode` → emit cursor with `unit_kind="user_prompt"`
  - `ModelRequestNode` → emit cursor with `unit_kind="model_node"`,
    `committable=False`, then stream events through the shared
    translator
  - `CallToolsNode` → emit cursor with `unit_kind="tool_call"`,
    stream `FunctionToolCallEvent` / `FunctionToolResultEvent`
    through the shared translator
  - `End` → exit the outermost `agent` cursor with
    `committable=True`
  - final output object (including structured `output_type` values)
  - `new_messages()` history updates
- [ ] Distinguish model-request nodes from tool-call nodes for
  cancellation safety (set `committable=False` on model-request
  nodes while streaming).
- [ ] Interruption patching walks the in-memory PydanticAI message
  history and mutates the most recent `ModelResponse` `TextPart`
  via `object.__setattr__` (matching the existing adapter
  behavior).
- [ ] `snapshot_state()` returns message history, allowlisted
  deps/model_settings, and current cursor position.

#### Graph mode (pydantic_graph)

- [ ] `invoke()` walks `Graph.iter(initial_node, state=state)`
  asynchronously:
  - For each yielded `BaseNode`, emit a cursor with
    `unit_kind="workflow_node"`, `display_name=type(node).__name__`,
    `sequence=<position in run.history>`, `committable=True`
    (nodes are atomic by `pydantic_graph` design)
  - When the node class name changes between yielded nodes, emit
    the `FrameworkUnitExited` → `FrameworkHandoff` →
    `FrameworkUnitEntered` triple (AC2.17) with
    `transition_kind="graph_transition"` and
    `reason=<previous node's return branch>` when determinable
  - Check `cancel_token` between nodes; mid-node cancellation is
    cooperative via the event-handler path below
- [ ] Per-agent event capture inside graph nodes via the
  PydanticAI `event_stream_handler` protocol:
  - The bridge constructs a `_GraphEventHandler` instance per
    `invoke()` call that wraps the `recorder` and the shared
    event translator
  - The handler is installed on the graph's state via a
    documented convention (`state._easycat_event_handler`) that
    workflow authors pass to their `agent.run(...)` /
    `agent.run_stream(...)` / `agent.iter(...)` calls as
    `event_stream_handler=ctx.state._easycat_event_handler`
  - The handler translates incoming `AgentStreamEvent`s through
    the shared translator, emitting nested cursor entries with
    `parent_unit_id=<current workflow_node id>` and
    `unit_kind="agent"` / `model_node` / `tool_call`
- [ ] **Convention validation at construction, not runtime.** The
  old "emit a warning journal record per turn if the convention
  is not honored" policy is replaced with a construction-time
  check that runs once and fails loudly:
  - At `__init__` time the bridge calls `state_factory()` once
    to produce a probe state instance and inspects it for an
    `_easycat_event_handler` attribute (present and writable).
  - If the attribute is missing or non-writable, the bridge
    raises `BridgeConfigurationError` with a message naming the
    convention and pointing at the bridge README example. The
    bridge does not construct.
  - If the attribute is present, the bridge still checks at
    first `invoke()` that the initial node factory installs the
    handler correctly (by monkey-patching the handler slot and
    asserting it was used by the first agent call within the
    turn). If the first turn runs without any agent calls
    passing the handler through, the bridge raises
    `ConventionViolationError` at end-of-turn — not a soft
    warning record.
  - This replaces the old silent-failure mode where a graph
    whose nodes misspelled `_easycat_event_handler` would log a
    warning from a handler that was never called. Hard error,
    fail-fast, single stack trace.
- [ ] Graph-mode `snapshot_state()` captures:
  - graph class name
  - current active node class name
  - state object serialized via artifact ref (user-defined
    `State` dataclasses can be arbitrarily large; never inline
    them in a record)
  - `run.result.history` node sequence as a workflow-level
    artifact after each turn
- [ ] Graph-mode `apply_interruption(delivered_text, mode)` walks
  the graph's state for the most recently active agent's message
  history and truncates the last `ModelResponse` `TextPart` using
  the same mechanism as Agent mode. Workflow authors can opt into
  richer interruption semantics by exposing a
  `truncate_last_assistant(delivered_text)` method on their state
  dataclass; the bridge calls it when present. **WS2A scope:** this
  implements the single-phase mutation path only. WS2B wraps it in
  the four-step atomic write ordering (plan → write
  `FrameworkStateCommitted` → apply → paired record).
- [ ] Graph-mode `reset()` clears state reference, active node,
  and any bridge-owned event-handler state.
- [ ] **Graph-mode MCP forwarding is deferred to WS2B (T2B.7).**
  The `agents=` constructor argument is reserved at the protocol
  level here so WS2B can wire it without breaking the
  construction API, but no actual forwarding happens in WS2A.
  The `EasyCatConfig.mcp_servers` field does not exist until
  WS2B.

### T2.5: GenericWorkflowBridge

**Bridge for user-defined orchestration code that does not use
`pydantic_ai.Agent` or `pydantic_graph.Graph`.** This is the path
for users who wrote their own multi-agent orchestration in plain
Python, or who use another framework EasyCat does not ship a
first-class bridge for, or who wrap non-PydanticAI inference
backends with custom turn-handling logic. The PydanticAI docs
explicitly recommend several custom-orchestration patterns (app
loops, hand-off functions, programmatic dispatch) that do not
involve `pydantic_graph`, and this bridge supports them as
first-class citizens.

Two modes, both shipped. Users pick by implementing the
appropriate signature:

- **Shallow mode (default).** User implements
  `on_user_turn(text) -> str` or
  `on_user_turn_streaming(text) -> AsyncIterator[str]`. The bridge
  wraps the whole turn in a single `workflow_node` cursor entry,
  yields text output as `AgentBridgeEvent.text_delta`, and does
  not see tool calls, sub-agent handoffs, or internal execution
  structure. Low-effort integration; debugging is limited to the
  turn boundary.
- **Deep mode (opt-in).** User implements
  `on_user_turn(text, *, recorder: AgentRecorder,
  cancel_token: CancelToken | None = None) -> AsyncIterator[str]`.
  The bridge passes its `AgentRecorder` and cancel token directly
  into the workflow code, and the user calls `recorder.record_*`
  methods from inside their own orchestration to emit unit
  entries, tool calls, and framework handoffs. The bridge still
  wraps the turn in an outer `workflow` cursor; everything the
  user records shows up nested beneath it.

The bridge detects which mode the user implements via signature
inspection: if `on_user_turn`'s signature has a `recorder`
parameter, deep mode is used; otherwise shallow mode is used.
Users can migrate from shallow to deep incrementally by adding
the parameter and calling `recorder.*` methods for whichever
units they care to expose.

- [ ] Create `src/easycat/integrations/agents/generic_workflow.py`
- [ ] Define `WorkflowProtocol` (shallow) and `DeepWorkflowProtocol`
  (with recorder) as `typing.Protocol` classes with
  `runtime_checkable`
- [ ] `GenericWorkflowBridge.__init__` accepts:
  - `workflow: Any` — any object implementing one of the two
    protocols
  - optional `display_name: str` — human-readable label for the
    outer workflow cursor (defaults to `type(workflow).__name__`)
- [ ] Signature inspection at construction via
  `inspect.signature(workflow.on_user_turn)` picks shallow vs
  deep mode and freezes the decision for the bridge's lifetime.
- [ ] `invoke()` in shallow mode:
  - Emit a single `workflow` cursor entry with
    `unit_kind="workflow_node"`, `committable=False` during
    execution, `committable=True` after the turn completes
  - Await / iterate the user's `on_user_turn`, yielding text
    output as `AgentBridgeEvent.text_delta` chunks
  - Emit the `workflow` cursor exit on completion
  - Check `cancel_token` before each yielded chunk
- [ ] `invoke()` in deep mode:
  - Emit an outer `workflow` cursor entry (same as shallow)
  - Pass `recorder` and `cancel_token` into the user's
    `on_user_turn(text, recorder=..., cancel_token=...)`
  - Iterate the user's async iterator for text chunks, yielding
    them as `AgentBridgeEvent.text_delta`
  - The user's code may call any `AgentRecorder` methods during
    execution — `record_unit_entered/exited` for their own
    logical units, `record_tool_call` for tool invocations,
    `record_framework_handoff` for hand-offs between
    sub-orchestrators, `record_state_snapshot` for committable
    state, `record_framework_error` on exceptions. The bridge
    does not validate or constrain which methods the user calls;
    it trusts the user to model their own orchestration.
  - Emit the outer cursor exit on completion
- [ ] `snapshot_state()`:
  - In both modes, returns a small snapshot with the workflow
    display name, current mode (shallow or deep), and any state
    the user's workflow object exposes via an optional
    `snapshot_state()` method (called if present, result stored
    via artifact ref if larger than 1KB)
- [ ] `apply_interruption(delivered_text, mode)`:
  - **Shallow mode forbids interruption.** Shallow-mode
    workflows are opaque to the bridge — the bridge has no way
    to compute which text was delivered, no way to truncate the
    in-flight response, and no way to drain to a commit point.
    Calling `apply_interruption` on a shallow-mode bridge raises
    `ShallowModeInterruptionError("Interruption is not supported
    in GenericWorkflowBridge shallow mode. Convert the workflow
    to deep mode by adding a `recorder: AgentRecorder` parameter
    to `on_user_turn`, or implement
    `workflow.apply_interruption(delivered_text, mode)` on the
    workflow object itself.")`.
  - **Shallow mode with explicit opt-in.** A shallow workflow
    can opt into being interruptible by implementing
    `apply_interruption(self, delivered_text: str, mode:
    CancellationMode) -> None` on the workflow object directly.
    If that method exists, the bridge delegates to it instead
    of raising. The bridge does not guess; the method's
    presence is the signal.
  - **Deep mode.** Delegates to `workflow.apply_interruption(
    delivered_text, mode=mode)` when the workflow object
    implements it. When the workflow does not implement the
    method in deep mode, the bridge falls back to a best-effort
    truncation: it sets the internal cancel flag the workflow
    is expected to check (deep-mode workflows already receive
    the `cancel_token`). A warning journal record is emitted if
    the workflow never reacted to the cancel flag within a
    grace period.
  - **Runtime-side integration.** The `InterruptionController`
    (WS3) inspects the bridge's supports-interruption flag
    before deciding whether to trigger barge-in. A shallow-mode
    workflow without `apply_interruption` is flagged as
    "interruption not supported" and barge-in is downgraded to
    "end-of-turn interruption only" — the controller still
    observes the user's input but waits for the current turn to
    complete before starting the next one. The user sees
    barge-in behavior that is slightly delayed, never broken.
- [ ] `reset()`:
  - Delegates to `workflow.reset()` or `workflow.clear_history()`
    when present; otherwise no-op
- [ ] **MCP forwarding is deferred to WS2B (T2B.7).** No
  `mcp_servers` wiring happens in WS2A. The `RecorderContext.
  mcp_servers` field exists on the recorder protocol (T2.1.5)
  and is populated by WS2B; deep-mode workflows reading it
  during WS2A will always see an empty tuple.
- [ ] The bridge README documents both modes with examples
  (see appendix at end of this workstream file).

### T2.7.5: Committable-Boundary Enumeration

- [ ] Each bridge publishes a static
  `COMMITTABLE_BOUNDARIES: dict[UnitKind, CommitRule]` mapping
  declaring which cursor kinds are committable and in which states.
  Rules:
  - `OpenAIAgentsBridge`: `tool_call` → committable between phases;
    `model_node` → non-committable during streaming; `agent` →
    committable between turns only
  - `PydanticAIBridge`: unified mapping covering both Agent mode
    and Graph mode. Agent-mode kinds: `iter()` node changes
    committable between nodes, non-committable during model
    streaming; `tool_call` committable between phases; `agent`
    committable between turns only. Graph-mode kinds (additive):
    `workflow_node` committable between every graph node
    transition (nodes are atomic by `pydantic_graph` design, so
    every `Graph.iter()` yield is a committable point). Agent
    mode never emits `workflow_node`; Graph mode inherits the
    Agent-mode kinds for the inner per-agent layer via the
    shared event translator.
  - `GenericWorkflowBridge`: `workflow_node` → committable
    between turns in shallow mode (the whole turn is opaque,
    so the only safe boundary is the turn edge); in deep mode,
    whichever kinds the user emits via `recorder.record_*`
    carry whatever `committable` flag the user sets on their
    cursors — the bridge does not enforce additional rules
    because the user owns their orchestration semantics.
    Document clearly in the bridge README that deep-mode
    committability is user-determined and forked replay quality
    depends on the user declaring it correctly.
- [ ] Workstream 4 consumes `COMMITTABLE_BOUNDARIES` by name to
  enforce `ReplayError` at non-committable sequences
- [ ] A bridge that cannot determine committability for a given
  cursor state defaults to `committable=False` (safe default)

### T2.8: Facade Preservation

- [ ] `auto_adapt_agent()` still works — update it to construct the
  appropriate bridge based on duck-typed detection of the incoming
  object:
  - `openai_agents.Agent` (or subclass) → `OpenAIAgentsBridge`
  - `pydantic_ai.Agent` → `PydanticAIBridge` (Agent mode)
  - `pydantic_graph.Graph` → raise `BridgeInputError` with a
    message naming the explicit
    `PydanticAIBridge(graph=..., state_factory=...,
    initial_node_factory=...)` constructor. Because Graph mode
    requires a state factory and initial node factory that
    auto-adapt cannot guess, users wanting Graph mode construct
    the bridge explicitly. Agent mode is the only auto-adapted
    PydanticAI path.
  - Any object implementing `on_user_turn(...)` (shallow or deep
    signature) → `GenericWorkflowBridge`. The bridge's own
    signature inspection picks shallow vs deep mode; auto-adapt
    does not need to distinguish.
  - Any object implementing the raw `ExternalAgentBridge`
    protocol → returned as-is (users writing custom bridges can
    hand-construct and pass through `auto_adapt_agent`).
  - Voice-to-voice / realtime APIs are out of scope (see WS2
    scope guardrail and Explicit Guardrails in the essential
    plan); if `auto_adapt_agent()` is handed a realtime API
    client object, it raises `BridgeInputError` with a message
    pointing the user at the provider SDK directly.
- [ ] `auto_adapt_agent()` is documented as best-effort convenience, not
  a compatibility guarantee for every pre-redesign construction path
- [ ] `auto_adapt_agent()` survives Workstream 5. It is listed in the
  preserved-public-surface allowlist that WS5 AC5.15 freezes in
  `easycat.__all__`.

### T2A.9: Test Migration (Bridge-Side)

- [ ] All `tests/agents/` pass unmodified (bridge construction,
  turn execution without interruption)
- [ ] Bridge-specific tests added in this workstream (see
  Verification) cover: construction, `invoke()` end-to-end
  without interruption, transition records, committable
  boundary publication, `AgentRecorder` invariant enforcement,
  `unit()` context manager, single-phase `apply_interruption`.
- [ ] **Interruption-contract tests (all three cancellation
  modes across all bridges), shallow-mode downgrade tests, and
  MCP pass-through tests are deferred to Workstream 2B.** The
  existing `tests/session/` barge-in tests remain passing with
  the single-phase `apply_interruption` implementation in
  WS2A — they exercise end-to-end barge-in behavior at a
  coarse granularity that single-phase mutation satisfies.
  The per-mode unit tests and the journal-atomicity failure
  injection tests land in WS2B.

## Acceptance Criteria

- [ ] **AC2.1** RFC reviewed and merged.
- [ ] **AC2.2** `src/easycat/integrations/agents/base.py` defines
  `ExternalAgentBridge` Protocol, `AgentTurnInput`, `AgentRecorder`,
  `AgentBridgeEvent`, `FrameworkStateSnapshot`, `ExecutionCursor`,
  `CancellationMode`.
- [ ] **AC2.3** Seven transition record types exist in `records.py`:
  `FrameworkUnitEntered`, `FrameworkUnitExited`,
  `FrameworkStateCommitted`, `FrameworkHandoff`,
  `FrameworkToolPhaseChanged`,
  `FrameworkCancellationBoundaryReached` (with
  `caused_by_signal_id` field), and `InterruptionApplyFailed`.
- [ ] **AC2.4** `OpenAIAgentsBridge`, `PydanticAIBridge`, and
  `GenericWorkflowBridge` exist and all implement
  `ExternalAgentBridge`. `PydanticAIBridge` successfully
  constructs from both a `pydantic_ai.Agent` and a
  `pydantic_graph.Graph` (with required factories); passing both
  or neither raises `BridgeInputError` with a clear message.
- [ ] **AC2.5** Every committed handoff in OpenAI Agents produces a
  `FrameworkHandoff` record in the journal with correct `from_unit`
  and `to_unit` values.
- [ ] **AC2.6** `PydanticAIBridge` Agent mode: every `iter()` node
  change produces a `FrameworkUnitEntered` / `FrameworkUnitExited`
  pair with correct `unit_kind` (`model_node`, `tool_call`,
  `user_prompt`). Tool calls inside a `CallToolsNode` produce
  matching `record_tool_call` events via the shared event
  translator.
- [ ] **AC2.6a** `PydanticAIBridge` Graph mode tool-call
  visibility parity. Given a `pydantic_graph.Graph` with two
  nodes, each of which calls a different agent that invokes a
  tool, the journal for one turn contains: `workflow_node`
  entered for node A → nested `agent` entered for A's agent →
  `tool_call` start/result records for A's tool → `agent`
  exited → `workflow_node` exited for A → `FrameworkHandoff`
  with `transition_kind="graph_transition"` → `workflow_node`
  entered for B → nested `agent` entered for B's agent →
  `tool_call` records for B's tool → `agent` exited →
  `workflow_node` exited for B. Every tool call carries the same
  `tool_name`, `args_ref`, `result_ref`, and `tool_call_id`
  fields as AC2.6. Graph mode achieves the same event depth as
  Agent mode, with workflow-node context layered on top.
- [ ] **AC2.6b** `PydanticAIBridge` Graph mode `run.result.history`
  serialization. After a Graph-mode turn, the journal contains
  an artifact ref for `run.result.history` (the sequence of
  nodes visited), and the snapshot's `active_node` matches the
  last node class name in that history.
- [ ] **AC2.6c** `PydanticAIBridge` Graph mode state snapshot
  via artifact ref. A graph whose state dataclass contains a
  500KB field produces a journal record whose inline size is
  < 4KB; the large state lives in the artifact store accessed
  via `snapshot_state.state_ref`.
- [ ] **AC2.6d** `PydanticAIBridge` Graph mode convention is
  enforced at construction time, not runtime. Two sub-tests:
  - Construction with a `state_factory` that returns a state
    object without an `_easycat_event_handler` attribute raises
    `BridgeConfigurationError` at `PydanticAIBridge(graph=...,
    state_factory=...)` call time.
  - Construction succeeds when the convention slot is present,
    but the first `invoke()` that completes without any agent
    call passing the handler through raises
    `ConventionViolationError` at end-of-turn with a message
    naming the convention. This is a hard error, not a warning
    record.
- [ ] **AC2.6e** `GenericWorkflowBridge` shallow mode. A user
  workflow implementing only `on_user_turn(text) -> str` (or
  streaming) produces a journal with one `workflow_node` cursor
  entry spanning the turn, text deltas derived from the
  returned text, and zero tool-call records. The turn
  completes successfully and the `workflow_node` cursor is
  marked `committable=True` after the turn ends.
- [ ] **AC2.6f** `GenericWorkflowBridge` deep mode. A user
  workflow implementing
  `on_user_turn(text, *, recorder, cancel_token)` that calls
  `recorder.record_unit_entered`, `recorder.record_tool_call`,
  `recorder.record_framework_handoff`, and
  `recorder.record_unit_exited` from inside its own code
  produces a journal with all those records nested beneath the
  outer `workflow_node` cursor. Signature inspection correctly
  routes the workflow to deep mode.
- [ ] **AC2A.7** Coarse interruption parity. The pre-existing
  `tests/session/` barge-in test suite passes unmodified with
  the single-phase `apply_interruption` implementations on all
  three bridges. This is not the full three-cancellation-mode
  test matrix (that lives in WS2B AC2B.7); it is the guarantee
  that existing end-to-end barge-in behavior does not regress
  during WS2A.
- [ ] **AC2A.7b** `ShallowModeInterruptionError` is raised and
  surfaced correctly at the bridge boundary. A dedicated test
  constructs a `GenericWorkflowBridge` around a shallow workflow
  without `apply_interruption`, calls
  `bridge.apply_interruption(...)` directly, asserts the
  exception and its message (which must name the deep-mode
  upgrade path). Controller-side downgrade handling is a WS2B
  concern.
- [ ] **AC2.8** Model-request nodes in PydanticAI are marked
  `committable=False` while streaming and `committable=True` between
  turns.
- [ ] **AC2.10** No new tool abstraction, registry, decorator, or
  proxy exists in `src/easycat/` — guardrail test passes. The
  test greps for an expanded pattern set including
  `easycat_tool`, `@register_*`, `class .*Registry`,
  `class .*Router` to catch easy-to-miss variants. The guardrail
  is documented as a smoke check, not a substitute for code
  review. This guardrail runs in WS2A because the temptation to
  add tool abstractions can arise in bridge design, not just in
  MCP wiring.
- [ ] **AC2.11** `auto_adapt_agent()` still constructs the correct
  bridge when handed an unknown but duck-typed agent object.

> **MCP pass-through ACs (`EasyCatConfig(mcp_servers=[...])`,
> per-bridge forwarding, mock + filesystem integration tests)
> live in Workstream 2B AC2B.9.**
- [ ] **AC2.12** All existing `tests/agents/` and `tests/session/`
  tests pass without modification.
- [ ] **AC2.13** `FrameworkStateSnapshot` values are JSON-safe,
  secret-safe, and contain no raw framework handles or credentials.
  Additional sub-tests:
  - Every snapshot whose serialized `fields` dict exceeds 4 KB
    must set `state_ref` and leave only a summary in `fields`.
    A test constructs a bridge producing an intentionally large
    snapshot and asserts the overflow policy fires.
  - `state_ref` format validation: every non-null `state_ref`
    matches `^[a-f0-9]{64}$` (SHA-256 hex).
  - `kind` field is non-empty and routes to a known bridge type.
- [ ] **AC2.13a** `AgentRecorder.context.mcp_servers` is
  populated at recorder construction time from
  `EasyCatConfig.mcp_servers` and is read-only. A test
  instantiates a recorder with a stub config containing
  `mcp_servers=["stdio://foo", "sse://bar"]`, passes it to each
  bridge's `invoke()`, and asserts the context is visible with
  matching values on every bridge. A second sub-test asserts
  `context` is frozen (`dataclasses.FrozenInstanceError` on any
  mutation attempt).
- [ ] **AC2.13b** `AgentRecorder.unit()` context manager
  guarantees paired enter/exit on exception. Test runs a
  deep-mode `GenericWorkflowBridge` workflow that raises
  mid-turn inside a `with recorder.unit(cursor):` block,
  asserts:
  - the journal contains the matching `record_unit_exited`
    record with `reason="exception:<ErrorType>"`
  - the exception is re-raised to the caller
  - no orphan `FrameworkUnitEntered` without a matching
    `FrameworkUnitExited` appears in the journal
- [ ] **AC2.13c** `RecorderInvariantError` catches obvious
  deep-mode bugs. Three sub-tests: (1) calling
  `record_unit_exited(cursor)` without a matching enter raises;
  (2) exiting cursor B when cursor A is still open raises;
  (3) two cursors with the same `unit_id` within a turn raise.
- [ ] **AC2.14** Any public config/construction changes introduced here
  (`mcp_servers`, bridge construction, `AgentRunner` migration path,
  adapter naming) are frozen in the RFC and covered by migration notes.
- [ ] **AC2.15** Voice-to-voice / realtime guardrail test. A grep-
  based test asserts zero matches in `src/easycat/` for
  `RealtimeBridge`, `realtime_session`, `RealtimeStage`, or any
  import of `src/easycat/stt/openai_realtime_provider.py` from
  outside the STT layer itself. The STT-layer provider stays
  (it predates the redesign and serves chained STT), but nothing
  in the bridge, stage, or session layers may build a realtime
  mode on top of it.
- [ ] **AC2.16** Each of the three shipped bridges
  (`OpenAIAgentsBridge`, `PydanticAIBridge`,
  `GenericWorkflowBridge`) publishes a static
  `COMMITTABLE_BOUNDARIES` mapping. A parametrized test over all
  bridges asserts the mapping is present, non-empty, and covers
  every `unit_kind` the bridge emits. `PydanticAIBridge`'s
  mapping includes both Agent-mode and Graph-mode kinds (the
  bridge is one class regardless of input mode). Workstream 4
  imports these mappings by reference, not by re-declaration.
- [ ] **AC2.17** Every handoff produces the `FrameworkUnitExited`
  → `FrameworkHandoff` → `FrameworkUnitEntered` triple on the
  journal timeline in strictly increasing sequence, with matching
  `from_unit`/`to_unit` across the three records and no
  interleaved records from the same turn between them.
- [ ] **AC2.18** `FrameworkStateSnapshot` values for every bridge
  are constructed by passing through the WS1 T1.5
  `apply_write_filter` hook and honor the hard-coded safe default
  (no raw API keys, auth headers, or env dumps reach the journal).
  A test injects a known API-key-shaped string into a framework
  snapshot field and asserts the string does not reach the journal
  backend. Per-field `RedactionPolicy` coverage is out of scope
  here and lives in `peripheral-redaction.md`.
- [ ] **AC2.19** `AgentTurnInput.from_text(text, context=...)`
  constructs a valid turn input without requiring STT output or a
  voice pipeline. A test calls it directly, passes the result to
  `bridge.invoke()` on every bridge (`OpenAIAgentsBridge`,
  `PydanticAIBridge` Agent mode, `PydanticAIBridge` Graph mode,
  `GenericWorkflowBridge` shallow, `GenericWorkflowBridge` deep),
  and asserts the bridge emits the normal `AgentBridgeEvent`
  stream and a consistent journal record sequence.

## Verification

| AC | Verification |
|---|---|
| AC2.1 | Git log shows RFC merge commit. |
| AC2.2 | `python -c "from easycat.integrations.agents.base import ExternalAgentBridge, AgentTurnInput, ExecutionCursor, CancellationMode"` exits 0. |
| AC2.3 | New test `test_transition_record_types_exist` — instantiates each of the seven record types with minimal fields. |
| AC2.4 | New test `test_all_bridges_implement_protocol` — asserts each of the three bridge classes (`OpenAIAgentsBridge`, `PydanticAIBridge`, `GenericWorkflowBridge`) passes `isinstance(..., ExternalAgentBridge)`. Additional sub-tests: `PydanticAIBridge(agent=...)` and `PydanticAIBridge(graph=..., state_factory=..., initial_node_factory=...)` both construct successfully; `PydanticAIBridge(agent=..., graph=...)` and `PydanticAIBridge()` both raise `BridgeInputError`. |
| AC2.5 | New test `test_openai_agents_handoff_recorded` — runs a two-agent OpenAI Agents setup that handoffs mid-turn, filters the journal for `FrameworkHandoff` records, asserts `from_unit != to_unit` and both match the expected agent names. |
| AC2.6 | New test `test_pydantic_ai_iter_node_transitions` — runs a PydanticAI agent with tool calls, asserts paired `FrameworkUnitEntered` / `FrameworkUnitExited` records for each `model_node` / `tool_call` / `user_prompt` node, plus `record_tool_call` entries for every `FunctionToolCallEvent` and `FunctionToolResultEvent`. |
| AC2.6a | New test `test_pydantic_ai_graph_mode_tool_call_depth` — builds a two-node `pydantic_graph.Graph` (node A calls agent X which invokes a tool, node B calls agent Y which invokes a different tool), runs one turn via `PydanticAIBridge(graph=...)`, walks the journal, asserts the nested sequence: `workflow_node(A)` entered → `agent(X)` entered → `tool_call(X.tool)` start/result → `agent(X)` exited → `workflow_node(A)` exited → `FrameworkHandoff(A→B, graph_transition)` → `workflow_node(B)` entered → `agent(Y)` entered → `tool_call(Y.tool)` start/result → `agent(Y)` exited → `workflow_node(B)` exited. Asserts every tool call carries the same fields as AC2.6. |
| AC2.6b | New test `test_pydantic_ai_graph_mode_history_artifact` — runs a three-node graph turn, asserts the journal contains an artifact ref for `run.result.history`, asserts the snapshot's `active_node` matches the last node class name in that history. |
| AC2.6c | New test `test_pydantic_ai_graph_mode_state_artifact_ref` — constructs a graph whose state dataclass contains a 500KB field, runs one turn, inspects the resulting journal record, asserts inline size is < 4KB and the state content is accessible via `snapshot_state.state_ref` in the artifact store. |
| AC2.6d | Two new tests. `test_pydantic_ai_graph_mode_missing_convention_slot` — constructs a `state_factory` that returns a state object without an `_easycat_event_handler` attribute, asserts `PydanticAIBridge(graph=..., state_factory=...)` raises `BridgeConfigurationError` at construction time with a message naming the convention and pointing at the bridge README. `test_pydantic_ai_graph_mode_convention_not_honored` — constructs a graph whose state has the `_easycat_event_handler` slot but whose nodes deliberately omit `event_stream_handler=...` when calling agents, runs one turn, asserts `ConventionViolationError` is raised at end-of-turn (hard error, not a warning record). |
| AC2.6e | New test `test_generic_workflow_shallow_mode` — builds a user workflow with only `on_user_turn(text) -> str`, wraps it in `GenericWorkflowBridge`, runs one turn, asserts the journal contains exactly one `workflow_node` cursor entry spanning the turn, text deltas matching the returned string, and zero `tool_call` records. Second sub-test uses the streaming variant `on_user_turn_streaming(text)` and asserts per-chunk text deltas. |
| AC2.6f | New test `test_generic_workflow_deep_mode` — builds a user workflow with `on_user_turn(text, *, recorder, cancel_token)` that emits `record_unit_entered` / `record_tool_call` / `record_framework_handoff` / `record_unit_exited` from inside its own code, wraps it in `GenericWorkflowBridge`, runs one turn, asserts the journal contains the outer `workflow_node` cursor plus all the nested records the user emitted. Third sub-test: signature inspection correctly routes a workflow without the `recorder` parameter to shallow mode and a workflow with it to deep mode. |
| AC2A.7 | Existing `tests/session/` barge-in test suite runs unmodified against the WS2A bridges and passes. Single-phase `apply_interruption` implementations must handle the end-to-end paths these tests exercise. The detailed per-cancellation-mode and journal-atomicity tests land in WS2B. |
| AC2A.7b | New test `test_shallow_mode_interruption_raises` — constructs `GenericWorkflowBridge` around a shallow workflow without `apply_interruption`, calls `bridge.apply_interruption("hello", mode=CancellationMode.immediate_stop)` directly, asserts `ShallowModeInterruptionError` with the expected message. |
| AC2.8 | New test `test_pydantic_ai_committable_flag_during_stream` — observes the execution cursor at four points in a turn (idle, model streaming, between tool calls, after final output) and asserts `committable` flips correctly. |
| AC2.10 | Grep-based test `test_no_easycat_native_tool_code` — asserts zero matches in `src/easycat/` for `@tool`, `@easycat_tool`, `@register_*`, `class .*Registry`, `class .*Router`, `class ToolRegistry`, `class MCPClient`, or `def register_tool`. |
| AC2.11 | New test `test_auto_adapt_agent_bridge_selection` — passes in synthetic duck-typed objects and asserts the correct bridge is constructed: `openai_agents.Agent` → `OpenAIAgentsBridge`; `pydantic_ai.Agent` → `PydanticAIBridge` Agent mode; object with `on_user_turn(text)` → `GenericWorkflowBridge`; object with `on_user_turn(text, *, recorder)` → `GenericWorkflowBridge` (signature inspection routes to deep mode internally). Separate sub-tests: `pydantic_graph.Graph` raises `BridgeInputError` naming the explicit `PydanticAIBridge(graph=..., state_factory=..., initial_node_factory=...)` constructor; realtime-API-shaped object raises `BridgeInputError` pointing the user at the provider SDK. |
| AC2.12 | `uv run pytest tests/agents/ tests/session/` exits 0. |
| AC2.13 | New test `test_framework_state_snapshot_is_safe_and_serializable` — exports a snapshot from each bridge, asserts JSON serialization succeeds and no banned secret-bearing fields or raw framework objects are present. |
| AC2.14 | RFC + migration note include before/after examples covering bridge construction, `auto_adapt_agent()`, and any config-field changes introduced by this workstream. |
| AC2.15 | Grep-based test `test_no_realtime_bridge_surface` — asserts zero matches in `src/easycat/integrations/`, `src/easycat/stages/`, `src/easycat/session/`, and `src/easycat/runtime/` for `RealtimeBridge`, `realtime_session`, or `RealtimeStage`. The existing `src/easycat/stt/openai_realtime_provider.py` is allowed (it is a chained STT provider that happens to use the Realtime API's websocket transport) but must not be imported from outside the STT layer. |
| AC2.16 | New test `test_committable_boundaries_published` — parametrized over all three bridges. For `PydanticAIBridge` the test covers both Agent-mode and Graph-mode unit kinds. Asserts `COMMITTABLE_BOUNDARIES` is present, non-empty, and covers every `unit_kind` the bridge's cursor model emits. |
| AC2.17 | New test `test_handoff_record_triple` — runs a two-agent OpenAI Agents handoff, filters journal records for the turn, asserts the exit → handoff → enter triple is present in sequence with matching `unit_id` values and no interleaved records from the same turn. |
| AC2.18 | New test `test_framework_snapshot_safe_default_path` — injects a known API-key-shaped string into a framework snapshot field, exports the snapshot, greps the journal backend and artifact store for the string, asserts zero hits. Runs for every bridge. Verifies every bridge routes through the WS1 `apply_write_filter` hook. |
| AC2.19 | New test `test_agent_turn_input_from_text_direct_invoke` — calls `AgentTurnInput.from_text("hello", context=[])`, passes it to `bridge.invoke()` directly (no Session, no voice pipeline), asserts the bridge emits `AgentBridgeEvent`s and a consistent journal record sequence. Parametrized over `OpenAIAgentsBridge`, `PydanticAIBridge` Agent mode, `PydanticAIBridge` Graph mode, `GenericWorkflowBridge` shallow, and `GenericWorkflowBridge` deep. |

## Risks and Mitigations

- **Framework APIs may not expose all execution cursor info**:
  mitigation — fall back to best-effort metadata with explicit
  `display_name=None`, log warnings once per session, document known
  gaps in bridge-specific README. Do not invent synthetic values.
- **MCP pass-through semantics differ between frameworks**:
  mitigation — keep the pass-through bridge-specific. Do not normalize
  into a common shape. The `mcp_servers` list is the only common
  surface; everything else is framework-internal.
- **`auto_adapt_agent()` duck-typing may misidentify an agent**:
  mitigation — add an explicit `isinstance()` check before duck-typing,
  with an `EASYCAT_E0xx` error when identification fails, pointing the
  user at explicit bridge construction.
- **Interruption state machine is subtle**: mitigation — the existing
  barge-in torture tests in `tests/session/` are the safety net. Do
  not modify them. If any start failing, stop and debug rather than
  updating expectations.
- **`PydanticAIBridge` couples to two moving upstream surfaces**
  (`pydantic_ai` agent internals and `pydantic_graph` graph
  internals): mitigation — pin minimum compatible versions of
  both packages in `pyproject.toml`, centralize event-mapping in
  `_pydantic_ai_events.py` so upstream event-taxonomy changes are
  fixed in one place, and document pinned versions in the bridge
  README. Unifying Agent and Graph modes in one bridge class is
  deliberate: they share inner event handling, so upstream churn
  that affects the event taxonomy fixes both modes at once.
- **Graph authors may skip the event-handler convention**:
  mitigation — AC2.6d requires a warning journal record on any
  turn where no per-agent events are captured, with a message
  naming the convention. `easycat doctor` surfaces the same
  warning on startup if it detects a graph whose state class
  lacks the expected convention hook.
- **`pydantic_graph` state objects can be arbitrarily large or
  contain non-serializable fields**: mitigation — always store
  state snapshots via artifact ref (never inline), walk the
  state's fields via `dataclasses.fields()` when building the
  snapshot and skip any field that fails JSON serialization,
  logging the skipped field names to a snapshot warning record.
- **`GenericWorkflowBridge` shallow mode gives users a false
  sense of debuggability**: mitigation — the bridge README,
  `easycat doctor`, and the shallow-mode warning on
  `mcp_servers=[...]` all explicitly document what shallow mode
  does and does not capture (only text output, no tool calls, no
  sub-agent handoffs). The opt-in deep mode is a one-parameter
  change away, documented alongside every shallow example in the
  WS2 appendix.
- **`GenericWorkflowBridge` deep mode users may emit malformed
  record sequences**: mitigation — `AgentRecorder` validates
  basic invariants at each call (e.g., `record_unit_exited`
  without a matching `record_unit_entered` raises a clear error;
  cursor `unit_id` conflicts raise a clear error). Deep mode
  trusts users with their orchestration but catches obvious bugs
  at the recorder boundary rather than letting them corrupt the
  journal.

## Handoff to Next Workstreams

When this workstream is complete, **Workstream 2B (Interruption and
MCP)** inherits:

- three shipped bridges implementing `ExternalAgentBridge` with
  **single-phase** `apply_interruption` (framework mutation
  without the journal-atomicity clause). WS2B replaces these
  in-place with the four-step atomic write ordering.
- `InterruptionApplyFailed` record type defined but not yet
  emitted (WS2B emits it on mutation failure)
- `RecorderContext.mcp_servers` field defined but populated
  empty (WS2B populates it from `EasyCatConfig.mcp_servers`)
- `ShallowModeInterruptionError` raised at the bridge boundary
  (WS2B adds controller-side downgrade handling in WS3 once
  `InterruptionController` exists)
- `COMMITTABLE_BOUNDARIES` published per bridge (WS2B tests
  drain-to-commit-point cancellation against these mappings)

**Workstream 3 (Stage Refactor)** inherits:

- three shipped bridges ready to be wrapped as `AgentStage`
- transition records flowing into the journal, which will populate
  state snapshots at stage boundaries
- `AgentRecorder` with `unit()` context manager, `context`
  side-channel, and invariant enforcement
- bridge committable enumeration, consumed by `AgentStage`'s
  `snapshot_state` path

**Workstream 4 (Replay)** inherits the execution cursor's
`committable` flag via the `COMMITTABLE_BOUNDARIES` mappings,
which define what counts as a valid fork boundary in each bridge.

## Appendix: Bridge Implementation Examples

These examples are the canonical reference for each supported
integration path. They are meant to be checked against the final
implementation — if any example stops working, either the example
is wrong or the bridge is wrong, and both must be resolved before
WS2 closes. Each example is deliberately small (≤ 80 lines) and
shows the full wiring including `EasyCatConfig` construction so
users can copy-paste as a starting point.

The examples also ship as test fixtures under
`tests/integrations/agents/examples/` (one file per example) that
run end-to-end in CI, which guarantees they cannot drift from
reality between the plan doc and the implementation.

### Example 1: `PydanticAIBridge` wrapping a plain `pydantic_ai.Agent`

The simplest PydanticAI path. One agent, one model, any number of
tools via `@agent.tool`. Zero custom bridge code; the bridge
captures every `PartDeltaEvent`, `FunctionToolCallEvent`,
`FunctionToolResultEvent`, and `FinalResultEvent` automatically.

```python
from datetime import date

from pydantic_ai import Agent, RunContext

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import PydanticAIBridge


weather_agent = Agent(
    "openai:gpt-5.2",
    system_prompt="Provide weather forecasts for locations the user asks about.",
)


@weather_agent.tool
async def weather_forecast(
    ctx: RunContext[None], location: str, forecast_date: date
) -> str:
    # In real code: call a weather API.
    return f"The forecast in {location} on {forecast_date} is 24°C and sunny."


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=PydanticAIBridge(agent=weather_agent),
    mcp_servers=["stdio://mcp-filesystem"],  # forwarded to the agent
)
session = create_session(config)
```

Journal output per turn: `agent` cursor entered → `user_prompt`
node → `model_node` with streamed text deltas → `tool_call` node
with `FunctionToolCallEvent(weather_forecast, ...)` and
`FunctionToolResultEvent(...)` → another `model_node` for the
final response → `FinalResultEvent` → `agent` cursor exited. Every
delta, every tool call, every token is captured.

### Example 2: `PydanticAIBridge` wrapping a `pydantic_graph.Graph`

Multi-agent workflow via `pydantic_graph`. Two specialist agents
coordinated by a two-node graph with shared state. The key piece
of workflow-author code is the `event_stream_handler=` argument on
each `agent.run(...)` call — this is the one-line convention that
gives the bridge deep per-agent event capture.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import PydanticAIBridge


# Two specialist agents.
research_agent = Agent(
    "openai:gpt-5.2",
    system_prompt="Research the user's question and return 3 bullet points.",
)

writer_agent = Agent(
    "openai:gpt-5.2",
    system_prompt="Write a concise spoken summary from the research bullets.",
)


@research_agent.tool
async def search_docs(ctx: RunContext[None], query: str) -> str:
    # In real code: call a search API.
    return f"Results for {query}: (stub)"


# Shared state. The `_easycat_event_handler` slot is the convention
# the bridge installs; workflow nodes pass it into agent calls.
@dataclass
class WorkflowState:
    research_bullets: str = ""
    final_text: str = ""
    _easycat_event_handler: Any = None  # bridge installs this on each turn


@dataclass
class ResearchNode(BaseNode[WorkflowState]):
    user_text: str

    async def run(self, ctx: GraphRunContext[WorkflowState]) -> WriteNode:
        result = await research_agent.run(
            self.user_text,
            event_stream_handler=ctx.state._easycat_event_handler,  # ← deep capture
        )
        ctx.state.research_bullets = result.output
        return WriteNode()


@dataclass
class WriteNode(BaseNode[WorkflowState, None, str]):
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> End[str]:
        result = await writer_agent.run(
            f"Research:\n{ctx.state.research_bullets}\n\nWrite a spoken summary.",
            event_stream_handler=ctx.state._easycat_event_handler,  # ← deep capture
        )
        ctx.state.final_text = result.output
        return End(result.output)


research_graph = Graph(nodes=[ResearchNode, WriteNode])


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=PydanticAIBridge(
        graph=research_graph,
        state_factory=WorkflowState,
        initial_node_factory=lambda text, state: ResearchNode(user_text=text),
        agents=[research_agent, writer_agent],  # explicit list for MCP forwarding
    ),
    mcp_servers=["stdio://mcp-filesystem"],
)
session = create_session(config)
```

Journal output per turn: `workflow_node(ResearchNode)` entered →
nested `agent(research_agent)` entered → `model_node` with text
deltas → `tool_call(search_docs)` with
`FunctionToolCallEvent`/`Result` → `agent(research_agent)`
exited → `workflow_node(ResearchNode)` exited →
`FrameworkHandoff(ResearchNode → WriteNode, graph_transition)` →
`workflow_node(WriteNode)` entered → nested `agent(writer_agent)`
entered → `model_node` with text deltas → `agent(writer_agent)`
exited → `workflow_node(WriteNode)` exited. Same event depth as
Example 1 for every agent call, with graph-node context layered
on top.

### Example 3: `GenericWorkflowBridge` in shallow mode

Use this when you have custom orchestration code and don't want to
expose its internals to the journal. The protocol is one method:
`on_user_turn(text) -> str`. No recorder, no bridge-specific
concepts. Existing orchestration classes usually match this
protocol with a minor rename.

```python
import asyncio

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import GenericWorkflowBridge


class SupportOrchestrator:
    """Custom multi-agent orchestration. Could use any backend internally."""

    def __init__(self) -> None:
        self._history: list[tuple[str, str]] = []

    async def on_user_turn(self, text: str) -> str:
        # Application-level routing, hand-offs, tool calls happen here.
        # The bridge sees only the final string; it does not see the
        # internal structure of this method.
        response = await self._dispatch(text)
        self._history.append((text, response))
        return response

    async def _dispatch(self, text: str) -> str:
        # In real code: call LLM APIs, run tools, coordinate specialists.
        return f"I'll help you with: {text}"

    def reset(self) -> None:
        self._history.clear()


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=GenericWorkflowBridge(workflow=SupportOrchestrator()),
)
session = create_session(config)
```

Journal output per turn: one `workflow_node` cursor spanning the
whole turn, text deltas reconstructed from the returned string,
zero tool-call records. The debugger sees turn boundaries and
timing but not the orchestrator's internal structure. If you pass
`mcp_servers=[...]` to this config, the bridge logs a warning at
startup because shallow mode cannot forward MCP to an opaque
backend.

### Example 4: `GenericWorkflowBridge` in deep mode

Same `GenericWorkflowBridge`, but the workflow now accepts a
`recorder` parameter and calls its methods directly to emit
structured records from inside its own orchestration code. Useful
when you want debugger visibility into your custom logic without
rewriting it against `pydantic_ai.Agent` or `pydantic_graph`.

```python
import asyncio
import time
from collections.abc import AsyncIterator

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.cancel import CancelToken
from easycat.integrations.agents import (
    AgentRecorder,
    ExecutionCursor,
    GenericWorkflowBridge,
)


class SupportOrchestrator:
    """Custom orchestration with opt-in journal visibility."""

    def __init__(self) -> None:
        self._history: list[tuple[str, str]] = []

    async def on_user_turn(
        self,
        text: str,
        *,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[str]:
        # Step 1: intent classification.
        intent_cursor = ExecutionCursor(
            unit_id="intent-classifier",
            unit_kind="specialist",
            display_name="IntentClassifier",
            parent_unit_id=None,
            sequence=1,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(intent_cursor)
        intent = await self._classify_intent(text)
        recorder.record_unit_exited(intent_cursor, reason="classified")

        # Step 2: tool call if the intent needs it.
        if intent == "weather":
            recorder.record_tool_call(
                phase="start",
                name="get_weather",
                args_ref=None,
                result_ref=None,
            )
            result = await self._get_weather(text)
            recorder.record_tool_call(
                phase="result",
                name="get_weather",
                args_ref=None,
                result_ref=None,
            )
        else:
            result = "I can't help with that yet."

        # Step 3: stream the response text.
        response_cursor = ExecutionCursor(
            unit_id="response-writer",
            unit_kind="agent",
            display_name="ResponseWriter",
            parent_unit_id=None,
            sequence=2,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(response_cursor)
        for word in result.split():
            if cancel_token and cancel_token.is_cancelled():
                break
            yield word + " "
        recorder.record_unit_exited(
            response_cursor.with_committable(True),
            reason="stream_complete",
        )

        self._history.append((text, result))

    async def _classify_intent(self, text: str) -> str:
        return "weather" if "weather" in text.lower() else "other"

    async def _get_weather(self, text: str) -> str:
        return "It's 24°C and sunny."

    def reset(self) -> None:
        self._history.clear()


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=GenericWorkflowBridge(workflow=SupportOrchestrator()),
)
session = create_session(config)
```

Signature inspection picks deep mode because `on_user_turn` has a
`recorder` parameter. Journal output per turn: outer `workflow`
cursor → `specialist(IntentClassifier)` entered/exited →
`tool_call(get_weather)` start/result → `agent(ResponseWriter)`
entered → text deltas per yielded chunk → `agent` exited → outer
`workflow` cursor exited. The user code controls which units are
visible and at what granularity; the bridge trusts the user with
orchestration semantics and validates basic invariants (paired
enter/exit, no duplicate `unit_id`) at the recorder boundary.

### Example 5: Custom `ExternalAgentBridge` from scratch

For users whose agent does not fit any of the above (custom
inference backend, unusual streaming protocol, tight integration
with a non-Python runtime, etc.). Maximum control, maximum code.
Only reach for this when none of the shipped bridges fit your
shape.

```python
import time
from collections.abc import AsyncIterator

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.cancel import CancelToken
from easycat.integrations.agents import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ErrorInfo,
    ExecutionCursor,
    FrameworkStateSnapshot,
)

import openai  # or any inference backend


class DirectOpenAIChatBridge:
    """Minimal custom bridge: OpenAI Chat Completions with no framework."""

    COMMITTABLE_BOUNDARIES = {"agent": CommitRule.BETWEEN_TURNS}

    def __init__(self, client: openai.AsyncOpenAI, *, model: str, system: str):
        self._client = client
        self._model = model
        self._system = system
        self._history: list[dict[str, str]] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        cursor = ExecutionCursor(
            unit_id=f"turn-{turn_input.turn_id}",
            unit_kind="agent",
            display_name="DirectOpenAIChat",
            parent_unit_id=None,
            sequence=len(self._history) + 1,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(cursor)

        self._history.append({"role": "user", "content": turn_input.text})
        messages = [{"role": "system", "content": self._system}, *self._history]

        try:
            stream = await self._client.chat.completions.create(
                model=self._model, messages=messages, stream=True
            )
            full = ""
            async for chunk in stream:
                if cancel_token and cancel_token.is_cancelled():
                    break
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full += delta
                    yield AgentBridgeEvent(type="text_delta", text=delta)

            self._history.append({"role": "assistant", "content": full})
            recorder.record_unit_exited(
                cursor.with_committable(True), reason="stream_complete"
            )
            yield AgentBridgeEvent(type="done", text=full)
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(cursor, reason="error")
            raise

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "model": self._model,
                "history": list(self._history),
                "turn_count": len(self._history) // 2,
            }
        )

    def apply_interruption(
        self, delivered_text: str, mode: CancellationMode
    ) -> None:
        if self._history and self._history[-1]["role"] == "assistant":
            if delivered_text:
                self._history[-1]["content"] = delivered_text + "..."
            else:
                self._history.pop()

    def reset(self) -> None:
        self._history.clear()


bridge = DirectOpenAIChatBridge(
    openai.AsyncOpenAI(),
    model="gpt-5.2",
    system="You are a helpful voice assistant.",
)
config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=bridge,
)
session = create_session(config)
```

Journal output per turn: `agent(DirectOpenAIChat)` entered → text
deltas per OpenAI chunk → `agent` exited with `committable=True`.
Interruption patches the last assistant message. No tool calls
unless you add them yourself via `recorder.record_tool_call(...)`
in your streaming loop. MCP is not supported out of the box — if
you need it, either use `PydanticAIBridge` with a `pydantic_ai.Agent`
or implement MCP client logic yourself using the `mcp` Python SDK
and emit `record_tool_call` events from your own dispatch code.

### Example 6: `OpenAIAgentsBridge` reference usage

For completeness, the OpenAI Agents SDK path. Shape is nearly
identical to Example 1 — one agent, tools via the SDK, zero custom
bridge code.

```python
from agents import Agent, RunContext, function_tool

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import OpenAIAgentsBridge


@function_tool
async def get_time(city: str) -> str:
    return f"The time in {city} is 3:47 PM."


support_agent = Agent(
    name="SupportAgent",
    instructions="Help the user with time zone questions.",
    tools=[get_time],
)


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=OpenAIAgentsBridge(agent=support_agent),
    mcp_servers=["stdio://mcp-filesystem"],
)
session = create_session(config)
```

Journal output per turn: `agent(SupportAgent)` entered → model
stream → `tool_call(get_time)` start/result → `agent` exited. The
OpenAI Agents SDK's handoff support adds `FrameworkHandoff` triples
between sub-agents when they fire, same as `PydanticAIBridge`
Graph mode adds them between `pydantic_graph` nodes.

### Choosing a path

| Your situation | Use |
|---|---|
| Single `pydantic_ai.Agent` | `PydanticAIBridge(agent=...)` |
| `pydantic_graph.Graph` multi-agent workflow | `PydanticAIBridge(graph=..., state_factory=..., initial_node_factory=...)` |
| OpenAI Agents SDK | `OpenAIAgentsBridge(agent=...)` |
| Custom orchestration, want minimal effort | `GenericWorkflowBridge` shallow (`on_user_turn(text)`) |
| Custom orchestration, want journal visibility | `GenericWorkflowBridge` deep (`on_user_turn(text, *, recorder)`) |
| Totally custom inference backend | Implement `ExternalAgentBridge` from scratch (Example 5) |

The shipped bridges cover the common cases. The custom bridge
path (Example 5) exists as an escape hatch for the uncommon ones
and is documented prominently so users know the full protocol is
accessible when they need it.
