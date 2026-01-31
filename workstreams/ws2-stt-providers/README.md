# WS2: Speech-to-Text Providers

**Feature:** #3 (STT — OpenAI + Deepgram + ElevenLabs)
**Depends on:** WS1 (STTProvider interface, audio format utilities)
**Parallel with:** WS3, WS4, WS5, WS6, WS7, WS8

## Goal

Implement three STT providers behind the unified `STTProvider` interface. Each provider can be developed and tested independently.

## Deliverables

### Unified STT Behavior

All providers must implement:

- `start_stream()` / `send_audio(chunk)` / `end_stream()` / `events() -> AsyncIterator[STTEvent]`
- Produce `STTEvent` objects (partial/final variants) via the `events()` async iterator — providers do **not** emit EasyCat-level events directly; the Session consumes `events()` and emits `stt.partial`/`stt.final`
- Normalize output: timestamps (optional), confidence (optional), language code (optional)

**Reconnect:** Providers with WebSocket connections (Deepgram, ElevenLabs) must use WS8's `ReconnectingWebSocket` wrapper rather than implementing bespoke reconnect logic.

### Provider: OpenAI STT

- Use Audio API transcriptions endpoint (models: `gpt-4o-transcribe`, etc.)
- Turn-based transcription: submit finalized user turns (VAD-driven segmentation)
- Handle API errors and retries

### Provider: Deepgram Streaming STT

- Real-time transcription over WebSocket (`listen-streaming`)
- Continuous partial + final events based on configuration
- WebSocket lifecycle management (connect, reconnect, close)

### Provider: ElevenLabs STT

- Batch transcription endpoint support
- Realtime speech-to-text WebSocket API
- Handle both modes behind the same interface

## Testing Strategy

Each provider can be tested independently with recorded audio samples:

- Unit tests with mocked API responses
- Integration tests against live APIs (gated behind env vars / credentials)
- Verify partial and final transcript events are emitted correctly

## Acceptance Criteria

- [ ] OpenAI STT: submits audio, returns final transcripts
- [ ] Deepgram STT: streams audio over WebSocket, emits partial + final transcripts
- [ ] ElevenLabs STT: supports both batch and realtime modes
- [ ] All providers normalize output to the common event format
- [ ] All providers handle connection errors gracefully
