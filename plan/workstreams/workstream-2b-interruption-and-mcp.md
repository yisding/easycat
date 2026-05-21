# Workstream 2B: Interruption Contract and MCP Pass-Through

> **Status: historical acceptance record.** Bridge interruption/MCP tests and
> runtime wiring exist, but the current source does not contain a standalone
> `InterruptionController` file. Current interruption behavior is split across
> `CancelOrchestrator`, `session/interruption.py`, `TurnContext`, and
> `TurnRunner`.

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstream 1 (Journal Foundation) and Workstream 2A
> (Agent Bridge Protocol and Bridges) must be complete. Workstream 3's
> `InterruptionController` extraction is a **parallel dependency**: the
> full per-bridge cancellation-mode tests in this workstream require
> the controller to exist, so WS2B and WS3 land together rather than
> sequentially.
>
> **Merge ordering**: WS3 merges first (the real
> `InterruptionController` exists and the barge-in tests pass with
> it). WS2B merges second and replaces the stub controller with
> references to the real one. WS2B's CI runs against the WS3
> branch during development; the final merge is sequenced WS3 →
> WS2B within the same release. If WS2B is ready before WS3, it
> gates on WS3's controller extraction (T3.2) landing first.
> **Successors**: Workstream 4 (Replay and Bundle) consumes the
> committable-boundary semantics validated here.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2a-agent-bridges.md`
> - `workstream-2c-remote-bridge.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-4-replay-and-bundle.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: Backwards compatibility is not a goal of the
> essential redesign. This workstream may add or rename interruption and
> MCP-facing config fields and method signatures if needed. Every such
> change must be documented in the plan and included in the migration guide.

## Goal

Take the bridges shipped in WS2A, which currently perform single-phase
framework mutation on `apply_interruption`, and wrap them in the full
**four-step journal-atomicity clause**: plan the mutation, write
`FrameworkStateCommitted`, apply the mutation, emit the paired
`InterruptionApplyFailed` on failure or
`FrameworkCancellationBoundaryReached` on success. Validate all three
cancellation modes (`immediate_stop`, `drain_current_unit`,
`drain_to_commit_point`) end-to-end across every bridge/mode. Ship MCP
pass-through (`EasyCatConfig.mcp_servers`) on all MCP-capable bridges
without adding any EasyCat-native tool abstraction. This is the
workstream that validates the bridge boundary is correct — if MCP
pass-through cannot round-trip cleanly through the bridges, the
bridge design is wrong and must be fixed before moving on.

## Scope

**In scope:**

- Four-step atomic write ordering for `apply_interruption` on every
  bridge
- `InterruptionApplyFailed` record emission on mutation failure
- Three cancellation modes tested end-to-end per bridge/mode
- Shallow-mode `GenericWorkflowBridge` downgrade path (caught by
  `InterruptionController`, turn downgraded to end-of-turn
  interruption, journal records the downgrade)
- `EasyCatConfig.mcp_servers: list[str] | None = None` field (net
  new)
- MCP pass-through on `OpenAIAgentsBridge`,
  `PydanticAIBridge` Agent mode, `PydanticAIBridge` Graph mode,
  `GenericWorkflowBridge` deep mode
- `RecorderContext.mcp_servers` populated by the runtime from
  `EasyCatConfig.mcp_servers`
- Mock MCP server and filesystem-integration MCP tests
- Interruption contract test suite, including failure injection
  (controlled mutation failure after `FrameworkStateCommitted`
  write) to prove the atomicity clause works

**Depends on but does not own:**

- `InterruptionController` type and implementation — owned by
  WS3 T3.2. WS2B defines the bridge-side contract the controller
  calls into and ships failure-injection tests against a stub
  controller if the real one is not yet ready.
- `VoiceDeliveryLedger` — owned by WS3 T3.3. WS2B's seven-step
  flow reads delivered-text from it but does not define it.

**Out of scope:**

- Bridge protocol design (WS2A)
- Framework event capture, transition records, committable
  enumeration (WS2A)
- Stage model and Session decomposition (WS3)
- Any EasyCat-native tool abstraction, registry, decorator, or
  proxy (permanent guardrail)
- Voice-to-voice / realtime speech-to-speech bridges (permanent
  guardrail)

## Tasks

### T2B.0: Architecture Freeze

- [x] Design decisions covering:
  - Seven-step interruption turn flow (runtime → controller →
    bridge → journal). The complete sequence:
    1. **Detect** — VAD barge-in, concurrent `send_text`, or
       external cancel signal is observed by the runtime.
    2. **Signal** — `InterruptionController` emits a
       `ControlSignalRecord(signal_kind="interrupt")` to the
       journal with the originating `cause`.
    3. **Measure** — Controller reads delivered text from
       `VoiceDeliveryLedger` (exact in text mode, estimated
       in voice mode).
    4. **Select** — Controller selects a `CancellationMode`
       per the configured interruption policy.
    5. **Mutate** — Controller calls
       `bridge.apply_interruption(delivered_text, mode)`. The
       bridge runs the four-step atomic write ordering
       internally (see below).
    6. **Observe** — Controller reads the outcome: success
       (`FrameworkCancellationBoundaryReached` written),
       failure (`InterruptionApplyFailed` written → fall back
       to `immediate_stop`), or
       `ShallowModeInterruptionError` raised (downgrade to
       end-of-turn interruption per T2B.2).
    7. **Transition** — Controller cancels TTS playback,
       updates turn state, and either starts a new turn
       immediately or waits for end-of-turn depending on the
       outcome and cancellation mode.
  - Four-step atomic write ordering: plan mutation → write
    `FrameworkStateCommitted` → apply mutation → emit paired
    `InterruptionApplyFailed` or
    `FrameworkCancellationBoundaryReached`
  - Per-bridge implementation notes for the split (bridges must
    expose a `_plan_interruption(delivered_text, mode)` helper
    and a `_apply_planned_mutation(plan)` helper; public
    `apply_interruption` composes them with the journal writes
    between)
  - `ShallowModeInterruptionError` controller-side downgrade
    handling (the InterruptionController catches the exception
    and downgrades the turn to "end-of-turn interruption only",
    surfaced as a `ControlSignalRecord(signal_kind="interrupt",
    cause="shallow_mode_downgrade")`)
  - `EasyCatConfig.mcp_servers` field shape (list of URI strings
    supporting `stdio://`, `sse://`, `http://`, `https://`
    schemes; each entry's validation rules) and construction
    semantics
  - `RecorderContext.mcp_servers` population path: runtime
    reads `EasyCatConfig.mcp_servers`, freezes it into the
    `RecorderContext` passed to each `invoke()`
  - Per-bridge MCP forwarding wiring:
    - `OpenAIAgentsBridge` → `Agent(mcp_servers=...)` at
      construction time (one-shot; MCP list cannot be changed
      mid-session)
    - `PydanticAIBridge` Agent mode → PydanticAI's MCP toolset
      adapter
    - `PydanticAIBridge` Graph mode → each agent discovered via
      constructor `agents=` or walked from graph nodes; MCP
      invocations flow through the shared event translator's
      tool-call path
    - `GenericWorkflowBridge` deep mode → exposed on
      `RecorderContext.mcp_servers` for user-owned registration;
      shallow mode emits a single warning record and completes
      the turn without wiring
  - Mock MCP server test architecture (unit CI, no external
    binary) and filesystem-integration test harness (gated on
    `MCP_FILESYSTEM_SERVER_PATH`)
  - Failure-injection pattern for atomicity tests: monkeypatch
    the planned-mutation apply step to raise a controlled
    `MutationInjectedError` between `FrameworkStateCommitted`
    write and the paired-record write

### T2B.1: Atomic `apply_interruption` Implementation

- [x] Refactor each bridge's `apply_interruption` to expose a
  `_plan_interruption(delivered_text, mode) -> InterruptionPlan`
  helper that returns a structure describing the intended
  mutation without applying it:

  ```python
  @dataclass(frozen=True)
  class InterruptionPlan:
      mutation_kind: str              # "interrupt_truncate", "interrupt_drain", ...
      pre_state_ref: str              # artifact ref to current framework state
      post_state_ref: str             # artifact ref to post-mutation state
      framework_instructions: dict[str, Any]  # bridge-specific patching payload
  ```

- [x] Refactor each bridge to expose a
  `_apply_planned_mutation(plan: InterruptionPlan) -> None`
  helper that performs only the framework-state mutation
  described by a plan (no planning, no journal writes)
- [x] Public `apply_interruption(delivered_text, mode)` is now a
  thin orchestrator that:
  1. calls `_plan_interruption(delivered_text, mode)`
  2. writes `FrameworkStateCommitted` to the journal via
     `AgentRecorder.record_*`, carrying the plan's refs
  3. If step 2 fails or the journal reports degraded mode,
     returns immediately without applying the mutation; the
     runtime observes the non-commit and falls back to
     `CancellationMode.immediate_stop`.
  4. Calls `_apply_planned_mutation(plan)`; if this raises,
     catches the exception, writes `InterruptionApplyFailed`
     paired with the earlier `FrameworkStateCommitted` (matching
     `mutation_kind` and cursor `unit_id`), and re-raises.
  5. On success, writes `FrameworkCancellationBoundaryReached`
     with `caused_by_signal_id` populated from the runtime's
     current `ControlSignalRecord.signal_id`.
- [x] The invariant: **the journal and framework state never
  diverge on partial failures.** Either both the
  `FrameworkStateCommitted` record and the live mutation
  succeed, or the mutation is skipped and the runtime knows via
  `InterruptionApplyFailed`.
- [x] `OpenAIAgentsBridge`, `PydanticAIBridge` (Agent + Graph),
  and `GenericWorkflowBridge` (deep mode) all implement this
  pattern. `PydanticAIBridge` shares the inner interruption
  patching logic (last `ModelResponse` `TextPart` mutation)
  between Agent and Graph modes; Graph mode adds outer state
  mutation as an additional step inside
  `_apply_planned_mutation`.

### T2B.2: Shallow-Mode Downgrade Path

- [x] `GenericWorkflowBridge` shallow mode's `apply_interruption`
  continues to raise `ShallowModeInterruptionError` at the
  bridge boundary (shipped in WS2A T2.5).
- [x] `InterruptionController` (WS3 T3.2) catches the exception
  and downgrades the turn:
  - emits a `ControlSignalRecord(signal_kind="interrupt",
    cause="shallow_mode_downgrade",
    observed_stage="interruption_controller")` to the journal
  - marks the turn as "pending end-of-turn interruption" in the
    controller state
  - waits for the current turn to complete normally
  - starts the next turn immediately, without attempting
    mid-turn barge-in
- [x] Shallow workflows that implement
  `workflow.apply_interruption(delivered_text, mode)` directly
  on the workflow object opt out of the downgrade: the bridge
  delegates to the workflow's method via the same four-step
  atomic write ordering as deep mode (with the workflow's
  method playing the role of `_apply_planned_mutation`).
- [x] Document the downgrade prominently in the
  `GenericWorkflowBridge` README, in `easycat doctor` output,
  and in the CLI warning when a shallow workflow is detected
  alongside a voice transport.

### T2B.3: Three-Cancellation-Mode Test Matrix

- [x] Parametrize interruption tests over the Cartesian product of
  every bridge/mode and every cancellation mode:

  | Bridge | Mode | Modes tested |
  |---|---|---|
  | `OpenAIAgentsBridge` | — | `immediate_stop`, `drain_current_unit`, `drain_to_commit_point` |
  | `PydanticAIBridge` | Agent | `immediate_stop`, `drain_current_unit`, `drain_to_commit_point` |
  | `PydanticAIBridge` | Graph | `immediate_stop`, `drain_current_unit`, `drain_to_commit_point` |
  | `GenericWorkflowBridge` | Deep | `immediate_stop`, `drain_current_unit`, `drain_to_commit_point` |
  | `GenericWorkflowBridge` | Shallow (no workflow override) | downgrade path only |
  | `GenericWorkflowBridge` | Shallow (with workflow override) | `immediate_stop`, `drain_current_unit`, `drain_to_commit_point` |

  > **Note:** `RemoteResponsesAPIBridge` (WS2C) is not in this matrix
  > because its interruption is local (N-1 response chain, no
  > framework state mutation) and does not use the four-step
  > atomic write ordering. WS2C owns its own drain tests
  > (T2C.2, T2C.7).

- [x] Each test asserts:
  - bridge received the correct `CancellationMode`
  - journal contains the `FrameworkStateCommitted` record
  - journal contains the matching
    `FrameworkCancellationBoundaryReached` record with
    `caused_by_signal_id` populated
  - framework state after the mutation matches the plan's
    `post_state_ref`
  - `drain_to_commit_point` drains until the next cursor marked
    `committable=True` per the bridge's
    `COMMITTABLE_BOUNDARIES` mapping (from WS2A T2.7.5)
- [x] `PydanticAIBridge` Graph mode `drain_to_commit_point`
  drains to the next graph node boundary (always committable by
  `pydantic_graph` design), which distinguishes it from Agent
  mode's drain semantics.

### T2B.4: Atomicity Failure-Injection Tests

- [x] Monkeypatch `_apply_planned_mutation` on each bridge to
  raise a controlled `MutationInjectedError` between the
  `FrameworkStateCommitted` write and the paired-record write.
  Assert:
  - the journal contains `FrameworkStateCommitted` followed by
    `InterruptionApplyFailed` (with the same `mutation_kind`
    and cursor `unit_id`)
  - the framework state is unchanged (the pre-mutation state
    is still live)
  - the runtime observed the failure via the journal and the
    turn falls back to `CancellationMode.immediate_stop`
  - no `FrameworkCancellationBoundaryReached` record is written
    for the failed mutation
- [x] Second injection pattern: make the
  `FrameworkStateCommitted` write itself fail (journal in
  degraded mode). Assert:
  - the bridge returns from `apply_interruption` immediately
  - `_apply_planned_mutation` is never called
  - the framework state is unchanged
  - the runtime falls back to `immediate_stop`
  - the journal (which is in degraded mode) records the attempt
    on stderr per the WS1 T1.9 degraded-mode contract

### T2B.5: `EasyCatConfig.mcp_servers` Field

- [x] Add `mcp_servers: list[str] | None = None` field to
  `EasyCatConfig` (net-new field — it does not exist today)
- [x] Validate entries at construction: each URI must match one
  of `stdio://`, `sse://`, `http://`, `https://`; invalid URIs
  raise `EasyCatConfigError` with a message naming the offending
  entry
- [x] Document the field in the config docstring, the
  `EasyCatConfig` dataclass header, and the migration guide
  (net-new; no previous field to migrate from)
- [x] Runtime reads the field and freezes it into the
  `RecorderContext.mcp_servers` tuple passed to every
  `invoke()` call

### T2B.6: Per-Bridge MCP Forwarding

- [x] `OpenAIAgentsBridge`:
  - At bridge construction, if the runtime passes
    `mcp_servers` via `RecorderContext`, the bridge constructs
    the underlying `Agent(mcp_servers=...)` with that list.
    Implementation note: the OpenAI Agents SDK accepts
    `mcp_servers` at construction, so the bridge stores the
    list and lazily re-constructs the `Agent` on first
    `invoke()` if it was not pre-configured. Mid-session
    changes are not supported — MCP list is frozen per
    session.
- [x] `PydanticAIBridge` Agent mode:
  - Forwards `mcp_servers` to the wrapped
    `pydantic_ai.Agent`'s MCP toolset adapter at first
    `invoke()`. PydanticAI's toolset adapter handles
    connection lifecycle, discovery, and tool invocation; the
    bridge does not proxy calls.
- [x] `PydanticAIBridge` Graph mode:
  - Forwards `mcp_servers` to every `pydantic_ai.Agent`
    instance referenced by the graph. Agents are either
    supplied explicitly via the bridge constructor's
    `agents=` argument or auto-discovered by walking the
    graph's node definitions at construction time.
  - MCP tool invocations during graph node execution flow
    through the shared event translator's tool-call path —
    same record shape as Agent-mode tool calls, tagged with
    the enclosing `workflow_node` via `parent_unit_id`.
- [x] `GenericWorkflowBridge`:
  - **Shallow mode**: passing `mcp_servers` emits exactly one
    warning journal record per session naming the limitation;
    the turn completes successfully without MCP wiring. No
    EasyCat-native MCP client is created.
  - **Deep mode**: the configured `mcp_servers` tuple is
    exposed on `RecorderContext.mcp_servers` and the user's
    workflow code reads it during `on_user_turn` to register
    MCP servers against its own inference backend. No
    EasyCat-native MCP client is created.
- [x] MCP tool invocations flow through existing bridge
  tool-call events — no new record type
- [x] Zero EasyCat-native tool registry, decorator, or proxy
  added. The WS2A AC2.10 guardrail test is re-run in WS2B CI
  to catch any drift introduced by MCP wiring code.

### T2B.7: Test Migration

- [x] All existing `tests/session/` interruption tests pass with
  the four-step atomic `apply_interruption` in place
- [x] Three-cancellation-mode test matrix (T2B.3) passes
- [x] Atomicity failure-injection tests (T2B.4) pass
- [x] MCP mock and filesystem-integration tests pass
- [x] Guardrail test for no EasyCat-native tool code still passes
  after MCP wiring lands

## Acceptance Criteria

- [x] **AC2B.2** Every bridge's `apply_interruption` implements
  the four-step atomic write ordering. A code inspection test
  (AST-level) asserts each bridge's `apply_interruption` body
  consists of `_plan_interruption` → journal write → apply →
  paired write, in that order, with no direct framework-state
  mutation outside `_apply_planned_mutation`.
- [x] **AC2B.3** `InterruptionApplyFailed` is defined in
  `records.py` (from WS2A T2.2) and actually emitted by every
  bridge on controlled mutation failure. Parametrized test over
  all four interruptible bridges.
- [x] **AC2B.4** Three cancellation modes work end-to-end on
  every bridge/mode combination per the T2B.3 matrix. Test
  asserts the journal record sequence matches the expected
  shape for each combination.
- [x] **AC2B.5** `drain_to_commit_point` respects each bridge's
  `COMMITTABLE_BOUNDARIES` mapping (from WS2A T2.7.5). A
  parametrized test constructs a turn that reaches a
  non-committable cursor, triggers `drain_to_commit_point`, and
  asserts the drain proceeded to the next committable cursor
  before stopping.
- [x] **AC2B.6** Atomicity on mutation failure. A test injects a
  controlled `MutationInjectedError` into
  `_apply_planned_mutation` after `FrameworkStateCommitted` is
  written; asserts the journal shows
  `FrameworkStateCommitted` → `InterruptionApplyFailed` with
  matching `mutation_kind` and cursor, the framework state is
  unchanged, no `FrameworkCancellationBoundaryReached` record
  is written, and the runtime falls back to `immediate_stop`.
- [x] **AC2B.7** Atomicity on journal-write failure. A test
  patches the journal backend to raise on `append` of
  `FrameworkStateCommitted`; asserts `_apply_planned_mutation`
  is never called, the framework state is unchanged, and the
  runtime falls back to `immediate_stop`.
- [x] **AC2B.8** Shallow-mode downgrade path. A test constructs
  `GenericWorkflowBridge` around a shallow workflow without
  `apply_interruption`, triggers mid-turn barge-in via the
  `InterruptionController`, asserts:
  - `bridge.apply_interruption` raises
    `ShallowModeInterruptionError` (from WS2A)
  - the controller catches the exception
  - the journal contains a `ControlSignalRecord` with
    `cause="shallow_mode_downgrade"`
  - the current turn completes normally
  - the next turn starts immediately after
- [x] **AC2B.9** `EasyCatConfig(mcp_servers=[...])` passes
  through to the underlying framework on bridges that support
  MCP. Tests:
  - `test_mcp_wiring_mock_server` (unit CI, always runs): a
    mock MCP server exercises the pass-through wiring on each
    MCP-capable bridge without requiring an external binary.
    Verifies the server list reaches the framework, a tool
    call round-trips, and the tool call appears in the journal
    via the existing tool-call event path. Parametrized across
    `OpenAIAgentsBridge`, `PydanticAIBridge` Agent mode, and
    `PydanticAIBridge` Graph mode.
  - `test_mcp_filesystem_integration` (integration CI, gated):
    marked `@pytest.mark.integration`, requires
    `MCP_FILESYSTEM_SERVER_PATH` env var pointing at an
    installed `mcp-filesystem` binary. CI installs the binary
    in a dedicated integration job. Unit CI skips with a log
    line naming the skipped test. Same parametrization as the
    mock test.
  - `test_generic_workflow_shallow_mcp_warning` (unit): passing
    `mcp_servers=[...]` to a shallow workflow emits exactly one
    warning journal record per session naming the limitation
    and the turn completes successfully without MCP wiring.
  - `test_generic_workflow_deep_mcp_passthrough` (unit): the
    user's workflow code reads the `mcp_servers` tuple from
    `RecorderContext.mcp_servers` and registers it with its
    own inference backend; the test uses a stub backend that
    records the registration call.
- [x] **AC2B.10** `EasyCatConfig` validation: invalid MCP URIs
  raise `EasyCatConfigError` at construction.
- [x] **AC2B.11** Zero EasyCat-native tool code. Re-run of the
  WS2A AC2.10 guardrail grep test after MCP wiring lands;
  still zero matches for `@tool`, `@easycat_tool`,
  `@register_*`, `class .*Registry`, `class .*Router`,
  `class ToolRegistry`, `class MCPClient`, or
  `def register_tool`.
- [x] **AC2B.12** Invariant: the signal flow composes with the
  framework flow. A test runs a barge-in turn on each
  bridge, filters the journal for all records caused by the
  interrupt signal, asserts every
  `FrameworkCancellationBoundaryReached` record's
  `caused_by_signal_id` field matches a
  `ControlSignalRecord.signal_id` earlier in the same turn.

## Verification

| AC | Verification |
|---|---|
| AC2B.2 | New AST-level test `test_apply_interruption_four_step_order` walks each bridge's `apply_interruption` method, asserts calls to `_plan_interruption`, a journal write for `FrameworkStateCommitted`, `_apply_planned_mutation`, and a journal write for either `InterruptionApplyFailed` (failure path) or `FrameworkCancellationBoundaryReached` (success path), in that order, with no direct framework mutation outside `_apply_planned_mutation`. |
| AC2B.3 | Parametrized test `test_interruption_apply_failed_emitted` — monkeypatches `_apply_planned_mutation` to raise `MutationInjectedError`, runs a barge-in turn on each bridge, asserts an `InterruptionApplyFailed` record is present in the journal with the expected `mutation_kind` and paired cursor. |
| AC2B.4 | Parametrized test matrix `test_cancellation_mode_matrix` over bridge/mode × cancellation mode. Asserts the expected journal record sequence per cell. |
| AC2B.5 | New test `test_drain_to_commit_point_respects_boundaries` — constructs a turn that hits a non-committable cursor per each bridge's `COMMITTABLE_BOUNDARIES`, triggers `drain_to_commit_point`, asserts the drain proceeds to the next committable cursor and stops there. |
| AC2B.6 | New test `test_atomicity_on_apply_failure` — the failure-injection pattern from T2B.4 subtest 1. |
| AC2B.7 | New test `test_atomicity_on_commit_write_failure` — the failure-injection pattern from T2B.4 subtest 2. |
| AC2B.8 | New test `test_shallow_mode_downgrade_path` — constructs `GenericWorkflowBridge` around a shallow workflow without `apply_interruption`, drives mid-turn barge-in via the `InterruptionController`, asserts the journal shows a `ControlSignalRecord(cause="shallow_mode_downgrade")` and the turn completes normally. |
| AC2B.9 | Four tests: `test_mcp_wiring_mock_server` (unit, always runs, parametrized over `OpenAIAgentsBridge` / `PydanticAIBridge` Agent / `PydanticAIBridge` Graph), `test_mcp_filesystem_integration` (integration, gated on `MCP_FILESYSTEM_SERVER_PATH`, same parametrization), `test_generic_workflow_shallow_mcp_warning`, `test_generic_workflow_deep_mcp_passthrough`. |
| AC2B.10 | New test `test_easycat_config_mcp_uri_validation` — constructs `EasyCatConfig` with a list containing `"ftp://bad"`, asserts `EasyCatConfigError` with a message naming the offending entry. |
| AC2B.11 | Grep-based test re-runs the WS2A AC2.10 pattern set after MCP wiring lands. Zero matches. |
| AC2B.12 | New test `test_signal_to_framework_cancellation_linkage` — runs a barge-in turn per bridge, filters journal for `FrameworkCancellationBoundaryReached` records, asserts each carries a `caused_by_signal_id` matching a prior `ControlSignalRecord.signal_id` in the same turn. |

## Risks and Mitigations

- **`InterruptionController` not ready when WS2B lands**:
  mitigation — WS2B owns a stub `InterruptionController` in
  `src/easycat/integrations/agents/_testing.py` that implements
  the minimum surface needed to drive the tests (observes
  bridge exceptions, records journal entries, triggers
  downgrade path). WS3 replaces the stub with the real
  controller. The production runtime uses only the real
  controller; the stub is test-only.
- **Four-step ordering adds latency**: mitigation — the atomic
  write ordering adds one journal `append` call before the
  mutation and one after. At the WS1 latency budget ceilings
  (50µs P50 for in-memory, 500µs P50 for SQLite), this is
  well under the 50ms P99 cumulative instrumentation ceiling
  even for bursty barge-ins. Measure during WS2B implementation
  and flag if reality disagrees.
- **Framework SDKs reject MCP list changes mid-session**:
  mitigation — document that `EasyCatConfig.mcp_servers` is
  frozen per session. Runtime reads the field once at session
  construction and does not support mid-session changes.
  Attempting to mutate `mcp_servers` after session start raises
  `EasyCatConfigError`.
- **Mock MCP server differs from real ones**: mitigation —
  the gated filesystem-integration test runs the real binary in
  CI's integration job. A regression in real-world behavior
  is caught by that test even if the mock drifts.
- **MCP pass-through in Graph mode misses auto-discovered
  agents**: mitigation — WS2A's Graph-mode construction-time
  convention validation already walks the graph for its
  `agents=` list or node-based discovery. WS2B's MCP
  forwarding re-uses the same walk. A dedicated test uses a
  graph with nested agents that are not in the `agents=`
  explicit list and asserts MCP still reaches them.
- **Shallow-mode downgrade confuses users**: mitigation — the
  downgrade path writes a high-visibility journal record
  (`cause="shallow_mode_downgrade"`), `easycat doctor` warns
  on detecting a shallow workflow paired with a voice
  transport, and the shallow-mode README example explicitly
  names the downgrade behavior.

## Handoff to Next Workstream

**Merge ordering note:** WS3 merges first (with the real
`InterruptionController`), then WS2B merges second (exercising
the controller through the bridges with the full
cancellation-mode matrix). The handoff below describes what
downstream workstreams inherit once **both** WS2B and WS3 have
landed.

When this workstream is complete, **Workstream 4 (Replay and
Bundle)** inherits:

- fully atomic `apply_interruption` on every bridge, so replay
  entry-point enforcement and forked replay can rely on the
  atomicity invariant without secondary validation
- `InterruptionApplyFailed` handling wired through the journal,
  so bundle export captures the full failure path
- validated committable boundary semantics — the
  drain-to-commit-point tests prove the bridges and their
  `COMMITTABLE_BOUNDARIES` mappings align, so replay
  entry-point enforcement can rely on the mappings without
  secondary validation
- MCP pass-through proven on all bridges, so replay does not
  accidentally reintroduce tool coupling
- shallow-mode downgrade path fully wired (stub controller in
  WS2B replaced with real controller from WS3 T3.2)

**Workstream 5 (Legacy Removal)** inherits the confirmation
that the bridge interruption contract is stable and exercises
all three cancellation modes — no legacy interruption code
paths remain to clean up beyond the WS5-scoped deletions.
