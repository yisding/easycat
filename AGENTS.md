# Repository Guidelines

## Project Structure & Module Organization
- `src/easycat/`: core library code.
- Key subpackages: `stt/`, `tts/`, `agents/`, `transports/`, `telephony/`.
- Core orchestrators/utilities live in top-level modules such as `session.py`, `config.py`, `events.py`, `turn_manager.py`, `smart_turn.py`, `agent_runner.py`, `metrics.py`, `tracing.py`, and `timeouts.py`.
- Provider interfaces are centralized in `providers.py`; STT/TTS provider factory registries are in `stt/factory.py` and `tts/factory.py`.
- `src/easycat/models/`: runtime model assets (for example ONNX smart-turn model).
- `tests/`: pytest suite mirroring domains (for example `tests/stt/`, `tests/tts/`, `tests/session/`, `tests/turns/`, `tests/transports/`, `tests/websocket/`, `tests/agents/`).
- `examples/`: runnable reference apps (`local_chat.py`, `ws_server.py`, `ws_browser_example.py`, `webrtc_server.py`, `twilio_app.py`, `pydantic_ai_voice.py`) plus browser/deployment assets in `webrtc_static/` and `ec2_webrtc/`.

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
