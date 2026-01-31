# WS1: Core & Audio Foundation — Task Plan

## Phase 1: Interface Definitions (unblocks all other workstreams)

### Task 1.1: Define event types and dispatch system
- Define all EasyCat-level event dataclasses/NamedTuples:
  - Audio: `AudioIn`
  - VAD: `VADStartSpeaking`, `VADStopSpeaking`
  - STT: `STTPartial`, `STTFinal`
  - Agent: `AgentDelta`, `AgentFinal`
  - TTS: `TTSAudio`, `TTSMarkers`
  - Lifecycle: `BotStartedSpeaking`, `BotStoppedSpeaking`, `TurnStarted`, `TurnEnded`
  - Interruption: `Interruption` (user barged in while bot was speaking)
  - Tools: `ToolCallStarted`, `ToolCallDelta`, `ToolCallResult`
  - Reconnect: `ReconnectAttempt`, `ReconnectSuccess`, `ReconnectFailure`
  - Telephony: `DTMF`, `DTMFAggregated`, `VoicemailDetected`
  - Error: `Error`
- Also define *provider-scoped event types* for use in provider async iterators:
  - `STTEvent` (with `partial` / `final` variants)
  - `TTSEvent` (with `audio` / `markers` variants)
  - These are internal to provider implementations; Session maps them to EasyCat events
- Implement an `EventBus` (or similar) with `subscribe(event_type, callback)` and `emit(event)` supporting both sync and async listeners
- Events should be lightweight, serializable, and carry timestamps

### Task 1.2: Define provider ABCs / Protocols
- `STTProvider` — `start_stream()`, `send_audio(chunk)`, `end_stream()`, `events() -> AsyncIterator[STTEvent]`
  - Providers produce `STTEvent` objects via the `events()` iterator; Session consumes them and emits `stt.partial`/`stt.final` EasyCat events
  - Providers never emit EasyCat events directly
- `TTSProvider` — `synthesize(text) -> AsyncIterator[TTSEvent]`, `stop()`, `cancel()`
  - Same pattern: providers yield `TTSEvent` objects; Session maps them to EasyCat events
- `VADProvider` — `process(chunk)` emitting speech start/stop events, configure thresholds
- `NoiseReducer` — `process(chunk) -> chunk`
- `Transport` — `receive_audio() -> AsyncIterator[bytes]`, `send_audio(chunk)`, `connect()`, `disconnect()`
- All protocols should include type hints and docstrings describing expected behavior

### Task 1.3: Define audio format types and constants
- `AudioFormat` dataclass: sample_rate, channels, sample_width, encoding
- Standard constants: `PCM16_MONO_8K`, `PCM16_MONO_16K`, `PCM16_MONO_24K`, `PCM16_MONO_48K`
- `AudioChunk` type: raw bytes + format metadata + timestamp

## Phase 2: Audio Utilities

### Task 1.4: Implement PCM16 resampling (arbitrary rates)
- Resample function: `resample(chunk, from_rate, to_rate) -> bytes`
- Support at minimum: 8000, 16000, 24000, 48000 Hz (any combination)
- 48 kHz is required because RNNoise expects 48 kHz float32 input; 24 kHz may be needed for some TTS providers; 8 kHz for telephony
- Use a well-known library (e.g., `soxr` preferred for quality, or `scipy.signal`)
- Note: `audioop` is deprecated as of Python 3.11 and removed in 3.13 — do not use it
- Unit tests with known audio samples verifying sample count and quality for each rate pair

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

### Task 1.8: Implement cancellation model and methods
- Implement a `CancelToken` (or equivalent) per turn that all pipeline stages check cooperatively
- Barge-in cancellation must propagate to: ongoing TTS playback, ongoing agent streaming, queued outbound audio, and pending STT streams
- `cancel_turn()` — trigger the cancel token, abort current STT stream, cancel agent streaming, discard partial results, reset turn state
- `cancel_tts_playback()` — stop TTS provider, flush outbound audio queue
- `reset_state()` — cancel everything, return to idle/listening state
- Each method emits appropriate events (including `Interruption` when triggered by barge-in)

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
