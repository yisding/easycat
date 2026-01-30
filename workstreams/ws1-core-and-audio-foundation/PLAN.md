# WS1: Core & Audio Foundation — Task Plan

## Phase 1: Interface Definitions (unblocks all other workstreams)

### Task 1.1: Define event types and dispatch system ✅
- Define all event dataclasses/NamedTuples: `AudioIn`, `VADStartSpeaking`, `VADStopSpeaking`, `STTPartial`, `STTFinal`, `AgentDelta`, `AgentFinal`, `TTSAudio`, `TTSMarkers`, `DTMF`, `DTMFAggregated`, `VoicemailDetected`, `Error`
- Implement an `EventBus` (or similar) with `subscribe(event_type, callback)` and `emit(event)` supporting both sync and async listeners
- Events should be lightweight, serializable, and carry timestamps

**Implementation:** `src/easycat/events.py` — 13 frozen dataclasses with timestamps, `EventBus` with sync/async handler dispatch and error isolation. Tests: `tests/test_events.py` (14 tests).

### Task 1.2: Define provider ABCs / Protocols ✅
- `STTProvider` — `start_stream()`, `send_audio(chunk)`, `end_stream()`, async iterator for events
- `TTSProvider` — `synthesize(text)` returning async iterator of audio chunks, `stop()`, `cancel()`
- `VADProvider` — `process(chunk)` emitting speech start/stop events, configure thresholds
- `NoiseReducer` — `process(chunk) -> chunk`
- `Transport` — `receive_audio() -> AsyncIterator[bytes]`, `send_audio(chunk)`, `connect()`, `disconnect()`
- All protocols should include type hints and docstrings describing expected behavior

**Implementation:** `src/easycat/providers.py` — 5 `@runtime_checkable` Protocol classes with full type hints and docstrings. Tests: `tests/test_providers.py` (5 structural subtyping tests).

### Task 1.3: Define audio format types and constants ✅
- `AudioFormat` dataclass: sample_rate, channels, sample_width, encoding
- Standard constants: `PCM16_MONO_8K`, `PCM16_MONO_16K`
- `AudioChunk` type: raw bytes + format metadata + timestamp

**Implementation:** `src/easycat/audio_format.py` — `AudioFormat` (frozen dataclass with computed properties), `AudioChunk` (with `num_samples`, `duration_ms`), two standard constants. Tests: `tests/test_audio_format.py` (6 tests).

## Phase 2: Audio Utilities

### Task 1.4: Implement PCM16 resampling (8kHz <-> 16kHz) ✅
- Resample function: `resample(chunk, from_rate, to_rate) -> bytes`
- Support at minimum 8000 <-> 16000 Hz
- Use a well-known library (e.g., `audioop`, `scipy.signal`, or `soxr`)
- Unit tests with known audio samples verifying sample count and quality

**Implementation:** `src/easycat/audio_utils.py` — `resample()` using linear interpolation with int16 clamping, plus `resample_chunk()` convenience wrapper. Tests: `tests/test_audio_utils.py` (6 resampling tests including sample count, DC signal preservation).

### Task 1.5: Implement mono downmix ✅
- `to_mono(chunk, channels) -> bytes`
- Handle stereo -> mono at minimum
- Unit tests

**Implementation:** `src/easycat/audio_utils.py` — `to_mono()` averaging multi-channel PCM16 samples, plus `to_mono_chunk()` wrapper. Tests: `tests/test_audio_utils.py` (5 downmix tests).

### Task 1.6: Implement chunk sizing utilities ✅
- `chunk_frames(audio_stream, frame_duration_ms, sample_rate) -> Iterator[bytes]`
- Support 10ms, 20ms, 30ms frame sizes (common for VAD)
- Handle partial frames at end of stream
- Unit tests verifying frame byte lengths

**Implementation:** `src/easycat/audio_utils.py` — `chunk_frames()` yielding fixed-duration frames with partial tail frame support. Tests: `tests/test_audio_utils.py` (6 chunk sizing tests at 8k/16k with 10/20/30ms frames).

## Phase 3: Session & Lifecycle

### Task 1.7: Implement Session class ✅
- Constructor accepts provider config (STT, TTS, VAD, noise reducer, transport — all optional with defaults to no-op stubs)
- `start()` — initialize providers, begin transport audio receive loop
- `stop()` — gracefully stop current turn, close providers
- `shutdown()` — force-close everything, release resources
- Holds session state: current turn, is_speaking, is_bot_speaking
- Embeds the `EventBus` from Task 1.1

**Implementation:** `src/easycat/session.py` — `Session` class with `SessionConfig`, `TurnState` enum (IDLE/LISTENING/PROCESSING/BOT_SPEAKING), no-op stubs from `src/easycat/stubs.py`. Tests: `tests/test_session.py` (5 lifecycle tests).

### Task 1.8: Implement cancellation methods ✅
- `cancel_turn()` — abort current STT stream, discard partial results, reset turn state
- `cancel_tts_playback()` — stop TTS provider, flush outbound audio queue
- `reset_state()` — cancel everything, return to idle/listening state
- Each method emits appropriate events

**Implementation:** `src/easycat/session.py` — `cancel_turn()`, `cancel_tts_playback()`, `reset_state()` with internal `_cancel_stt()` and `_cancel_tts()` helpers. Tests: `tests/test_session.py` (3 cancellation tests).

### Task 1.9: Implement pipeline orchestration ✅
- Wire the core loop: Audio In -> Noise Reduction -> VAD -> STT -> Agent -> TTS -> Audio Out
- Each stage pulls from the previous via async generators or event-driven callbacks
- The pipeline should work end-to-end with no-op stub providers
- Configurable: stages can be skipped (e.g., no noise reduction)

**Implementation:** `src/easycat/session.py` — `_run_pipeline()` async loop with configurable `enable_noise_reduction` and `enable_vad` flags, `_handle_end_of_speech()` driving STT→Agent→TTS→Transport. Tests: `tests/test_session.py` (5 pipeline tests including full turn and skip-empty-transcript).

## Phase 4: Validation

### Task 1.10: End-to-end smoke test with stubs ✅
- Create no-op stubs for every provider
- Run a full session lifecycle: start -> feed audio -> stub VAD triggers -> stub STT returns text -> stub agent returns text -> stub TTS returns audio -> audio out -> stop
- Verify all expected events fire in correct order
- Verify session state transitions are correct

**Implementation:** `tests/test_smoke.py` — Full end-to-end test with custom stub providers verifying: all 7 event types fire, correct ordering (AudioIn < VADStart < VADStop < STTFinal < AgentFinal < TTSAudio), content correctness, transport output, and final session state.

---

## Summary

| Phase | Tasks | Status |
|-------|-------|--------|
| Phase 1: Interface Definitions | 1.1, 1.2, 1.3 | ✅ Complete |
| Phase 2: Audio Utilities | 1.4, 1.5, 1.6 | ✅ Complete |
| Phase 3: Session & Lifecycle | 1.7, 1.8, 1.9 | ✅ Complete |
| Phase 4: Validation | 1.10 | ✅ Complete |

**All 56 tests passing. Ruff lint and format clean.**

### Files Created

| File | Purpose |
|------|---------|
| `src/easycat/__init__.py` | Public API exports |
| `src/easycat/audio_format.py` | AudioFormat, AudioChunk, PCM16 constants |
| `src/easycat/audio_utils.py` | Resampling, mono downmix, chunk sizing |
| `src/easycat/events.py` | Event dataclasses + EventBus |
| `src/easycat/providers.py` | Protocol interfaces (STT, TTS, VAD, NoiseReducer, Transport) |
| `src/easycat/session.py` | Session class, lifecycle, cancellation, pipeline |
| `src/easycat/stubs.py` | No-op stub providers for defaults |
| `tests/test_audio_format.py` | Audio format and chunk tests |
| `tests/test_audio_utils.py` | Resampling, downmix, chunk sizing tests |
| `tests/test_events.py` | Event dataclass and EventBus tests |
| `tests/test_providers.py` | Protocol conformance tests |
| `tests/test_session.py` | Session lifecycle, cancellation, pipeline tests |
| `tests/test_smoke.py` | End-to-end smoke test |
