# Repository Guidelines

## Project Structure & Module Organization
- `src/easycat/`: core library code.
- Key subpackages: `session/`, `stages/`, `stt/`, `tts/`, `transports/`, `telephony/`, `integrations/agents/`, `runtime/`, `debug/`, `debugger/`, `cli/`.
- Core orchestrators/utilities live alongside: `config.py`, `events.py`, `turn_manager.py`, `smart_turn.py`, `timeouts.py`.
- Provider interfaces are centralized in `providers.py`; STT/TTS factory registries live in `stt/factory.py` and `tts/factory.py`.
- Agent framework bridges live in `src/easycat/integrations/agents/` (`OpenAIAgentsBridge`, `PydanticAIBridge`, `GenericWorkflowBridge`, `RemoteResponsesAPIBridge`, plus `AgentRunner`).
- `src/easycat/models/`: runtime model assets (for example ONNX smart-turn model).
- `tests/`: pytest suite mirroring domains (`tests/stt/`, `tests/tts/`, `tests/session/`, `tests/stages/`, `tests/transports/`, `tests/websocket/`, `tests/integrations/agents/`, `tests/telephony/`, `tests/runtime/`, `tests/debug/`).
- `examples/`: runnable reference apps covering local microphone, WebSocket, WebRTC, Twilio, and Cartesia/Deepgram/ElevenLabs provider swaps.

## Build, Test, and Development Commands
- `uv sync --group dev`: install project + dev tools.
- `uv sync --extra <name>`: install optional provider/transport extras (for example `openai`, `openai-agents`, `webrtc`, `telephony`, `local`, `rnnoise`).
- `uv run pytest`: run full test suite.
- `uv run pytest tests/tts/test_tts_openai.py`: run a focused test file.
- `uv run pytest tests/transports/test_webrtc.py`: run focused WebRTC transport tests.
- `uv run ruff check .`: lint (imports, style, correctness rules).
- `uv run ruff format .`: apply formatting.
- `uv run python examples/ws_server.py`: run a local example.
- `uv run python examples/webrtc_server.py`: run the WebRTC example server.

## Coding Style & Naming Conventions
- Python `>=3.11`; match existing typing-first style.
- Prefer async-first code paths and typed protocols/interfaces for provider boundaries.
- Use 4-space indentation and keep lines within Ruff’s configured limit (`99`).
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Keep provider implementations focused (one provider per file) and prefer small, composable modules.
- When adding STT/TTS providers, update both config dataclasses and central factory registries.
- Let Ruff manage import ordering and common style rules; run it before opening a PR.

## Testing Guidelines
- Framework: `pytest` with `pytest-asyncio` (`asyncio_mode = auto`).
- Test files use `test_*.py`; test functions use `test_*`.
- Put tests near related domain folders (audio, session, turns, transports, providers, agents, websocket, telephony).
- For live API tests, use `@pytest.mark.integration` and skip when credentials are missing.
- No fixed coverage gate is enforced; add or update tests for every behavior change.

## Commit & Pull Request Guidelines
- Recent history shows short, imperative subjects (for example: `add smart turn`, `fix test cases`). Keep that style, but be specific.
- Recommended format: `<scope>: <imperative summary>` (example: `stt: normalize partial transcript events`).
- PRs should include: problem statement, change summary, and test evidence (`uv run pytest` / targeted runs).
- If behavior changes user-visible flows (examples/transports/telephony), include a brief usage note or sample output.

## Security & Configuration Tips
- Use environment variables for secrets (`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`); never commit keys.
- Example apps may also require deployment/runtime env vars such as `TWILIO_STREAM_URL`, `TURN_SERVER_URL`, `TURN_USERNAME`, and `TURN_CREDENTIAL`.
- Keep optional provider dependencies in extras and document any new env vars in `README.md`.

## Session Lifecycle Notes
- `await session.stop()` and `await session.shutdown()` both perform full live-backend teardown through `Session.destroy()`.
- `Session.close()` is only the logical clean-close marker for the journal; do not treat it as full session teardown.
- After a clean `stop()` or `shutdown()`, postmortem inspection is still valid: `session.journal.read()` and `session.export_debug_bundle(...)` should continue to work.
