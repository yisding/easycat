# WS1: Core & Audio Foundation — Task Plan

## Phase 1: Interface Definitions (unblocks all other workstreams)

### Task 1.1: Define event types and dispatch system
- Define all event dataclasses/NamedTuples: `AudioIn`, `VADStartSpeaking`, `VADStopSpeaking`, `STTPartial`, `STTFinal`, `AgentDelta`, `AgentFinal`, `TTSAudio`, `TTSMarkers`, `DTMF`, `DTMFAggregated`, `VoicemailDetected`, `Error`
- Implement an `EventBus` (or similar) with `subscribe(event_type, callback)` and `emit(event)` supporting both sync and async listeners
- Events should be lightweight, serializable, and carry timestamps

### Task 1.2: Define provider ABCs / Protocols
- `STTProvider` — `start_stream()`, `send_audio(chunk)`, `end_stream()`, async iterator for events
- `TTSProvider` — `synthesize(text)` returning async iterator of audio chunks, `stop()`, `cancel()`
- `VADProvider` — `process(chunk)` emitting speech start/stop events, configure thresholds
- `NoiseReducer` — `process(chunk) -> chunk`
- `Transport` — `receive_audio() -> AsyncIterator[bytes]`, `send_audio(chunk)`, `connect()`, `disconnect()`
- All protocols should include type hints and docstrings describing expected behavior

### Task 1.3: Define audio format types and constants
- `AudioFormat` dataclass: sample_rate, channels, sample_width, encoding
- Standard constants: `PCM16_MONO_8K`, `PCM16_MONO_16K`
- `AudioChunk` type: raw bytes + format metadata + timestamp

## Phase 2: Audio Utilities

### Task 1.4: Implement PCM16 resampling (8kHz <-> 16kHz)
- Resample function: `resample(chunk, from_rate, to_rate) -> bytes`
- Support at minimum 8000 <-> 16000 Hz
- Use a well-known library (e.g., `audioop`, `scipy.signal`, or `soxr`)
- Unit tests with known audio samples verifying sample count and quality

### Task 1.5: Implement mono downmix
- `to_mono(chunk, channels) -> bytes`
- Handle stereo -> mono at minimum
- Unit tests

### Task 1.6: Implement chunk sizing utilities
- `chunk_frames(audio_stream, frame_duration_ms, sample_rate) -> Iterator[bytes]`
- Support 10ms, 20ms, 30ms frame sizes (common for VAD)
- Handle partial frames at end of stream
- Unit tests verifying frame byte lengths

## Phase 3: Session & Lifecycle

### Task 1.7: Implement Session class
- Constructor accepts provider config (STT, TTS, VAD, noise reducer, transport — all optional with defaults to no-op stubs)
- `start()` — initialize providers, begin transport audio receive loop
- `stop()` — gracefully stop current turn, close providers
- `shutdown()` — force-close everything, release resources
- Holds session state: current turn, is_speaking, is_bot_speaking
- Embeds the `EventBus` from Task 1.1

### Task 1.8: Implement cancellation methods
- `cancel_turn()` — abort current STT stream, discard partial results, reset turn state
- `cancel_tts_playback()` — stop TTS provider, flush outbound audio queue
- `reset_state()` — cancel everything, return to idle/listening state
- Each method emits appropriate events

### Task 1.9: Implement pipeline orchestration
- Wire the core loop: Audio In -> Noise Reduction -> VAD -> STT -> Agent -> TTS -> Audio Out
- Each stage pulls from the previous via async generators or event-driven callbacks
- The pipeline should work end-to-end with no-op stub providers
- Configurable: stages can be skipped (e.g., no noise reduction)

## Phase 4: Validation

### Task 1.10: End-to-end smoke test with stubs
- Create no-op stubs for every provider
- Run a full session lifecycle: start -> feed audio -> stub VAD triggers -> stub STT returns text -> stub agent returns text -> stub TTS returns audio -> audio out -> stop
- Verify all expected events fire in correct order
- Verify session state transitions are correct
