# WS5: Transports

**Feature:** #9 (Transports)
**Depends on:** WS1 (Transport interface, audio format utilities)
**Parallel with:** WS2, WS3, WS4, WS6, WS7, WS8

## Goal

Implement three transport layers that handle audio I/O for different deployment contexts. Each transport can be developed independently.

## Deliverables

### Local Transport (Developer Mode)

- Mic input capture (system microphone)
- Speaker output playback
- Fast iteration loop for local development and testing
- Handle audio device selection (optional)
- Platform-specific audio APIs (e.g., PyAudio, sounddevice)

### WebSocket Transport

- WebSocket server for browser/mobile client connections
- Bidirectional audio streaming (receive mic audio, send bot audio)
- Simple protocol matching telephony stream patterns for consistency
- Session management (connect, disconnect, reconnect)
- Frame protocol definition (audio chunks, control messages)

### Twilio Media Streams Transport

- Bidirectional WebSocket integration with Twilio Media Streams
- Receive inbound call audio from Twilio
- Send audio back to caller in real-time
- TwiML `<Stream>` / `<Connect><Stream>` compatible session bootstrap
- Handle Twilio-specific message formats (connected, start, media, stop)
- Audio format conversion (Twilio uses mulaw 8kHz -> internal PCM16)

## Testing Strategy

- Local transport: manual testing with microphone/speaker
- WebSocket transport: automated tests with a test WebSocket client
- Twilio transport: tests with mocked Twilio WebSocket messages, plus integration tests with Twilio sandbox
- All transports: verify audio flows bidirectionally and format conversion is correct

## Acceptance Criteria

- [ ] Local transport captures mic audio and plays back speaker audio
- [ ] WebSocket transport accepts connections and streams audio bidirectionally
- [ ] Twilio transport handles Media Streams protocol (connected/start/media/stop)
- [ ] Twilio transport correctly converts mulaw 8kHz <-> PCM16
- [ ] All transports conform to the Transport interface from WS1
- [ ] Session connect/disconnect lifecycle works for all transports
