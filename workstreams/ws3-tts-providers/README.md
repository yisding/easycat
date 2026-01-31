# WS3: Text-to-Speech Providers

**Feature:** #4 (TTS — OpenAI + Deepgram + ElevenLabs)
**Depends on:** WS1 (TTSProvider interface, audio format utilities)
**Parallel with:** WS2, WS4, WS5, WS6, WS7, WS8

## Goal

Implement three TTS providers behind the unified `TTSProvider` interface. Each provider can be developed and tested independently.

## Deliverables

### Unified TTS Behavior

All providers must support:

- Input: text chunks or full text
- Output: streaming audio frames/chunks + optional alignment/markers
- `stop()` / `cancel()` mid-utterance (for barge-in support)
- Output format: PCM16 preferred internally. **Request PCM/linear16 directly from each provider** when supported to avoid introducing ffmpeg/system dependencies for MP3/Opus decoding. Only decode compressed formats when no PCM option exists.

**Reconnect:** Providers with WebSocket connections (Deepgram, ElevenLabs) must use WS8's `ReconnectingWebSocket` wrapper rather than implementing bespoke reconnect logic.

### Provider: OpenAI TTS

- Audio API (`audio/speech`) with streaming output support
- Handle voice selection, speed parameters
- Convert output to internal PCM16 format

### Provider: Deepgram TTS (Aura)

- WebSocket streaming TTS (continuous text stream -> audio stream)
- Handle WebSocket lifecycle
- Stream audio chunks as they arrive

### Provider: ElevenLabs TTS

- Streaming TTS via chunked transfer encoding
- WebSocket option for streaming
- Support voice selection and model parameters

## Testing Strategy

Each provider can be tested independently:

- Unit tests with mocked API responses verifying audio chunk output
- Integration tests against live APIs (gated behind env vars)
- Verify mid-utterance cancellation works correctly
- Verify output format is correct PCM16

## Acceptance Criteria

- [ ] OpenAI TTS: generates streaming audio from text
- [ ] Deepgram TTS: streams audio over WebSocket from text input
- [ ] ElevenLabs TTS: supports both chunked and WebSocket streaming
- [ ] All providers output PCM16 audio (converting if necessary)
- [ ] All providers support `stop()` / `cancel()` for barge-in
- [ ] All providers handle connection errors gracefully
