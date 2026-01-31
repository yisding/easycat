# WS9: Configuration, Packaging & Examples — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (interfaces), plus provider implementations from WS2–WS8 for examples.
> Config and packaging design can start in parallel with WS1. Examples require working providers.

## Phase 1: Configuration

### Task 9.1: EasyCatConfig top-level configuration
- Define `EasyCatConfig` as a dataclass (or Pydantic model if Pydantic is already a dependency)
- Fields:
  - `stt`: STT provider config (union of provider-specific configs)
  - `tts`: TTS provider config (union of provider-specific configs)
  - `vad`: VAD provider config (Krisp or Silero settings)
  - `noise_reduction`: Noise reducer config (Krisp or RNNoise settings)
  - `transport`: Transport config (local, WebSocket, or Twilio settings)
  - `turn_taking`: Turn-taking settings (silence timeout, pre-roll duration, mode)
  - `timeouts`: Timeout settings (STT, agent, TTS TTFB)
  - `telephony`: Telephony settings (DTMF aggregator config, voicemail policy)
  - `metrics`: Metrics/tracing settings (enabled, exporter backend)
- Sensible defaults: `EasyCatConfig()` with just an OpenAI API key should work for local development (Silero VAD, RNNoise, local transport)
- Validation at construction time: fail fast on missing required keys, invalid provider combinations
- Unit tests: valid config, missing API key, invalid provider name, defaults

### Task 9.2: Per-provider sub-configs
- Define sub-config dataclasses for each provider:
  - STT: `OpenAISTTConfig`, `DeepgramSTTConfig`, `ElevenLabsSTTConfig`
  - TTS: `OpenAITTSConfig`, `DeepgramTTSConfig`, `ElevenLabsTTSConfig`
  - VAD: `KrispVADConfig`, `SileroVADConfig`
  - Noise: `KrispNoiseConfig`, `RNNoiseConfig`
  - Transport: `LocalTransportConfig`, `WebSocketTransportConfig`, `TwilioTransportConfig`
- Each sub-config contains provider-specific settings (API keys, model names, voice IDs, sample rates, etc.)
- Integrate with WS2–WS8 factory functions (`create_stt_provider(config)`, etc.)
- Unit tests: validate each sub-config

### Task 9.3: Session factory from config
- Implement `create_session(config: EasyCatConfig) -> Session` that wires up all providers from config
- Uses factory functions from WS2 (STT), WS3 (TTS), WS4 (VAD, noise), WS5 (transport)
- Integrates WS7 (agent runner) and WS8 (reliability, metrics) based on config
- This is the main entry point for users — one call to get a fully configured session
- Unit tests with stub providers

## Phase 2: Packaging & Extras

### Task 9.4: Define pyproject.toml extras
- Add optional dependency groups to `pyproject.toml`:
  - `openai` — `openai` SDK
  - `deepgram` — `deepgram-sdk`
  - `elevenlabs` — `elevenlabs` SDK
  - `krisp` — Krisp SDK package
  - `telephony` — `twilio`, `websockets` (server)
  - `local` — `sounddevice` (or `pyaudio`)
  - `all` — all of the above
- Core dependencies (always installed): Silero VAD runtime (torch or onnxruntime), RNNoise bindings, base framework deps
- Verify: `uv add --optional openai openai` etc.

### Task 9.5: Graceful import errors for missing extras
- When a provider is configured but its optional dependency is not installed, raise a clear error:
  - e.g., `ImportError: easycat[deepgram] extra is required for Deepgram STT. Install with: uv add easycat[deepgram]`
- Implement lazy import pattern in each provider module
- Unit tests: mock missing imports, verify clear error messages

## Phase 3: Example Applications

### Task 9.6: Local chat example (`examples/local_chat.py`)
- Single-file example: local mic -> EasyCat session -> speaker output
- Demonstrates: voice loop, turn-taking, barge-in
- Uses OpenAI STT + TTS (configurable via env vars)
- Includes docstring with setup/run instructions
- Handles graceful shutdown (Ctrl+C / signal handling)
- Target: < 100 lines of user-facing code

### Task 9.7: WebSocket server example (`examples/ws_server.py`)
- Single-file example: WebSocket server accepting browser connections
- Demonstrates: WebSocket transport, session per connection, bidirectional audio
- Includes docstring with setup/run instructions
- Optional: include a minimal HTML client page or reference one

### Task 9.8: Twilio telephony example (`examples/twilio_app.py`)
- Single-file example: Twilio Media Streams inbound call handling
- Demonstrates: TwiML setup, telephony transport, DTMF, voicemail detection
- Includes docstring with Twilio setup instructions (webhook URL, ngrok for local dev)
- Uses FastAPI or a lightweight framework for the HTTP + WebSocket server

## Phase 4: Validation

### Task 9.9: Example smoke tests
- For each example, write a test that:
  - Imports the example module
  - Verifies the config is valid
  - Verifies the session can be created (with stub/mock providers)
- These are not full E2E tests — full testing is manual
- Verify examples run without syntax errors or import failures
