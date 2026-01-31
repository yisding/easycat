# WS1: Core Runtime & Audio Foundation

**Features:** #1 (Core Runtime), #10 (Audio & Format Handling)
**Parallel with:** None — this workstream defines interfaces that all others depend on. However, interface definitions should be finalized early so other workstreams can start coding against them before WS1 implementation is complete.

## Goal

Establish the session model, event system, lifecycle management, and audio format primitives that every other workstream builds on.

## Deliverables

### Session Object

- `Session` class (one per call / per websocket client)
- Lifecycle methods: `start()`, `stop()`, `shutdown()`
- Cancellation methods: `cancel_turn()`, `cancel_tts_playback()`, `reset_state()`
- Holds references to configured providers (STT, TTS, VAD, noise reduction, transport)

### Event Model

Define the event types and dispatch mechanism (streaming-first):

- `audio_in(chunk)`
- `vad.start_speaking`, `vad.stop_speaking`
- `stt.partial(text)`, `stt.final(text)`
- `agent.delta(text)`, `agent.final(text)`
- `tts.audio(chunk)`, `tts.markers(...)`
- `bot.started_speaking`, `bot.stopped_speaking`
- `turn.started`, `turn.ended`
- `interruption(details)` — user barged in while bot was speaking
- `tool.call_started(tool_name, call_id)`, `tool.call_delta(call_id, delta)`, `tool.call_result(call_id, result)`
- `reconnect.attempt(provider, attempt)`, `reconnect.success(provider)`, `reconnect.failure(provider, error)`
- `dtmf(digit)`, `dtmf.aggregated(sequence)`
- `voicemail.detected(type=human|machine|unknown)`
- `error(exception)`

**Design note:** These are *EasyCat-level events* emitted by the Session. Providers produce their own provider-scoped events (e.g., `STTEvent`, `TTSEvent`) via async iterators; the Session is the single place that maps provider events to EasyCat events. This keeps provider implementations lean and testable.

### Pipeline Orchestration

Wire the core pipeline skeleton:

```
Audio In -> (Noise Reduction) -> (VAD / Turn) -> STT -> Agent Workflow -> TTS -> Audio Out
```

Each stage is a pluggable interface. WS1 provides the orchestration; other workstreams provide implementations.

### Abstract Interfaces

Define Python ABCs / Protocols for:

- `STTProvider` — `start_stream()`, `send_audio(chunk)`, `end_stream()`, `events() -> AsyncIterator[STTEvent]`
- `TTSProvider` — `synthesize(text) -> AsyncIterator[TTSEvent]`, `stop()`, `cancel()`
- `VADProvider` — process audio, emit speech start/stop
- `NoiseReducer` — process audio chunk, return cleaned chunk
- `Transport` — `receive_audio()`, `send_audio()`, connect/disconnect lifecycle

**Provider event semantics:** Providers produce *provider-scoped events* via async iterators (e.g., `STTEvent` with partial/final variants, `TTSEvent` with audio/marker variants). The Session consumes these iterators and emits the corresponding EasyCat-level events (e.g., `stt.partial`, `stt.final`). Providers never emit EasyCat events directly. This makes testing, backpressure, and cancellation straightforward.

### Cancellation Model

A unified *cancel token* (or equivalent) per turn, so that barge-in can cancel:

- Ongoing TTS playback
- Ongoing agent streaming
- Any queued outbound audio
- Pending STT streams

All pipeline stages check the cancel token cooperatively. The `cancel_turn()` method on Session triggers cancellation across the entire pipeline for the current turn.

### Audio Format Utilities

- Internal format contract: PCM16 mono with timestamps
- Resampling: support arbitrary rates (8 kHz / 16 kHz / 24 kHz / 48 kHz) — required because RNNoise expects 48 kHz input, providers may use 24 kHz, and telephony uses 8 kHz
- Mono downmix
- Chunk sizing utilities (10-30ms frames for VAD)

## Unblocking Other Workstreams

Prioritize shipping the **interface definitions** (ABCs/Protocols + event types) as the first PR. Other workstreams can code against these interfaces immediately, even before the pipeline orchestration is wired up.

## Acceptance Criteria

- [x] Session lifecycle works (start/stop/shutdown) with no-op provider stubs
- [x] Events can be dispatched and subscribed to
- [x] Pipeline runs end-to-end with stub providers (audio in -> stub noise -> stub VAD -> stub STT -> stub agent -> stub TTS -> audio out)
- [x] Audio resampling works for all supported rates (8k, 16k, 24k, 48k)
- [x] Chunk sizing produces correct frame sizes for VAD
- [x] Cancel token propagates across all pipeline stages
- [x] Provider event iterators are consumed by Session and mapped to EasyCat events
