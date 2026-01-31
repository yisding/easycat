# WS8: Reliability & Observability — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.2 (session model, event types).
> Produces cross-cutting utilities. Other workstreams integrate these later, but development is independent.
>
> **Status: ✅ COMPLETE** — All 11 tasks across 5 phases implemented and tested.

## Phase 1: Reconnection

### Task 8.1: WebSocket reconnect strategy ✅
- Implement `ReconnectingWebSocket` wrapper that **WS2 (STT), WS3 (TTS), and WS5 (transports) must use** — this is the single source of reconnect logic in the project
- Exponential backoff with jitter (base delay, max delay, jitter factor — all configurable)
- Configurable max retry count (with option for unlimited)
- Emit `reconnect.attempt`, `reconnect.success`, `reconnect.failure` events (defined in WS1 event model) into the Session event bus
- Preserve any pending state (e.g., re-send audio stream config on reconnect for STT providers)
- Callbacks: `on_reconnect`, `on_give_up` for provider-specific recovery logic
- **Cross-workstream note:** WS2 (Deepgram, ElevenLabs STT), WS3 (Deepgram, ElevenLabs TTS), and WS5 (Twilio transport) should be updated to depend on this wrapper. Their PLANs already reference this dependency.
- Unit tests: simulate disconnect -> verify reconnect attempts with correct backoff timing

> **Impl:** `src/easycat/reconnecting_ws.py` — Enhanced with `jitter_factor`, `event_bus` integration, `on_reconnect`/`on_give_up` callbacks, unlimited retries (`max_retries=-1`). Tests: `tests/test_reconnecting_ws.py`.

### Task 8.2: Provider health check pattern ✅
- Define a `health_check()` method on provider protocols (optional, providers can implement)
- Periodically ping providers to detect stale connections before they fail
- Log or emit events on health check failures
- Test: mock a stale WebSocket -> verify health check detects it

> **Impl:** `src/easycat/health_check.py` — `HealthCheckable` protocol, `PeriodicHealthChecker` with configurable interval and EventBus error emission. Tests: `tests/test_health_check.py`.

## Phase 2: Timeouts

### Task 8.3: STT response timeout ✅
- Configurable timeout from audio submission to transcript receipt
- If STT provider doesn't return a transcript within the timeout:
  - Emit `error(STTTimeoutError)` with context (provider name, timeout value)
  - Cancel the pending STT stream
  - Allow session to recover
- Test: mock slow STT provider -> verify timeout fires and session recovers

> **Impl:** `src/easycat/timeouts.py` — `with_stt_timeout()` async generator wrapper. Tests: `tests/test_timeouts.py`.

### Task 8.4: Agent run timeout ✅
- Configurable timeout from transcript dispatch to agent response
- If agent doesn't respond within timeout:
  - Emit `error(AgentTimeoutError)`
  - Cancel the agent invocation (if possible)
  - Return session to listening state
- Test: mock hanging agent -> verify timeout and recovery

> **Impl:** `src/easycat/timeouts.py` — `with_agent_timeout()` coroutine wrapper. Tests: `tests/test_timeouts.py`.

### Task 8.5: TTS first-byte timeout ✅
- Configurable timeout from text submission to first audio byte
- If TTS provider doesn't produce audio within timeout:
  - Emit `error(TTSTimeoutError)`
  - Cancel the TTS request
  - Allow session to continue (user can speak again)
- Test: mock slow TTS provider -> verify timeout fires

> **Impl:** `src/easycat/timeouts.py` — `with_tts_timeout()` async generator wrapper (timeout applies only to first byte). Tests: `tests/test_timeouts.py`.

## Phase 3: Backpressure

### Task 8.6: Bounded audio queues ✅
- Implement `BoundedAudioQueue` with configurable max size (in chunks or bytes)
- Used for both inbound (mic -> processing) and outbound (TTS -> playback) audio
- When queue is full, apply a configurable policy:
  - **drop_oldest** — discard oldest chunks (default for inbound)
  - **drop_newest** — discard incoming chunks
  - **block** — back-pressure the producer (with timeout)
- Emit warnings/metrics when drops occur
- Unit tests: fill queue beyond capacity -> verify drop policy

> **Impl:** `src/easycat/bounded_queue.py` — `BoundedAudioQueue` with `DropPolicy` enum (DROP_OLDEST, DROP_NEWEST, BLOCK). Tests: `tests/test_bounded_queue.py`.

### Task 8.7: Stale audio flush on cancellation ✅
- When a turn is canceled (`cancel_turn()`, barge-in), flush audio queues
- Discard any audio that was buffered for the canceled turn
- Ensure the next turn starts with clean queues
- Test: queue has audio from turn 1 -> cancel -> verify queue is empty for turn 2

> **Impl:** `src/easycat/bounded_queue.py` — `flush_for_new_turn()` advances turn_id, resets drops, clears queue. Tests: `tests/test_bounded_queue.py`.

## Phase 4: Metrics

### Task 8.8: Metrics collection framework ✅
- Define a `MetricsCollector` interface with methods:
  - `record_latency(name, value_ms)`
  - `increment_counter(name)`
  - `get_metrics() -> dict` (for retrieval/export)
- Provide a default in-memory implementation
- Allow pluggable backends (e.g., Prometheus, StatsD) via the interface
- Collect at minimum:
  - `stt_latency_ms` — end of user speech to final transcript
  - `agent_latency_ms` — transcript to first agent response
  - `tts_ttfb_ms` — text submission to first audio byte
  - `turn_end_to_end_ms` — end of user speech to first bot audio
  - Counts: `interruptions`, `reconnects`, `errors`
- Unit tests: record metrics, retrieve and verify values

> **Impl:** `src/easycat/metrics.py` — `MetricsCollector` protocol, `InMemoryMetrics` with `LatencyStats`, standard metric name constants. Tests: `tests/test_metrics.py`.

### Task 8.9: Metrics integration points ✅
- Define where in the pipeline each metric is captured (document the measurement points)
- Provide helper decorators or context managers that other workstreams can wrap around their code:
  - `@timed_metric("stt_latency_ms")` or `with metrics.time("stt_latency_ms"):`
- Test: wrap a mock function -> verify latency is recorded

> **Impl:** `src/easycat/metrics.py` — `@timed_metric()` decorator, `measure_latency()` async context manager, `measure_latency_sync()` sync context manager. Tests: `tests/test_metrics.py`.

## Phase 5: Tracing

### Task 8.10: Tracing span infrastructure ✅
- Implement span creation for EasyCat pipeline stages:
  - `noise_reduction`, `vad`, `stt`, `agent`, `tts`
- Each span records: start time, end time, status, metadata
- Integrate with Agents SDK trace context pass-through (from WS7)
- Provide a default in-memory trace exporter
- Optional: OpenTelemetry-compatible span export (stretch goal)
- Unit tests: run a pipeline stage -> verify span is recorded with correct timing

> **Impl:** `src/easycat/tracing.py` — `Span`, `SpanStatus`, `TraceContext`, `TraceExporter`, `InMemoryTraceExporter`, `Tracer` with async/sync context managers. Tests: `tests/test_tracing.py`.

### Task 8.11: Trace context propagation ✅
- Ensure trace context flows through the full pipeline:
  - Session start -> noise reduction -> VAD -> STT -> agent -> TTS -> audio out
- Each stage creates a child span under the session's root span
- If the Agents SDK provides trace IDs, link EasyCat spans to them
- Test: run a full turn -> verify all spans are linked under one trace

> **Impl:** `src/easycat/tracing.py` — `TraceContext` with `root_span_id` and `agent_trace_id` fields; `Tracer.trace()` creates child spans linked to root. Tests: `tests/test_tracing.py`.

---

## Summary

All 11 tasks across 5 phases are **complete**. New modules:

| Module | Tasks | Key exports |
|--------|-------|-------------|
| `reconnecting_ws.py` | 8.1 | `ReconnectingWebSocket`, `ReconnectConfig` |
| `health_check.py` | 8.2 | `HealthCheckable`, `PeriodicHealthChecker` |
| `timeouts.py` | 8.3–8.5 | `with_stt_timeout`, `with_agent_timeout`, `with_tts_timeout`, error types |
| `bounded_queue.py` | 8.6–8.7 | `BoundedAudioQueue`, `DropPolicy` |
| `metrics.py` | 8.8–8.9 | `MetricsCollector`, `InMemoryMetrics`, `timed_metric`, `measure_latency` |
| `tracing.py` | 8.10–8.11 | `Tracer`, `TraceContext`, `Span`, `InMemoryTraceExporter` |
