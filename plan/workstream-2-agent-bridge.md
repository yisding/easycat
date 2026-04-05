# Workstream 2: Agent Bridge Layer

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstream 1 (Journal Foundation) must be complete.
> **Successors**: Workstream 3 (Stage Refactor) wraps the bridge as
> `AgentStage`.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-4-replay-and-bundle.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may change agent-facing config and
> construction APIs if needed, but every such change must be frozen in the
> RFC and included in the migration guide.

## Goal

Replace the runner-centric adapter flow with an `ExternalAgentBridge`
protocol that exposes framework execution state (handoffs, tool calls,
node transitions) as first-class journal records, and validate the
boundary correctness by making MCP pass-through work without any
EasyCat-native tool code.

## Scope

**In scope:**

- `ExternalAgentBridge` protocol and shared types
- `OpenAIAgentsBridge` port (from `OpenAIAgentsAdapter`)
- `PydanticAIBridge` port (from `PydanticAIAdapter`)
- `PydanticAIWorkflowBridge` port (from `PydanticAIWorkflowAdapter`)
- Execution cursor model
- Framework transition records
- Secret-safe framework state snapshots
- Interruption and cancellation contract with three boundary modes
- Voice state vs framework state separation
- MCP pass-through on both bridges
- `auto_adapt_agent()` preserved as a facade

**Out of scope:**

- Stage model — bridge is still invoked from Session (Workstream 3)
- Replay semantics for agent stage (Workstream 4)
- Any EasyCat-native tool abstraction, registry, or decorator
  (permanent guardrail)

## Tasks

### T2.0: Architecture Freeze (RFC)

- [ ] Write Phase 2 RFC covering:
  - `ExternalAgentBridge` protocol signature
  - `AgentTurnInput`, `AgentRecorder`, `AgentBridgeEvent`,
    `FrameworkStateSnapshot` type definitions
  - public migration path from `AgentRunner`/adapter-centric usage to the
    bridge-centric runtime
  - Execution cursor fields and `committable` semantics per framework
  - Transition record catalog and field shapes
  - Interruption contract: the seven-step turn flow and three
    cancellation modes
  - MCP pass-through wiring per framework (OpenAI Agents
    `Agent(mcp_servers=...)` vs PydanticAI toolset adapter)
- [ ] Review and merge RFC before implementation.

### T2.1: Bridge Protocol and Shared Types

- [ ] Create `src/easycat/integrations/agents/base.py`
- [ ] Define `ExternalAgentBridge` Protocol with
  `invoke`, `snapshot_state`, `apply_interruption`, `reset`
- [ ] Define `AgentTurnInput`, `AgentRecorder`, `AgentBridgeEvent`
- [ ] Define `FrameworkStateSnapshot` dataclass
- [ ] `FrameworkStateSnapshot` must be JSON-safe, secret-safe, and use
  artifact refs for large or sensitive payloads rather than raw framework
  objects
- [ ] Define `ExecutionCursor` dataclass: `unit_id`, `unit_kind` (enum:
  `agent`, `specialist`, `workflow_node`, `model_node`, `tool_call`),
  `display_name`, `parent_unit_id`, `sequence`, `entered_at`,
  `committable`
- [ ] Define `CancellationMode` enum: `immediate_stop`,
  `drain_current_unit`, `drain_to_commit_point`

### T2.2: Transition Records

- [ ] Add transition record types to
  `src/easycat/runtime/records.py`:
  - `FrameworkUnitEntered`
  - `FrameworkUnitExited`
  - `FrameworkStateCommitted`
  - `FrameworkHandoff`
  - `FrameworkToolPhaseChanged`
  - `FrameworkCancellationBoundaryReached`
- [ ] Each extends `FrameworkTransitionRecord` from Workstream 1
- [ ] Include `from_unit`, `to_unit`, `transition_kind`, `reason`,
  `framework_metadata`, `state_snapshot_ref` fields

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

### T2.4: PydanticAI Bridge

- [ ] Create `src/easycat/integrations/agents/pydantic_ai.py`
- [ ] Port `src/easycat/agents/pydantic_ai.py` contents to a
  bridge-shaped class
- [ ] Capture and record:
  - input text and message history passed to the framework
  - `deps` and `model_settings`
  - streaming node events from `iter()`
  - tool call/result events
  - final output object
  - `new_messages()` history updates
- [ ] Treat `iter()` node changes as framework unit transitions
- [ ] Distinguish model-request nodes from tool-call nodes for
  cancellation safety (set `committable=False` on model-request nodes
  while streaming)
- [ ] `snapshot_state()` returns message history, deps/model_settings,
  workflow active node

### T2.5: PydanticAI Workflow Bridge

- [ ] Create `src/easycat/integrations/agents/workflows.py`
- [ ] Port `src/easycat/agents/pydantic_ai_workflow.py` contents
- [ ] Record workflow-managed specialist/node state separately from
  raw message history
- [ ] Report committed `active_agent_id` or node changes as explicit
  transition records
- [ ] Preserve workflow semantics — workflow logic stays in user code;
  bridge only records transitions

### T2.6: Interruption Contract Implementation

- [ ] Implement the seven-step turn flow for interruption:
  1. runtime detects interruption
  2. runtime computes delivered assistant text
  3. runtime selects a cancellation boundary
  4. runtime requests bridge cancellation/drain behavior
  5. runtime calls `apply_interruption(delivered_text, mode=...)`
  6. bridge updates framework-native state
  7. journal records voice event + framework-state mutation
- [ ] Test all three modes on both OpenAI Agents and PydanticAI

### T2.7: MCP Pass-Through

- [ ] Add `mcp_servers: list[str] | None = None` to `EasyCatConfig`
- [ ] OpenAI Agents bridge forwards to `Agent(mcp_servers=...)`
- [ ] PydanticAI bridge forwards to the agent's MCP toolset adapter
- [ ] MCP tool invocations flow through existing bridge tool-call
  events — no new record type
- [ ] Zero EasyCat-native tool registry, decorator, or proxy added
  (guardrail test below)

### T2.8: Facade Preservation

- [ ] `auto_adapt_agent()` still works — update it to construct the
  appropriate bridge based on duck-typed detection of the incoming
  agent object
- [ ] `auto_adapt_agent()` is documented as best-effort convenience, not
  a compatibility guarantee for every pre-redesign construction path

### T2.9: Test Migration

- [ ] All `tests/agents/` pass unmodified
- [ ] All `tests/session/` that exercise interruption pass unmodified
- [ ] Add new bridge-specific tests (see Verification)

## Acceptance Criteria

- [ ] **AC2.1** RFC reviewed and merged.
- [ ] **AC2.2** `src/easycat/integrations/agents/base.py` defines
  `ExternalAgentBridge` Protocol, `AgentTurnInput`, `AgentRecorder`,
  `AgentBridgeEvent`, `FrameworkStateSnapshot`, `ExecutionCursor`,
  `CancellationMode`.
- [ ] **AC2.3** Six transition record types exist in `records.py`.
- [ ] **AC2.4** `OpenAIAgentsBridge`, `PydanticAIBridge`, and
  `PydanticAIWorkflowBridge` exist and all implement
  `ExternalAgentBridge`.
- [ ] **AC2.5** Every committed handoff in OpenAI Agents produces a
  `FrameworkHandoff` record in the journal with correct `from_unit`
  and `to_unit` values.
- [ ] **AC2.6** Every `iter()` node change in PydanticAI produces a
  `FrameworkUnitEntered` / `FrameworkUnitExited` pair with correct
  `unit_kind`.
- [ ] **AC2.7** Interruption in all three cancellation modes works on
  both frameworks and is recorded in the journal with the matching
  `CancellationMode`.
- [ ] **AC2.8** Model-request nodes in PydanticAI are marked
  `committable=False` while streaming and `committable=True` between
  turns.
- [ ] **AC2.9** `EasyCatConfig(mcp_servers=[...])` passes through to
  the underlying framework. An end-to-end test with the official MCP
  `filesystem` server exercises a tool call and the tool call appears
  in the journal through the existing tool-call event path.
- [ ] **AC2.10** No new tool abstraction, registry, decorator, or
  proxy exists in `src/easycat/` — guardrail test passes.
- [ ] **AC2.11** `auto_adapt_agent()` still constructs the correct
  bridge when handed an unknown but duck-typed agent object.
- [ ] **AC2.12** All existing `tests/agents/` and `tests/session/`
  tests pass without modification.
- [ ] **AC2.13** `FrameworkStateSnapshot` values are JSON-safe,
  secret-safe, and contain no raw framework handles or credentials.
- [ ] **AC2.14** Any public config/construction changes introduced here
  (`mcp_servers`, bridge construction, `AgentRunner` migration path,
  adapter naming) are frozen in the RFC and covered by migration notes.

## Verification

| AC | Verification |
|---|---|
| AC2.1 | Git log shows RFC merge commit. |
| AC2.2 | `python -c "from easycat.integrations.agents.base import ExternalAgentBridge, AgentTurnInput, ExecutionCursor, CancellationMode"` exits 0. |
| AC2.3 | New test `test_transition_record_types_exist` — instantiates each of the six record types with minimal fields. |
| AC2.4 | New test `test_all_bridges_implement_protocol` — asserts each bridge class passes `isinstance(..., ExternalAgentBridge)` via the runtime-checkable protocol. |
| AC2.5 | New test `test_openai_agents_handoff_recorded` — runs a two-agent OpenAI Agents setup that handoffs mid-turn, filters the journal for `FrameworkHandoff` records, asserts `from_unit != to_unit` and both match the expected agent names. |
| AC2.6 | New test `test_pydantic_ai_iter_node_transitions` — runs a PydanticAI agent with tool calls, asserts paired `FrameworkUnitEntered` / `FrameworkUnitExited` records for each node. |
| AC2.7 | Three new tests, one per mode: `test_interruption_immediate_stop`, `test_interruption_drain_current_unit`, `test_interruption_drain_to_commit_point`. Each triggers a barge-in at a different point, asserts the bridge received the correct `CancellationMode`, and the journal records the mode on the cancellation boundary record. |
| AC2.8 | New test `test_pydantic_ai_committable_flag_during_stream` — observes the execution cursor at four points in a turn (idle, model streaming, between tool calls, after final output) and asserts `committable` flips correctly. |
| AC2.9 | New test `test_mcp_pass_through_filesystem_server` — configures `mcp_servers=["stdio://mcp-filesystem /tmp"]`, runs a turn that asks the agent to list `/tmp`, asserts the tool call appears in the journal as a standard tool-call event (no new record type) and the response includes directory contents. Runs against both OpenAI Agents and PydanticAI (parametrize). |
| AC2.10 | Grep-based test `test_no_easycat_native_tool_code` — asserts zero matches in `src/easycat/` for `@tool`, `class ToolRegistry`, `class MCPClient`, or `def register_tool`. |
| AC2.11 | New test `test_auto_adapt_agent_bridge_selection` — passes in a synthetic duck-typed object, asserts the correct bridge class is constructed. |
| AC2.12 | `uv run pytest tests/agents/ tests/session/` exits 0. |
| AC2.13 | New test `test_framework_state_snapshot_is_safe_and_serializable` — exports a snapshot from each bridge, asserts JSON serialization succeeds and no banned secret-bearing fields or raw framework objects are present. |
| AC2.14 | RFC + migration note include before/after examples covering bridge construction, `auto_adapt_agent()`, and any config-field changes introduced by this workstream. |

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
- **PydanticAI workflow bridge touches the most moving upstream
  surface**: mitigation — keep the workflow bridge as a thin wrapper
  that forwards to the existing workflow module. Do not absorb
  workflow semantics into the bridge.

## Handoff to Next Workstream

When this workstream is complete, Workstream 3 (Stage Refactor)
inherits:

- all three bridges implementing `ExternalAgentBridge`, ready to be
  wrapped as `AgentStage`
- transition records flowing into the journal, which will populate
  state snapshots at stage boundaries
- interruption contract fully specified, so `InterruptionController`
  extraction is a pure refactor rather than a rewrite
- MCP pass-through proven, so stage refactor does not accidentally
  reintroduce tool coupling

Workstream 4 (Replay) inherits the execution cursor's `committable`
flag, which defines what counts as a valid fork boundary in each
bridge.
