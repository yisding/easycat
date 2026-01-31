# WS9: Configuration, Packaging & Examples

**Features:** Cross-cutting — config, optional dependencies, runnable examples
**Depends on:** WS1 (session model, provider interfaces), WS2–WS8 (provider implementations to wire up)
**Parallel with:** Can begin config/packaging design in parallel with WS1; examples require WS2–WS8 implementations.

## Goal

Provide the "golden path" install-and-run experience: a single configuration object, properly declared optional dependencies (extras), and minimal runnable example apps that demonstrate local, WebSocket, and telephony usage.

## Deliverables

### EasyCatConfig

A single top-level configuration object that ties together:

- Provider selection (STT, TTS, VAD, noise reduction)
- Per-provider sub-configs (API keys, model names, voice IDs, etc.)
- Audio format settings (sample rate, frame size)
- Turn-taking settings (silence timeout, pre-roll duration, push-to-talk mode)
- Timeout settings (STT, agent, TTS TTFB)
- Transport selection and settings
- Telephony settings (DTMF aggregator config, voicemail policy)
- Metrics/tracing settings

Design:

- `EasyCatConfig` as a top-level dataclass/Pydantic model
- Per-provider sub-configs: `OpenAISTTConfig`, `DeepgramSTTConfig`, `ElevenLabsSTTConfig`, `OpenAITTSConfig`, `DeepgramTTSConfig`, `ElevenLabsTTSConfig`, `KrispConfig`, `SileroConfig`, etc.
- Validation at construction time (fail fast on missing API keys, invalid combinations)
- Sensible defaults: Silero VAD, RNNoise, no telephony — so `EasyCatConfig()` with just an API key works for local development

### Optional Dependencies (Extras)

Define `pyproject.toml` extras so users install only what they need:

- `easycat[openai]` — OpenAI STT + TTS deps
- `easycat[deepgram]` — Deepgram SDK
- `easycat[elevenlabs]` — ElevenLabs SDK
- `easycat[krisp]` — Krisp SDK (commercial VAD + noise reduction)
- `easycat[telephony]` — Twilio SDK + WebSocket server deps
- `easycat[local]` — sounddevice / PyAudio for local mic/speaker
- `easycat[all]` — everything

Core package (no extras) includes: Silero VAD (torch or ONNX), RNNoise, base framework.

### Example Applications

Minimal, runnable examples that demonstrate the end-to-end experience:

#### `examples/local_chat.py`
- Local mic -> EasyCat session -> speaker output
- Uses OpenAI STT + TTS (or configurable)
- Demonstrates: basic voice loop, turn-taking, barge-in

#### `examples/ws_server.py`
- WebSocket server that accepts browser/client connections
- Bidirectional audio streaming
- Demonstrates: WebSocket transport, session per connection

#### `examples/twilio_app.py`
- Twilio Media Streams integration
- Inbound call handling with TwiML setup
- Demonstrates: telephony transport, DTMF, voicemail detection

Each example should:
- Be self-contained (single file, < 100 lines)
- Include a docstring explaining how to run it
- Handle graceful shutdown (Ctrl+C)

## Testing Strategy

- Config: unit tests for validation (missing keys, invalid combos, defaults)
- Extras: verify imports fail gracefully when optional deps are missing
- Examples: smoke tests that verify examples can be imported and configured (not full E2E — those are manual)

## Acceptance Criteria

- [ ] `EasyCatConfig` validates and wires up all provider/transport/pipeline settings
- [ ] Per-provider sub-configs exist for all supported providers
- [ ] `pyproject.toml` extras are defined and installable
- [ ] Importing a provider without its extra installed gives a clear error message
- [ ] `examples/local_chat.py` runs with mic/speaker on a developer machine
- [ ] `examples/ws_server.py` accepts WebSocket connections and streams audio
- [ ] `examples/twilio_app.py` handles Twilio Media Streams inbound calls
- [ ] All examples include setup instructions in docstrings
