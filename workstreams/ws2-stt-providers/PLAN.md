# WS2: STT Providers — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (interfaces and audio types).
> All three providers can be developed in parallel by different engineers once the interface exists.

## Phase 1: Shared Utilities

### Task 2.1: STT base class and test harness ✅
- Implement a concrete base class extending `STTProvider` with shared logic: `STTEvent` production via `events()` async iterator, error wrapping, audio format validation
- Providers produce `STTEvent` objects (not EasyCat events) — the Session is responsible for mapping these to `stt.partial`/`stt.final`
- Create a test harness: feed recorded WAV audio through any STT provider and collect `STTEvent` objects
- Provide sample audio files (short utterances, silence, noisy speech) in a test fixtures directory

## Phase 2: Provider Implementations (parallel)

### Task 2.2: OpenAI STT provider ✅
- Implement `OpenAISTT(STTProvider)`
- Use the Audio API transcriptions endpoint (`gpt-4o-transcribe` model)
- Turn-based: accept complete audio buffers (from VAD-segmented turns), submit via API, return final transcript
- Since this is turn-based (not streaming), `send_audio(chunk)` buffers internally; `end_stream()` triggers the API call
- **Important:** WS4/WS1 must provide a turn-finalization trigger (e.g., `TurnEnded` event or direct `end_stream()` call) at the right moment to submit the buffered audio — coordinate with WS4's TurnManager design
- Handle: API errors, rate limits, retries with backoff
- Config: model, language, prompt (optional context)
- Unit tests with mocked HTTP responses
- Integration test (gated behind `OPENAI_API_KEY` env var)

### Task 2.3: Deepgram streaming STT provider ✅
- Implement `DeepgramSTT(STTProvider)`
- Open a WebSocket to Deepgram's `listen-streaming` endpoint on `start_stream()`
- Forward audio chunks via `send_audio(chunk)`
- Parse incoming WebSocket messages for partial and final transcript events
- Emit `stt.partial(text)` and `stt.final(text)` as they arrive
- Handle WebSocket lifecycle: connect, keepalive, close on `end_stream()`
- Plan to use WS8's `ReconnectingWebSocket` wrapper for reconnect logic; current implementation uses direct `websockets` with a `ws_connect` override and will be swapped once WS8 is available
- Config: model, language, encoding, sample_rate, punctuate, interim_results
- Unit tests with mocked WebSocket
- Integration test (gated behind `DEEPGRAM_API_KEY`)

### Task 2.4: ElevenLabs STT provider ✅
- Implement `ElevenLabsSTT(STTProvider)`
- Support realtime WebSocket speech-to-text API
- Also support batch transcription endpoint as a fallback mode
- Emit partial and final transcript events
- Config: model, language
- Unit tests with mocked responses
- Integration test (gated behind `ELEVENLABS_API_KEY`)

## Phase 3: Normalization & Validation

### Task 2.5: Transcript normalization layer ✅
- Ensure all three providers normalize output to a common format:
  - `text: str`
  - `is_final: bool`
  - `confidence: Optional[float]`
  - `language: Optional[str]`
  - `timestamps: Optional[List[WordTimestamp]]`
- Write comparison tests: same audio through all providers, verify output **schema and contract** are consistent (field types, presence of is_final, etc.) — do **not** assert text equivalence across providers, as vendor transcription results differ

### Task 2.6: Provider selection and factory ✅
- Implement `create_stt_provider(config) -> STTProvider` factory function
- Config specifies provider name + provider-specific settings
- Validate config at construction time (fail fast on missing API keys, bad params)

---

## Completion Summary

All 6 tasks completed. 171 tests passing (90 WS1 + 81 WS2), ruff lint and format clean.

### Key files:
- `src/easycat/events.py` — Extended `STTEvent` with normalization fields (`confidence`, `language`, `word_timestamps`) and added `WordTimestamp` dataclass
- `src/easycat/stt/base.py` — `STTBase` concrete base class with event queue, audio validation, stream lifecycle; `pcm_to_wav()` utility
- `src/easycat/stt/openai_provider.py` — `OpenAISTT` turn-based transcription via Audio API; buffers audio, converts to WAV, submits on end_stream; retries with backoff
- `src/easycat/stt/deepgram_provider.py` — `DeepgramSTT` real-time WebSocket streaming; background receive loop parses partial/final results
- `src/easycat/stt/elevenlabs_provider.py` — `ElevenLabsSTT` dual-mode: realtime WebSocket + batch HTTP transcription
- `src/easycat/stt/factory.py` — `create_stt_provider()` factory with config validation and `STTProviderConfig` dataclass
- `src/easycat/stt/__init__.py` — Package exports
- `tests/stt_helpers.py` — Test harness (`collect_stt_events`), PCM audio generators (sine, silence, noise), chunk splitter
- `tests/test_stt_base.py` — 18 tests: base class lifecycle, validation, WAV conversion, harness functions, protocol conformance
- `tests/test_stt_openai.py` — 10 tests: transcription, config, auth, errors, reusability
- `tests/test_stt_deepgram.py` — 12 tests: streaming, partial/final, metadata, URL building, reusability
- `tests/test_stt_elevenlabs.py` — 18 tests: realtime + batch modes, auth, confidence, word timestamps, errors
- `tests/test_stt_normalization.py` — 10 tests: schema consistency across all providers, confidence/timestamp coverage
- `tests/test_stt_factory.py` — 10 tests: provider creation, param forwarding, validation, protocol conformance

### Notes:
- WebSocket providers (Deepgram, ElevenLabs) accept a `ws_connect` override for testing; will switch to WS8's `ReconnectingWebSocket` when available
- HTTP providers (OpenAI, ElevenLabs batch) accept an `http_client` override for testing
- Added `httpx` and `websockets` as project dependencies (WS9 will move to optional extras)
