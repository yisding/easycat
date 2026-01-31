# WS7: OpenAI Agents SDK Integration

**Feature:** #2 (Agents SDK Integration)
**Depends on:** WS1 (session model, event types)
**Parallel with:** WS2, WS3, WS4, WS5, WS6, WS8

## Goal

Integrate EasyCat with the OpenAI Agents SDK so that each user turn triggers an agent workflow, with streaming support and tracing pass-through.

## Deliverables

### Agent Workflow Runner

- Accept an Agents SDK `workflow` / `Agent` and run it per user turn
- On `stt.final(text)`, invoke the configured agent with the transcript
- Collect agent response and feed it to TTS
- Handle agent errors and timeouts

### Streaming Support

- Support streaming where available using Agents SDK primitives:
  - Text deltas -> emit `agent.delta(text)` events
  - Tool events -> emit `tool.call_started`, `tool.call_delta`, `tool.call_result` events (defined in WS1)
- Feed streaming text deltas to TTS for incremental synthesis (reduces latency)

### Barge-In Cancellation

- If the user interrupts (barge-in) while the agent is still streaming text/audio, cancel the agent run (or at minimum, ignore further deltas)
- Connect to WS1's cancel token: when the token is triggered, stop consuming agent stream output and clean up
- This prevents wasted API calls and ensures the agent doesn't continue generating after the user has interrupted

### Tracing Pass-Through

- Pass through Agents SDK built-in tracing hooks
- Optionally augment with EasyCat-specific spans (e.g., STT latency, TTS TTFB)
- Integrate with WS8 metrics/tracing where applicable

### Scope Boundaries

EasyCat does NOT reimplement:

- Tool calling schemas
- Handoffs
- Guardrails
- Agent memory
- **Conversation history management** — the Agents SDK has its own context patterns; do not build a second "context manager" that conflicts with it. Use the Agents SDK's built-in mechanisms for passing conversation history.

These stay in the Agents SDK. EasyCat is the audio I/O and voice pipeline layer.

## Testing Strategy

- Unit tests with a mock Agent that returns canned responses
- Test streaming: mock agent emits text deltas, verify `agent.delta` events
- Test error handling: agent throws, verify `error` event
- Integration test with a real Agents SDK agent (gated behind API key)

## Acceptance Criteria

- [ ] Agent runs per user turn on `stt.final` events
- [ ] Agent text response is forwarded to TTS
- [ ] Streaming text deltas produce `agent.delta` events
- [ ] Streaming deltas are fed to TTS incrementally
- [ ] Agents SDK tracing hooks are passed through
- [ ] Agent errors produce `error` events
- [ ] Tool events from the agent are visible in the session as `tool.call_started`/`tool.call_delta`/`tool.call_result`
- [ ] Barge-in cancels the agent streaming run (via WS1 cancel token)
- [ ] Conversation history uses Agents SDK built-in mechanisms (no duplicate context manager)
