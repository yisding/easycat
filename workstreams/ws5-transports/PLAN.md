# WS5: Transports — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (Transport interface, audio types).
> All three transports can be developed in parallel by different engineers.

## Phase 1: Local Transport (Developer Mode)

### Task 5.1: Local audio capture (microphone input) ✅
- Implement mic input using `sounddevice` or `pyaudio`
- Capture audio as PCM16 mono at 16kHz (configurable sample rate)
- Yield audio chunks as an async iterator matching the Transport interface
- Handle device selection (default device or by name/index)
- Handle platform differences (macOS, Linux, Windows)
- Test: capture 2 seconds of mic audio, verify format and chunk sizes

**Implementation:** `src/easycat/transports/local.py` — Uses `sounddevice` InputStream with a callback that converts float32 → PCM16 and enqueues chunks for the async iterator. Gracefully falls back to a no-op when `sounddevice` is not installed.

### Task 5.2: Local audio playback (speaker output) ✅
- Implement speaker output using `sounddevice` or `pyaudio`
- Accept PCM16 audio chunks and play them through the default output device
- Support `stop()` to halt playback immediately (for barge-in)
- Handle device selection
- Test: play a generated sine wave, verify audible output

**Implementation:** `src/easycat/transports/local.py` — Uses `sounddevice` OutputStream with a callback that reads from an asyncio queue, converts PCM16 → float32, and writes to the output buffer. Barge-in is supported by draining the queue on disconnect.

### Task 5.3: Local transport wrapper ✅
- Implement `LocalTransport(Transport)` combining mic capture + speaker playback
- `connect()` — open audio devices
- `disconnect()` — close audio devices, release resources
- `receive_audio()` — returns async iterator from mic
- `send_audio(chunk)` — queues audio for speaker playback
- End-to-end test: loopback (mic -> speaker) to verify the transport works

**Implementation:** `src/easycat/transports/local.py` — `LocalTransport` class with `LocalTransportConfig` for device selection and audio format. Tests in `tests/transports/test_transports.py::TestLocalTransport`.

## Phase 2: WebSocket Transport

### Task 5.4: WebSocket server setup ✅
- Implement `WebSocketTransport(Transport)` using `websockets` or `aiohttp`
- Host a WebSocket server on a configurable port
- Accept client connections (one session per connection)
- Define the wire protocol: binary frames for audio, text frames for control messages (JSON)

**Implementation:** `src/easycat/transports/websocket.py` — Uses `websockets.serve()` to host a server. Accepts one client at a time (rejects additional connections with code 4000). Sends `{"type": "ready"}` on connect.

### Task 5.5: WebSocket audio streaming ✅
- Receive audio chunks from client WebSocket messages -> `receive_audio()`
- Send audio chunks to client as WebSocket binary messages -> `send_audio(chunk)`
- Handle connection lifecycle: connect, disconnect, unexpected close
- Backpressure: if the client is slow to consume, handle gracefully

**Implementation:** `src/easycat/transports/websocket.py` — Inbound audio uses a bounded asyncio queue (configurable `max_pending_chunks`). Drops frames with a warning when queue is full. Client disconnect signals end of the `receive_audio` iterator.

### Task 5.6: WebSocket protocol definition ✅
- Document the frame protocol:
  - Binary frames: raw PCM16 audio
  - Text frames: JSON control messages (`{"type": "start"}`, `{"type": "stop"}`, `{"type": "config", ...}`)
- Support optional audio format negotiation (client specifies sample rate, encoding)
- Write a minimal test client (Python script) that connects, sends audio, receives audio

**Implementation:** Protocol documented in docstring. Format negotiation via `{"type": "config", "sample_rate": N}`. Test client exercised in `tests/transports/test_transports.py::TestWebSocketTransport`.

## Phase 3: Twilio Media Streams Transport

### Task 5.7: Twilio Media Streams WebSocket handler ✅
- Implement `TwilioTransport(Transport)` handling Twilio's bidirectional WebSocket protocol
- Parse Twilio message types: `connected`, `start`, `media`, `stop`, `mark`, **`dtmf`**
- Extract audio from `media` messages (base64-encoded mulaw)
- **Non-audio messages:** Emit DTMF digits and control events (call status, etc.) into the Session event bus so WS6 can consume them — this is the explicit handoff design between WS5 and WS6
- Send audio back in Twilio's expected format (base64-encoded mulaw `media` messages)

**Implementation:** `src/easycat/transports/twilio_media.py` — `TwilioTransport` handles all Twilio message types. DTMF digits are emitted directly into the provided `EventBus` as `DTMF` events (Option 1 from README). Also supports `send_mark()` and `clear_audio()` for playback control.

### Task 5.8: Twilio audio format conversion ✅
- Twilio sends/receives mulaw 8kHz mono audio
- Convert inbound: mulaw 8kHz -> PCM16 16kHz (using WS1 audio utilities)
- Convert outbound: PCM16 -> mulaw 8kHz
- Handle the `streamSid` for outbound message routing
- Unit tests: round-trip mulaw <-> PCM16 conversion

**Implementation:** `mulaw_to_pcm16()` and `pcm16_to_mulaw()` helper functions with a local mu-law codec and WS1's `resample()` for rate conversion. Round-trip tests in `tests/transports/test_transports.py::TestAudioConversion`.

### Task 5.9: TwiML session bootstrap ✅
- Provide helpers to generate TwiML for `<Stream>` / `<Connect><Stream>` setup
- Support inbound call routing to the WebSocket handler
- Document the Twilio webhook + WebSocket setup flow
- Test with a mock TwiML request

**Implementation:** `twiml_connect_stream()` for bidirectional `<Connect><Stream>` and `twiml_stream()` for one-way `<Start><Stream>`. Both accept URL, track, and optional parameters. Tests in `tests/transports/test_transports.py::TestTwiML`.

## Phase 4: Validation

### Task 5.10: Transport conformance tests ✅
- Write a shared test suite that any `Transport` implementation must pass:
  - Connect / disconnect lifecycle
  - Send and receive audio chunks
  - Audio format matches expected output
  - Graceful handling of disconnect during active streaming
- Run the suite against all three transports

**Implementation:** `tests/transports/test_transports.py::TestTransportConformance` — Verifies all three transports have the required protocol methods and pass `isinstance(t, Transport)` checks using `runtime_checkable` Protocol. Full integration tests per transport cover lifecycle, send/receive, and disconnect behavior. **31 tests total, all passing.**
