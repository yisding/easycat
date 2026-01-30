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
- `dtmf(digit)`, `dtmf.aggregated(sequence)`
- `voicemail.detected(type=human|machine|unknown)`
- `error(exception)`

### Pipeline Orchestration

Wire the core pipeline skeleton:

```
Audio In -> (Noise Reduction) -> (VAD / Turn) -> STT -> Agent Workflow -> TTS -> Audio Out
```

Each stage is a pluggable interface. WS1 provides the orchestration; other workstreams provide implementations.

### Abstract Interfaces

Define Python ABCs / Protocols for:

- `STTProvider` — `start_stream()`, `send_audio(chunk)`, `end_stream()`
- `TTSProvider` — `synthesize(text)`, `stop()`, `cancel()`
- `VADProvider` — process audio, emit speech start/stop
- `NoiseReducer` — process audio chunk, return cleaned chunk
- `Transport` — `receive_audio()`, `send_audio()`, connect/disconnect lifecycle

### Audio Format Utilities

- Internal format contract: PCM16 mono with timestamps
- Resampling: 8 kHz <-> 16 kHz (minimum)
- Mono downmix
- Chunk sizing utilities (10-30ms frames for VAD)

## Unblocking Other Workstreams

Prioritize shipping the **interface definitions** (ABCs/Protocols + event types) as the first PR. Other workstreams can code against these interfaces immediately, even before the pipeline orchestration is wired up.

## Acceptance Criteria

- [ ] Session lifecycle works (start/stop/shutdown) with no-op provider stubs
- [ ] Events can be dispatched and subscribed to
- [ ] Pipeline runs end-to-end with stub providers (audio in -> stub noise -> stub VAD -> stub STT -> stub agent -> stub TTS -> audio out)
- [ ] Audio resampling 8k <-> 16k works correctly
- [ ] Chunk sizing produces correct frame sizes for VAD
