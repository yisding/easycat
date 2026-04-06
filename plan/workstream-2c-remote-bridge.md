# Workstream 2C: Remote Agent Bridge (Responses API)

> **Part of the essential debug-first runtime redesign.** Design rationale
> lives in `essential-debug-first-runtime.md`. This file is the
> operational plan.
>
> **Predecessors**: Workstream 2A (Agent Bridge Protocol and Bridges)
> must be complete. The `ExternalAgentBridge` protocol, `AgentRecorder`,
> `AgentBridgeEvent`, and `ExecutionCursor` types must be stable.
>
> **Runs in parallel** with Workstreams 2B and 3. This workstream does
> not depend on the `InterruptionController` (WS3 T3.2) or the
> four-step atomic write ordering (WS2B T2B.1) because interruption in
> the remote bridge is handled locally via N-1 response chain and
> partial input replay — no framework state mutation RPC is needed.
>
> **Successors**: Workstream 4 (Replay and Bundle) covers replay for
> the remote bridge (SIMULATED and LIVE only; ARTIFACT is not
> available because internal framework state is not captured).
> Workstream 5 (Legacy Removal) includes the remote bridge in the
> `easycat.__all__` allowlist.
>
> **Sibling workstreams:**
>
> - `workstream-1-journal-foundation.md`
> - `workstream-2a-agent-bridges.md`
> - `workstream-2b-interruption-and-mcp.md`
> - `workstream-3-stage-refactor.md`
> - `workstream-4-replay-and-bundle.md`
> - `workstream-5-legacy-removal.md`

> **Compatibility policy**: This workstream introduces a net-new bridge
> and config surface. There is no pre-existing API to maintain
> compatibility with.

## Goal

Ship `ResponsesAPIBridge`, a fourth `ExternalAgentBridge`
implementation that speaks the OpenAI Responses API over HTTP to a
remote agent server. This enables deployments where the agent runs
on separate infrastructure from the voice pipeline — the most
common enterprise deployment pattern. Interruption uses the
Responses API's native `input` field to chain from the last
completed response and replay the interrupted exchange with
truncated assistant text. An optional `easycat.*` metadata
convention supports stateful agent servers that maintain state
beyond conversation history.

## Scope

**In scope:**

- `ResponsesAPIBridge` implementing `ExternalAgentBridge`
- SSE stream parsing for Responses API events
- Translation of Responses API SSE events to `AgentBridgeEvent`
  yields and `AgentRecorder` calls
- N-1 response chain interruption: voice runtime tracks
  `last_completed_response_id`, chains next turn from it,
  replays interrupted exchange as `input` items with truncated
  assistant text
- Drain logic on the SSE stream: `immediate_stop` cancels the
  stream, `drain_current_unit` reads until
  `response.output_item.done` for the active tool call,
  `drain_to_commit_point` reads until the next turn-edge
  boundary (for the remote bridge, turn edges are the only
  committable boundaries)
- `easycat.*` metadata convention for stateful agent servers
  (optional, graceful degradation when not supported)
- Capability discovery via response metadata
  (`easycat.supports_interruption`)
- `EasyCatConfig` accepts a URL string for the `agent` field,
  auto-detected by `auto_adapt_agent()` and routed to
  `ResponsesAPIBridge`
- Authentication: `api_key` parameter on the bridge,
  `EASYCAT_REMOTE_AGENT_API_KEY` env var, and
  `Authorization: Bearer` header on requests. The API key is
  excluded from journal records and bundles by the WS1
  safe-default allowlist.
- Journal records: turn-level events and tool calls from the
  SSE stream, recorded via `AgentRecorder` with
  `kind="remote_responses_api"` on `FrameworkStateSnapshot`
- Reduced `COMMITTABLE_BOUNDARIES`: turn edges only
  (`{"agent": CommitRule.BETWEEN_TURNS}`)
- Tests against a mock Responses API server (unit CI, always
  runs) and against the real OpenAI API (integration, gated on
  `OPENAI_API_KEY`)

**Out of scope:**

- Internal framework transitions (not visible from the SSE
  stream — the remote agent is opaque beyond text, tool calls,
  and structured output)
- ARTIFACT replay for the agent stage (no captured framework
  state to replay from; SIMULATED and LIVE are supported)
- `apply_interruption` as a remote RPC (interruption is local)
- Custom WebSocket protocol (permanent guardrail — the Responses
  API over HTTP is the only remote agent protocol)
- MCP pass-through on the remote bridge (the remote agent server
  manages its own tools; MCP configuration is the agent
  server's responsibility, not EasyCat's)
- OpenResponses plugin implementation (documented as a user
  contribution path, not shipped by EasyCat)

## Tasks

### T2C.0: Architecture Freeze

- [ ] Design decisions covering:
  - `ResponsesAPIBridge` class design and constructor arguments
  - SSE event taxonomy: which Responses API events map to which
    `AgentBridgeEvent` types and `AgentRecorder` calls
  - N-1 chain interruption protocol: how
    `last_completed_response_id` is tracked, what input items
    are constructed on interruption, how tool calls from the
    interrupted turn are replayed as `function_call` +
    `function_call_output` input items
  - Drain semantics on the SSE stream per cancellation mode
  - `easycat.*` metadata key namespace and semantics:
    - Request metadata: `easycat.interrupted_response_id`,
      `easycat.delivered_text`, `easycat.cancellation_mode`
    - Response metadata: `easycat.supports_interruption`,
      `easycat.supports_drain`, `easycat.framework`
  - Capability discovery: how the bridge reads response metadata
    on the first turn and caches it for the session
  - `EasyCatConfig.agent` URL string detection and
    `auto_adapt_agent()` routing
  - Authentication model: API key handling, env var, header
  - Journal record shape for remote bridge turns: what records
    are emitted, what `FrameworkStateSnapshot` contains (local
    history only), why internal framework transitions are absent
  - Mock Responses API server design for unit tests
  - Reduced debugging depth documentation: what users gain and
    lose compared to in-process bridges

### T2C.1: SSE Stream Parser and Event Translator

- [ ] Create `src/easycat/integrations/agents/responses_api.py`
- [ ] Implement SSE stream parser for the Responses API event
  taxonomy. Events to handle:
  - `response.created` → record turn start
  - `response.output_item.added` → track new output item (text,
    function call, etc.)
  - `response.content_part.added` → track new content part
  - `response.content_part.delta` → yield
    `AgentBridgeEvent.text_delta`, accumulate delivered text
  - `response.function_call_arguments.delta` → accumulate tool
    call arguments
  - `response.function_call_arguments.done` → emit
    `recorder.record_tool_call(phase="start", name=...,
    args_ref=...)`
  - `response.output_item.done` → for function call outputs,
    emit `recorder.record_tool_call(phase="result", ...)`; for
    text outputs, finalize the text accumulator
  - `response.completed` → emit cursor exit, yield
    `AgentBridgeEvent.done`
  - `response.failed` → emit `recorder.record_framework_error`,
    raise
  - `response.incomplete` → handle partial completion (content
    filter, token limit, etc.)
- [ ] Create shared event-translator module
  `src/easycat/integrations/agents/_responses_api_events.py`
  following the `_<framework>_events.py` naming convention from
  WS2A T2.4
- [ ] Unknown SSE event types are logged at debug level and
  skipped — forward-compatible with Responses API additions

### T2C.2: ResponsesAPIBridge Implementation

- [ ] `ResponsesAPIBridge.__init__` accepts:
  - `base_url: str` — Responses API base URL
    (e.g., `https://api.openai.com` or a self-hosted server)
  - `model: str` — model identifier passed on every request
  - `api_key: str | None = None` — falls back to
    `EASYCAT_REMOTE_AGENT_API_KEY` env var
  - `timeout: float = 120.0` — HTTP timeout per request
  - `metadata: dict[str, str] | None = None` — static metadata
    sent on every request (user-defined, merged with
    `easycat.*` keys)
- [ ] Uses `httpx.AsyncClient` for HTTP and SSE streaming
- [ ] `invoke(turn_input, recorder, cancel_token)`:
  - Constructs the Responses API request body:
    - `model` from constructor
    - `input` from `turn_input.text` (simple case) or from
      the interrupted-turn replay items (interruption case)
    - `previous_response_id` from
      `self._last_completed_response_id` (or `None` for first
      turn; or N-1 id for post-interruption turns)
    - `stream: true`
    - `metadata` merged from constructor metadata +
      interruption metadata (if applicable)
  - Streams SSE events through the event translator
  - On `cancel_token` cancellation:
    - `immediate_stop`: close the HTTP stream immediately
    - `drain_current_unit`: continue reading events until the
      active `function_call`'s `response.output_item.done`
      arrives, then close
    - `drain_to_commit_point`: for the remote bridge,
      equivalent to letting the turn complete (turn edges are
      the only committable boundaries), so this drains the
      entire stream — documented as a known limitation of
      the remote bridge
  - Accumulates the response's output items for use as replay
    input items on interruption
  - On successful completion (no interruption), updates
    `self._last_completed_response_id`
- [ ] `apply_interruption(delivered_text, mode)`:
  - Stashes `InterruptionInfo(response_id, delivered_text,
    mode)` — no RPC
  - If `self._remote_supports_interrupt is True`, sets
    `easycat.*` metadata keys for the next request
  - Regardless of remote support, reconstructs the interrupted
    turn's input items from accumulated SSE events with
    assistant text truncated to `delivered_text`
  - Does NOT update `self._last_completed_response_id` (the
    interrupted response is skipped in the chain)
- [ ] `snapshot_state()`:
  - Returns `FrameworkStateSnapshot` with `kind=
    "remote_responses_api"` and `fields` containing:
    `response_count`, `last_completed_response_id`,
    `remote_supports_interrupt`, `base_url` (host only,
    no path or credentials)
  - No artifact ref (local history is small enough to inline
    for typical conversation lengths; if it exceeds 4KB, the
    standard overflow policy from WS2A T2.1 applies)
- [ ] `reset()`:
  - Clears `_last_completed_response_id`, accumulated items,
    interruption state, and capability cache

### T2C.3: EasyCatConfig URL Detection

- [ ] `EasyCatConfig.agent` accepts a URL string
  (e.g., `"https://my-agent.internal:8080"`) in addition to
  bridge instances
- [ ] `auto_adapt_agent()` detects URL strings via
  `urllib.parse.urlparse` — if the value has a scheme in
  `{"http", "https"}` and a netloc, route to
  `ResponsesAPIBridge(base_url=url, model=config.agent_model)`
- [ ] `EasyCatConfig.agent_model: str | None = None` — new
  field, required when `agent` is a URL string. Raises
  `EasyCatConfigError` if `agent` is a URL and `agent_model` is
  not set.
- [ ] `EasyCatConfig.remote_agent_api_key: str | None = None` —
  new field, forwarded to the bridge. Excluded from the WS1
  safe-default config allowlist (the field name contains `key`).
  Falls back to `EASYCAT_REMOTE_AGENT_API_KEY` env var.
- [ ] Document the URL string path in the config docstring and
  migration guide

### T2C.4: Capability Discovery

- [ ] On the first response received from the remote server, the
  bridge reads the response's `metadata` field for:
  - `easycat.supports_interruption` — if `"true"`, the bridge
    sends `easycat.*` metadata on subsequent interrupted turns
  - `easycat.supports_drain` — if `"true"`, the bridge sends
    `easycat.cancellation_mode` in metadata
  - `easycat.framework` — informational, recorded in the journal
    and bundle manifest as the remote agent's framework identity
- [ ] Discovery result is cached for the session lifetime (the
  remote server's capabilities do not change mid-session)
- [ ] If the first response has no `easycat.*` metadata, the bridge
  assumes the server is a plain Responses API server and uses the
  N-1 chain + partial replay path exclusively (no `easycat.*`
  metadata sent on subsequent requests)

### T2C.5: Interrupted Turn Replay Construction

- [ ] During `invoke()`, the bridge accumulates the response's
  output items as structured data:
  - Text content parts → accumulated text string
  - Function calls → `(name, arguments, call_id)` tuples
  - Function call outputs → `(call_id, output)` tuples
- [ ] On interruption, `apply_interruption` reconstructs the
  interrupted turn as a list of Responses API input items:
  1. The user's original input text as a user message
  2. Each completed function call as a `function_call` input item
  3. Each completed function call output as a
     `function_call_output` input item
  4. The truncated assistant text as an assistant message
     (truncated to `delivered_text`)
  5. The new user input (from the next `invoke()` call's
     `turn_input.text`) appended as a user message
- [ ] Items 1–4 are computed in `apply_interruption` and stashed.
  Item 5 is appended in the next `invoke()` call.
- [ ] The next `invoke()` sets `previous_response_id` to
  `self._last_completed_response_id` (the N-1 response, not the
  interrupted one) and puts items 1–5 in the `input` field

### T2C.6: Mock Responses API Server

- [ ] Create `tests/integrations/agents/mock_responses_server.py`
- [ ] Implement a minimal ASGI server (using `starlette` or raw
  ASGI) that:
  - Accepts `POST /v1/responses` with the Responses API request
    schema
  - Returns SSE streams with configurable event sequences
    (text deltas, tool calls, completions, errors)
  - Stores conversation state keyed by response ID for
    `previous_response_id` chaining
  - Optionally reads `easycat.*` request metadata and patches
    stored conversation state (simulates a Tier 2 server)
  - Optionally returns `easycat.*` response metadata
    (simulates capability advertisement)
  - Supports configurable latency per event (for drain timing
    tests)
- [ ] The mock server runs in-process via `httpx.ASGITransport`
  — no subprocess, no port binding, no flaky teardown
- [ ] The mock server is test infrastructure only — it does not
  ship as part of EasyCat's public surface

### T2C.7: Tests

- [ ] All tests use the mock Responses API server from T2C.6
  unless gated on `OPENAI_API_KEY` for integration tests
- [ ] Bridge construction and protocol conformance:
  - `ResponsesAPIBridge` implements `ExternalAgentBridge`
  - Constructor validates `base_url` (must have scheme and
    netloc)
  - `api_key` is excluded from snapshots and journal records
- [ ] Turn execution without interruption:
  - Simple text turn: user input → text deltas → done
  - Turn with tool calls: user input → function call →
    function call output → text deltas → done
  - Journal contains `record_tool_call` entries for each tool
    call from the stream
  - `snapshot_state()` returns `kind="remote_responses_api"`
- [ ] Interruption via N-1 chain:
  - Interrupt mid-text: cancel stream, apply interruption,
    next turn chains from N-1 response with truncated text
  - Interrupt mid-tool-call with `drain_current_unit`: stream
    reads until tool call completes, then cancels; next turn
    includes the completed tool call in input items
  - Interrupt on first turn (no N-1 response): entire
    conversation goes as input items with no
    `previous_response_id`
  - Two consecutive interruptions: each chains from the last
    *completed* response, not from the previous interrupted one
- [ ] Capability discovery:
  - Server returns `easycat.supports_interruption: "true"` →
    bridge sends `easycat.*` metadata on next interrupted turn
  - Server returns no `easycat.*` metadata → bridge uses N-1
    chain only, no `easycat.*` metadata sent
  - Discovery is cached: second turn does not re-probe
- [ ] Metadata-aware server:
  - Server reads `easycat.interrupted_response_id` and patches
    its stored conversation → next turn's
    `previous_response_id` points to the interrupted response
    (not N-1), and the conversation is coherent
- [ ] `EasyCatConfig` URL detection:
  - `EasyCatConfig(agent="https://example.com", agent_model=
    "gpt-5.2")` constructs a `ResponsesAPIBridge`
  - `EasyCatConfig(agent="https://example.com")` without
    `agent_model` raises `EasyCatConfigError`
  - `auto_adapt_agent("https://example.com")` returns a
    `ResponsesAPIBridge`
- [ ] Integration test (gated on `OPENAI_API_KEY`):
  - Runs a full turn against the real OpenAI Responses API
  - Verifies SSE events are parsed correctly
  - Verifies `previous_response_id` chaining works across
    two turns
- [ ] Guardrail: no custom WebSocket protocol. Grep-based test
  asserts zero matches in `src/easycat/integrations/agents/` for
  `websocket`, `WebSocket`, `ws://`, or `wss://` outside of
  comments.

## Acceptance Criteria

- [ ] **AC2C.2** `ResponsesAPIBridge` exists in
  `src/easycat/integrations/agents/responses_api.py` and
  implements `ExternalAgentBridge`.
- [ ] **AC2C.3** A turn against the mock server produces the
  correct journal record sequence: `agent` cursor entered, tool
  call records (if tools were invoked), text deltas, `agent`
  cursor exited. `FrameworkStateSnapshot.kind` is
  `"remote_responses_api"`.
- [ ] **AC2C.4** N-1 chain interruption. A test interrupts a
  multi-turn conversation mid-response, inspects the next
  request's `previous_response_id` (must be the N-1 response,
  not the interrupted one) and `input` items (must contain the
  user's original input, any completed tool calls/outputs, the
  truncated assistant text, and the new user input). The mock
  server validates the reconstructed conversation is coherent.
- [ ] **AC2C.5** `drain_current_unit` on the SSE stream. A test
  triggers interruption while a tool call is in-flight,
  configures the mock server to emit the tool call's
  `response.output_item.done` after a delay, and asserts the
  bridge waited for that event before closing the stream. The
  next turn's input items include the completed tool call.
- [ ] **AC2C.6** Capability discovery. Two sub-tests: (1) server
  returns `easycat.supports_interruption: "true"` in response
  metadata → bridge sends `easycat.*` metadata on the next
  interrupted turn; (2) server returns no `easycat.*` metadata →
  bridge sends no `easycat.*` metadata and uses N-1 chain only.
- [ ] **AC2C.7** Graceful degradation. A test runs two
  consecutive turns against a plain mock server (no `easycat.*`
  support) with an interruption between them. The conversation
  is coherent despite the server not understanding interruption
  metadata.
- [ ] **AC2C.8** `EasyCatConfig(agent="https://example.com",
  agent_model="gpt-5.2")` constructs a `ResponsesAPIBridge`.
  `auto_adapt_agent("https://example.com")` returns a
  `ResponsesAPIBridge`. Missing `agent_model` raises
  `EasyCatConfigError`.
- [ ] **AC2C.9** API key is excluded from journal records,
  snapshots, and bundles. A test constructs a bridge with
  `api_key="sk-synthetic-test-key"`, runs a turn, greps the
  journal backend for the key string, asserts zero hits.
- [ ] **AC2C.10** `COMMITTABLE_BOUNDARIES` is
  `{"agent": CommitRule.BETWEEN_TURNS}`. A test asserts the
  mapping is present and correct.
- [ ] **AC2C.11** No custom WebSocket protocol. Grep-based test
  passes.
- [ ] **AC2C.12** All existing tests pass: `uv run pytest` exits 0.
- [ ] **AC2C.13** Integration test against the real OpenAI
  Responses API passes (gated on `OPENAI_API_KEY`).

## Verification

| AC | Verification |
|---|---|
| AC2C.2 | `python -c "from easycat.integrations.agents.responses_api import ResponsesAPIBridge"` exits 0; new test `test_responses_api_bridge_implements_protocol` asserts `isinstance(ResponsesAPIBridge(...), ExternalAgentBridge)`. |
| AC2C.3 | New test `test_responses_api_turn_execution` — runs a turn with text + tool calls against the mock server, walks the journal, asserts the expected record sequence and snapshot kind. |
| AC2C.4 | New test `test_responses_api_n1_chain_interruption` — runs 3 turns, interrupts the 2nd, inspects the 3rd turn's request body for correct `previous_response_id` and input items with truncated assistant text and completed tool calls. |
| AC2C.5 | New test `test_responses_api_drain_current_unit` — configures mock to delay `output_item.done` for a tool call by 100ms, triggers `immediate_stop` (asserts stream closes before tool completes) and `drain_current_unit` (asserts stream stays open until tool completes). |
| AC2C.6 | New test `test_responses_api_capability_discovery` — two parametrized sub-tests: server with `easycat.*` metadata and server without. Asserts the bridge's behavior matches. |
| AC2C.7 | New test `test_responses_api_graceful_degradation` — plain mock server, two turns with interruption, asserts conversation is coherent (the reconstructed input items produce a sensible conversation from the server's perspective). |
| AC2C.8 | New test `test_easycat_config_url_agent` — asserts URL detection, `auto_adapt_agent` routing, and missing-model error. |
| AC2C.9 | New test `test_responses_api_key_not_in_journal` — runs a turn with a synthetic API key, greps the journal and artifact store, asserts zero hits. |
| AC2C.10 | New test `test_responses_api_committable_boundaries` — asserts the mapping is `{"agent": CommitRule.BETWEEN_TURNS}`. |
| AC2C.11 | Grep-based test `test_no_websocket_protocol_in_bridges` — asserts zero matches for WebSocket patterns in `src/easycat/integrations/agents/`. |
| AC2C.12 | `uv run pytest` exits 0. |
| AC2C.13 | New integration test `test_responses_api_openai_live` — gated on `OPENAI_API_KEY`, runs two turns against the real OpenAI API, asserts SSE events parse correctly and `previous_response_id` chaining works. |

## Risks and Mitigations

- **Responses API is an OpenAI-specific standard**: mitigation —
  OpenResponses provides an open-source implementation. The
  protocol is HTTP + SSE with a JSON schema, not a proprietary
  SDK. Any server that speaks the schema works. Document that
  EasyCat's remote bridge speaks "the Responses API" (a de facto
  standard), not "the OpenAI API" (a vendor endpoint).
- **SSE event taxonomy changes between Responses API versions**:
  mitigation — unknown event types are logged and skipped
  (forward-compatible). Pin the minimum Responses API version in
  the plan. The event translator module
  (`_responses_api_events.py`) centralizes all event mapping so
  upstream changes are fixed in one place.
- **N-1 chain produces slightly different results than patching
  the interrupted response in-place**: mitigation — this is a
  known trade-off of the Tier 1 path. Document that the agent
  server sees the full (un-truncated) assistant text only if it
  stored `previous_response_id = resp_N` and is unaware of the
  interruption. With N-1 chaining, the server sees the truncated
  text. If exact state parity matters, the user upgrades to
  Tier 2 (metadata-aware server) or Tier 3 (in-process bridge).
- **`drain_to_commit_point` is effectively "drain entire turn"
  for the remote bridge**: mitigation — document this as a known
  limitation. The remote bridge has only turn-edge committable
  boundaries because the voice runtime cannot see inside the
  remote agent's execution. Users who need fine-grained
  drain-to-commit should use an in-process bridge.
- **Mock server diverges from real Responses API behavior**:
  mitigation — the gated integration test against the real
  OpenAI API catches regressions. The mock is for CI; the
  integration test is the truth.
- **API key leaks into journal**: mitigation — the key field
  name contains `key`, so the WS1 safe-default allowlist drops
  it automatically. AC2C.9 tests this explicitly.

## Handoff to Next Workstreams

When this workstream is complete:

- **Workstream 3** wraps `ResponsesAPIBridge` in `AgentStage`
  alongside the three in-process bridges. No special handling
  needed — the remote bridge implements the same
  `ExternalAgentBridge` protocol.
- **Workstream 4** adds SIMULATED and LIVE replay support for
  the remote bridge. ARTIFACT replay is not available (no
  captured framework state). The bundle manifest includes the
  remote agent's base URL (host only) and discovered
  `easycat.framework` value.
- **Workstream 5** includes `ResponsesAPIBridge` and
  `auto_adapt_agent` URL detection in the `easycat.__all__`
  allowlist.
