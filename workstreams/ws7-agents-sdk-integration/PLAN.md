# WS7: Agents SDK Integration — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.2 (session model, event types).
> Can be developed against a stub session and mock agents.

## Phase 1: Agent Workflow Runner

### Task 7.1: Agent runner core
- Implement `AgentRunner` that accepts an OpenAI Agents SDK `Agent` or workflow
- Subscribe to `stt.final(text)` events from the session
- On each final transcript, invoke the agent with the user's text
- Collect the agent's text response
- Emit `agent.final(text)` event
- Feed the response text to the configured TTS provider via the session
- Handle agent exceptions: catch errors, emit `error(exception)` event, don't crash the session

### Task 7.2: Conversation context management
- **Prefer the Agents SDK's built-in context/history mechanism** over building a separate context manager — avoid duplicating conversation state management that the SDK already handles
- If the Agents SDK requires explicit history passing, maintain a minimal transcript list (user/assistant turns) and pass it on each invocation
- Clear context on `session.reset_state()`
- Test: multi-turn conversation, verify agent sees prior context

### Task 7.3: Agent timeout handling
- Wrap agent invocations with a configurable timeout
- If the agent doesn't respond within the timeout, emit `error(AgentTimeoutError)` and cancel the invocation
- Allow the session to recover (return to listening state)
- Test: mock agent that hangs -> verify timeout fires

## Phase 2: Streaming Support

### Task 7.4: Streaming text delta support
- When the Agents SDK agent supports streaming, consume text deltas as they arrive
- Emit `agent.delta(text)` events for each delta
- Forward deltas to TTS incrementally for reduced latency (start speaking before the full response is ready)
- Emit `agent.final(text)` with the complete response when streaming is done
- Test: mock agent streams "Hello " + "world" -> verify two delta events + one final event

### Task 7.5: Streaming tool event pass-through
- When the agent invokes tools during streaming, emit `tool.call_started`, `tool.call_delta`, `tool.call_result` events (defined in WS1 event model)
- Allow the session/application to observe tool calls (e.g., for UI display or logging)
- Do not interfere with tool execution — the Agents SDK handles tool logic
- Test: mock agent calls a tool -> verify tool events are visible on the session

### Task 7.5b: Agent cancellation on barge-in
- Subscribe to WS1's cancel token for the current turn
- When the cancel token fires (barge-in), stop consuming agent stream output:
  - If the Agents SDK supports cancellation, cancel the run
  - Otherwise, stop reading from the stream iterator and discard remaining output
- Emit `agent.final(text)` with whatever partial text was received before cancellation (or skip if nothing meaningful)
- Clean up any resources from the agent invocation
- Test: mock agent streaming + simulate barge-in -> verify agent output stops being consumed

## Phase 3: Tracing

### Task 7.6: Agents SDK tracing pass-through
- Detect and pass through the Agents SDK's built-in tracing hooks
- Ensure trace context propagates from the session into agent invocations
- If the Agents SDK provides trace IDs or span contexts, carry them through
- Test: verify trace context is present on agent invocation

### Task 7.7: EasyCat custom tracing spans
- Emit custom spans for EasyCat-specific stages surrounding the agent call:
  - `stt_to_agent` — time from STT final to agent invocation
  - `agent_execution` — time spent in agent
  - `agent_to_tts` — time from agent response to TTS start
- These integrate with WS8's observability layer
- Test: verify spans are emitted with correct timing

## Phase 4: Validation

### Task 7.8: End-to-end agent integration test
- Create a simple test agent (echoes input, or uses a canned response)
- Run a full turn: audio in -> STT -> agent -> TTS -> audio out
- Verify the agent received the correct transcript
- Verify TTS received the agent's response
- Verify all events fired in correct order
- Integration test with a real Agents SDK agent (gated behind `OPENAI_API_KEY`)
