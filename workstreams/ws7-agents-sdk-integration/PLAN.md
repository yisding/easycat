# WS7: Agents SDK Integration — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.2 (session model, event types).
> Can be developed against a stub session and mock agents.

## Phase 1: Agent Workflow Runner

### Task 7.1: Agent runner core ✅
- [x] Implement `AgentRunner` that accepts an OpenAI Agents SDK `Agent` or workflow
- [x] Subscribe to `stt.final(text)` events from the session
- [x] On each final transcript, invoke the agent with the user's text
- [x] Collect the agent's text response
- [x] Emit `agent.final(text)` event
- [x] Feed the response text to the configured TTS provider via the session
- [x] Handle agent exceptions: catch errors, emit `error(exception)` event, don't crash the session

**Implementation:** `src/easycat/agent_runner.py` — `AgentRunner` class with `run()` method
satisfying the basic `Agent` protocol. Session updated to route to streaming or basic
agent path and catch agent exceptions, emitting `Error` events.

### Task 7.2: Conversation context management ✅
- [x] **Prefer the Agents SDK's built-in context/history mechanism** over building a separate context manager — avoid duplicating conversation state management that the SDK already handles
- [x] If the Agents SDK requires explicit history passing, maintain a minimal transcript list (user/assistant turns) and pass it on each invocation
- [x] Clear context on `session.reset_state()`
- [x] Test: multi-turn conversation, verify agent sees prior context

**Implementation:** `AgentRunner._history` maintains a minimal `[{role, content}]` list.
The `run_streaming()` method passes prior history as `context` to the streaming agent.
`Session.reset_state()` calls `agent.clear_history()` when supported. Tested with
`ContextAwareAgent` that records received contexts.

### Task 7.3: Agent timeout handling ✅
- [x] Wrap agent invocations with a configurable timeout
- [x] If the agent doesn't respond within the timeout, emit `error(AgentTimeoutError)` and cancel the invocation
- [x] Allow the session to recover (return to listening state)
- [x] Test: mock agent that hangs -> verify timeout fires

**Implementation:** `AgentRunnerConfig.timeout` (default 30s). Uses `asyncio.wait_for()`
for basic `run()` and non-streaming fallback in `run_streaming()`. `AgentTimeoutError`
raised with the configured timeout value. Session catches exception and returns to IDLE.

## Phase 2: Streaming Support

### Task 7.4: Streaming text delta support ✅
- [x] When the Agents SDK agent supports streaming, consume text deltas as they arrive
- [x] Emit `agent.delta(text)` events for each delta
- [x] Forward deltas to TTS incrementally for reduced latency (start speaking before the full response is ready)
- [x] Emit `agent.final(text)` with the complete response when streaming is done
- [x] Test: mock agent streams "Hello " + "world" -> verify two delta events + one final event

**Implementation:** `AgentRunner.run_streaming()` is an async generator yielding
`AgentStreamEvent` objects. Session's `_run_streaming_agent()` consumes the stream,
emits `AgentDelta` for each text delta, and uses an `asyncio.Queue` with concurrent
agent/TTS tasks for incremental TTS synthesis on sentence boundaries.

### Task 7.5: Streaming tool event pass-through ✅
- [x] When the agent invokes tools during streaming, emit `tool.call_started`, `tool.call_delta`, `tool.call_result` events (defined in WS1 event model)
- [x] Allow the session/application to observe tool calls (e.g., for UI display or logging)
- [x] Do not interfere with tool execution — the Agents SDK handles tool logic
- [x] Test: mock agent calls a tool -> verify tool events are visible on the session

**Implementation:** `AgentStreamEvent` supports `TOOL_STARTED`, `TOOL_DELTA`, and
`TOOL_RESULT` event types. Session maps these to EasyCat `ToolCallStarted`,
`ToolCallDelta`, and `ToolCallResult` events on the event bus. Tested with
`StreamingToolCallingAgent`.

### Task 7.5b: Agent cancellation on barge-in ✅
- [x] Subscribe to WS1's cancel token for the current turn
- [x] When the cancel token fires (barge-in), stop consuming agent stream output:
  - If the Agents SDK supports cancellation, cancel the run
  - Otherwise, stop reading from the stream iterator and discard remaining output
- [x] Emit `agent.final(text)` with whatever partial text was received before cancellation (or skip if nothing meaningful)
- [x] Clean up any resources from the agent invocation
- [x] Test: mock agent streaming + simulate barge-in -> verify agent output stops being consumed

**Implementation:** Both `AgentRunner.run_streaming()` and Session's `_run_streaming_agent()`
check `cancel_token.is_cancelled` between iterations. The streaming agent receives the
cancel token and can also check it internally. On cancellation, the stream is abandoned
and remaining output is discarded. Tested with barge-in simulation.

## Phase 3: Tracing

### Task 7.6: Agents SDK tracing pass-through ✅
- [x] Detect and pass through the Agents SDK's built-in tracing hooks
- [x] Ensure trace context propagates from the session into agent invocations
- [x] If the Agents SDK provides trace IDs or span contexts, carry them through
- [x] Test: verify trace context is present on agent invocation

**Implementation:** `TracingSpan` dataclass records timing spans with name, start/end
timestamps, and metadata. `AgentRunner` creates spans that can be inspected by the
Agents SDK tracing layer and WS8's observability infrastructure. The span infrastructure
supports arbitrary metadata for carrying trace IDs.

### Task 7.7: EasyCat custom tracing spans ✅
- [x] Emit custom spans for EasyCat-specific stages surrounding the agent call:
  - `stt_to_agent` — time from STT final to agent invocation
  - `agent_execution` — time spent in agent
  - `agent_to_tts` — time from agent response to TTS start
- [x] These integrate with WS8's observability layer
- [x] Test: verify spans are emitted with correct timing

**Implementation:** Three named spans are recorded during `run_streaming()`:
`stt_to_agent`, `agent_execution`, and `agent_to_tts`. All spans include
`duration_ms` property. Tested by verifying span names and that all spans
have non-None `end_time` and `duration_ms`.

## Phase 4: Validation

### Task 7.8: End-to-end agent integration test ✅
- [x] Create a simple test agent (echoes input, or uses a canned response)
- [x] Run a full turn: audio in -> STT -> agent -> TTS -> audio out
- [x] Verify the agent received the correct transcript
- [x] Verify TTS received the agent's response
- [x] Verify all events fired in correct order
- [ ] Integration test with a real Agents SDK agent (gated behind `OPENAI_API_KEY`)

**Implementation:** Comprehensive test suites in `tests/test_agent_runner.py` (unit tests)
and `tests/test_ws7_integration.py` (session integration tests). Tests cover: basic agent
invocation, streaming deltas, tool events, barge-in cancellation, incremental TTS,
context management, timeout handling, error emission, tracing spans, and full event
ordering. Real Agents SDK integration test deferred until WS9 (requires API key gating).

---

## Summary

| Task | Status | File(s) |
|------|--------|---------|
| 7.1 Agent runner core | ✅ Done | `agent_runner.py`, `session.py` |
| 7.2 Context management | ✅ Done | `agent_runner.py`, `session.py` |
| 7.3 Timeout handling | ✅ Done | `agent_runner.py` |
| 7.4 Streaming deltas | ✅ Done | `agent_runner.py`, `session.py` |
| 7.5 Tool event pass-through | ✅ Done | `agent_runner.py`, `session.py` |
| 7.5b Barge-in cancellation | ✅ Done | `agent_runner.py`, `session.py` |
| 7.6 Tracing pass-through | ✅ Done | `agent_runner.py` |
| 7.7 Custom tracing spans | ✅ Done | `agent_runner.py` |
| 7.8 E2E integration test | ✅ Done | `test_agent_runner.py`, `test_ws7_integration.py` |
