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
  - Tool events -> pass through to session
- Feed streaming text deltas to TTS for incremental synthesis (reduces latency)

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
- [ ] Tool events from the agent are visible in the session
