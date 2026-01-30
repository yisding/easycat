# WS3: TTS Providers — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (interfaces and audio types).
> All three providers can be developed in parallel by different engineers once the interface exists.

## Phase 1: Shared Utilities

### Task 3.1: TTS base class and test harness
- Implement a concrete base class extending `TTSProvider` with shared logic: audio format conversion to PCM16, event emission, cancellation state tracking
- Create a test harness: send text to any TTS provider, collect audio chunks, verify playable output
- Helper to write collected audio chunks to a WAV file for manual listening tests

## Phase 2: Provider Implementations (parallel)

### Task 3.2: OpenAI TTS provider
- Implement `OpenAITTS(TTSProvider)`
- Use Audio API (`audio/speech`) with streaming response
- `synthesize(text)` returns an async iterator of PCM16 audio chunks
- Support `stop()` / `cancel()` by closing the HTTP stream mid-response
- Convert from API output format to internal PCM16 if needed
- Config: model, voice, speed, response_format
- Unit tests with mocked HTTP streaming response
- Integration test (gated behind `OPENAI_API_KEY`)

### Task 3.3: Deepgram TTS (Aura) provider
- Implement `DeepgramTTS(TTSProvider)`
- WebSocket streaming TTS: open connection, send text continuously, receive audio stream
- `synthesize(text)` sends text over the WebSocket and yields audio chunks
- Support `stop()` / `cancel()` by sending close/flush message and discarding remaining audio
- Convert from Deepgram audio format to PCM16
- Config: model, encoding, sample_rate
- Unit tests with mocked WebSocket
- Integration test (gated behind `DEEPGRAM_API_KEY`)

### Task 3.4: ElevenLabs TTS provider
- Implement `ElevenLabsTTS(TTSProvider)`
- Support streaming TTS via chunked transfer encoding (HTTP)
- Also support WebSocket streaming option
- `synthesize(text)` returns async iterator of PCM16 audio chunks
- Support `stop()` / `cancel()` by aborting the stream
- Config: voice_id, model_id, stability, similarity_boost, output_format
- Unit tests with mocked HTTP/WebSocket responses
- Integration test (gated behind `ELEVENLABS_API_KEY`)

## Phase 3: Output Handling & Validation

### Task 3.5: Audio output format normalization
- Ensure all providers output PCM16 mono at a consistent sample rate
- Implement format conversion in the base class if provider returns MP3, opus, mulaw, etc.
- Use the audio utilities from WS1 (Task 1.4) for resampling

### Task 3.6: Mid-utterance cancellation tests
- For each provider, test the cancel path:
  - Start synthesis of a long text
  - Cancel partway through
  - Verify no more audio chunks are yielded after cancel
  - Verify the provider connection is cleaned up (no resource leaks)

### Task 3.7: Provider selection and factory
- Implement `create_tts_provider(config) -> TTSProvider` factory function
- Config specifies provider name + provider-specific settings
- Validate config at construction time
