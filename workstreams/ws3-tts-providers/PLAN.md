# WS3: TTS Providers — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (interfaces and audio types).
> All three providers can be developed in parallel by different engineers once the interface exists.

## Phase 1: Shared Utilities

### Task 3.1: TTS base class and test harness ✅
- [x] Implement a concrete base class extending `TTSProvider` with shared logic: audio format conversion to PCM16, event emission, cancellation state tracking
- [x] Create a test harness: send text to any TTS provider, collect audio chunks, verify playable output
- [x] Helper to write collected audio chunks to a WAV file for manual listening tests

**Files:**
- `src/easycat/tts/base.py` — `TTSBase` class with shared normalization, event helpers, and cancel tracking
- `src/easycat/tts/test_harness.py` — `collect_tts_output`, `extract_audio_chunks`, `write_wav`, `verify_pcm16_audio`
- `tests/tts/test_tts_base.py` — 19 tests covering base class, FakeTTS, and harness utilities

## Phase 2: Provider Implementations (parallel)

### Task 3.2: OpenAI TTS provider ✅
- [x] Implement `OpenAITTS(TTSProvider)`
- [x] Use Audio API (`audio/speech`) with streaming response
- [x] `synthesize(text)` returns an async iterator of PCM16 audio chunks
- [x] Support `stop()` / `cancel()` by closing the HTTP stream mid-response
- [x] Convert from API output format to internal PCM16 if needed
- [x] Config: model, voice, speed, response_format
- [x] Unit tests with mocked HTTP streaming response
- [x] Integration test (gated behind `OPENAI_API_KEY`)

**Files:**
- `src/easycat/tts/openai_tts.py` — `OpenAITTS` and `OpenAITTSConfig`
- `tests/tts/test_tts_openai.py` — 9 tests (7 unit + 1 integration + config tests)

### Task 3.3: Deepgram TTS (Aura) provider ✅
- [x] Implement `DeepgramTTS(TTSProvider)`
- [x] WebSocket streaming TTS: open connection, send text continuously, receive audio stream
- [x] **Use `ReconnectingWebSocket`** wrapper for WebSocket lifecycle — do not implement bespoke reconnection
- [x] `synthesize(text)` sends text over the WebSocket and yields audio chunks
- [x] Support `stop()` / `cancel()` by sending close/flush message and discarding remaining audio
- [x] Request PCM/linear16 output format from Deepgram directly if supported; convert only if not available
- [x] Config: model, encoding, sample_rate
- [x] Unit tests with mocked WebSocket
- [x] Integration test (gated behind `DEEPGRAM_API_KEY`)

**Files:**
- `src/easycat/tts/deepgram_tts.py` — `DeepgramTTS` and `DeepgramTTSConfig`
- `tests/tts/test_tts_deepgram.py` — 10 tests (8 unit + 1 integration + config tests)

### Task 3.4: ElevenLabs TTS provider ✅
- [x] Implement `ElevenLabsTTS(TTSProvider)`
- [x] Support streaming TTS via chunked transfer encoding (HTTP)
- [x] Also support WebSocket streaming option — **use `ReconnectingWebSocket`** for WebSocket lifecycle
- [x] `synthesize(text)` returns async iterator of PCM16 audio chunks
- [x] Request PCM output format from ElevenLabs if supported; convert only if not available
- [x] Support `stop()` / `cancel()` by aborting the stream
- [x] Config: voice_id, model_id, stability, similarity_boost, output_format
- [x] Unit tests with mocked HTTP/WebSocket responses
- [x] Integration test (gated behind `ELEVENLABS_API_KEY`)

**Files:**
- `src/easycat/tts/elevenlabs_tts.py` — `ElevenLabsTTS`, `ElevenLabsTTSConfig`, `ElevenLabsStreamMode`
- `tests/tts/test_tts_elevenlabs.py` — 14 tests (12 unit + 1 integration + config tests)

## Phase 3: Output Handling & Validation

### Task 3.5: Audio output format normalization ✅
- [x] **Prefer requesting PCM/linear16 directly from each provider** when supported — avoid introducing ffmpeg or system-level audio decoding dependencies for MP3/Opus unless no PCM output option exists for a given provider
- [x] Ensure all providers output PCM16 mono at a consistent sample rate
- [x] Implement format conversion in the base class only for providers that cannot return PCM directly (e.g., mulaw from telephony paths)
- [x] Use the audio utilities from WS1 (Task 1.4) for resampling

**Implementation:** `TTSBase._normalize_audio()` handles mono downmix via `to_mono()` and sample rate conversion via `resample()` from WS1.

### Task 3.6: Mid-utterance cancellation tests ✅
- [x] For each provider, test the cancel path:
  - [x] Start synthesis of a long text
  - [x] Cancel partway through
  - [x] Verify no more audio chunks are yielded after cancel
  - [x] Verify the provider connection is cleaned up (no resource leaks)

**Files:**
- `tests/tts/test_tts_cancellation.py` — 10 tests covering all providers (OpenAI, Deepgram, ElevenLabs HTTP, ElevenLabs WS)

### Task 3.7: Provider selection and factory ✅
- [x] Implement `create_tts_provider(config) -> TTSProvider` factory function
- [x] Config specifies provider name + provider-specific settings
- [x] Validate config at construction time

**Files:**
- `src/easycat/tts/factory.py` — `create_tts_provider`, `TTSProviderConfig`
- `tests/tts/test_tts_factory.py` — 13 tests covering creation, validation, and settings

## Additional Infrastructure

### ReconnectingWebSocket wrapper ✅
- [x] Minimal `ReconnectingWebSocket` implementation for TTS providers (to be superseded by WS8's full reliability implementation)
- [x] Automatic reconnection with exponential backoff
- [x] Send/receive/close lifecycle
- [x] Unit tests

**Files:**
- `src/easycat/reconnecting_ws.py` — `ReconnectingWebSocket`, `ReconnectConfig`
- `tests/websocket/test_reconnecting_ws.py` — 13 tests

## Summary

**Status: COMPLETE** — All 7 tasks across 3 phases implemented and tested.

| Metric | Count |
|--------|-------|
| Source files added | 7 (5 in `tts/` + `reconnecting_ws.py` + `tts/__init__.py`) |
| Test files added | 6 |
| Total tests | 88 (85 unit + 3 integration, skipped without API keys) |
| Dependencies added | `httpx`, `websockets` |
