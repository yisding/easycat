# WS8: Reliability & Observability

**Features:** #11 (Reliability, Retries, Backpressure), #12 (Metrics & Observability)
**Depends on:** WS1 (session model, event types)
**Parallel with:** WS2, WS3, WS4, WS5, WS6, WS7
**Note:** This workstream produces cross-cutting utilities. Other workstreams will integrate these, but they can develop in parallel by building the utilities against the defined interfaces.

## Goal

Build reliability primitives (reconnection, timeouts, backpressure) and observability tooling (metrics, tracing) that other workstreams integrate into their components.

## Deliverables

### WebSocket Reconnect Strategy

- Reconnect logic for streaming STT/TTS provider WebSockets
- Exponential backoff with jitter
- Configurable max retries
- Emit `reconnect` events for observability

### Timeouts

Configurable timeouts for:

- STT response (time from audio submission to transcript)
- Agent run (time from transcript to agent response)
- TTS first audio byte (TTFB from text submission to first audio chunk)
- Emit `error` events on timeout with clear timeout type identification

### Backpressure Protection

- Bounded queues for audio in/out
- Drop/skip policy for stale audio when a turn is canceled
- Prevent unbounded memory growth during slow consumers or fast producers

### Metrics

Expose the following metrics (format TBD — support at minimum a callback/hook interface):

- `stt_latency_ms` — time from end of user speech to final transcript
- `agent_latency_ms` — time from transcript to first agent response
- `tts_ttfb_ms` — time from text submission to first audio byte
- `turn_end_to_end_ms` — time from end of user speech to first bot audio
- Counts: interruptions, reconnects, errors

### Tracing

- Integrate with Agents SDK built-in tracing
- Pass through trace context
- Emit custom spans for EasyCat pipeline stages (noise reduction, VAD, STT, agent, TTS)
- Support OpenTelemetry-compatible span export (optional, stretch)

## Testing Strategy

- Reconnect: simulate WebSocket disconnects, verify reconnection with backoff
- Timeouts: inject slow providers, verify timeout errors fire
- Backpressure: flood audio queue, verify bounded behavior and drop policy
- Metrics: run a turn end-to-end, verify all metric values are captured
- Tracing: verify spans are emitted for each pipeline stage

## Acceptance Criteria

- [ ] WebSocket reconnect works with exponential backoff
- [ ] STT, agent, and TTS timeouts fire and produce error events
- [ ] Audio queues are bounded and stale audio is dropped on cancellation
- [ ] All five latency/count metrics are captured correctly
- [ ] Tracing spans cover each pipeline stage
- [ ] Agents SDK trace context is passed through
