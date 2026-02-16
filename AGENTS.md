# Repository Guidelines

## Project Structure & Module Organization
- `src/easycat/`: core library code.
- Key subpackages: `stt/`, `tts/`, `agents/`, `transports/`, `telephony/`, plus shared modules like `session.py`, `events.py`, and `config.py`.
- `tests/`: pytest suite, generally mirroring source areas (for example `tests/stt/`, `tests/tts/`, `tests/session/`).
- `examples/`: runnable reference apps (`local_chat.py`, `ws_server.py`, `twilio_app.py`, `pydantic_ai_voice.py`).
- `workstreams/`: design plans and execution notes; not runtime code.

## Build, Test, and Development Commands
- `uv sync --group dev`: install project + dev tools.
- `uv run pytest`: run full test suite.
- `uv run pytest tests/tts/test_tts_openai.py`: run a focused test file.
- `uv run ruff check .`: lint (imports, style, correctness rules).
- `uv run ruff format .`: apply formatting.
- `uv run python examples/ws_server.py`: run a local example.

## Coding Style & Naming Conventions
- Python `>=3.11`; match existing typing-first style.
- Use 4-space indentation and keep lines within Ruff’s configured limit (`99`).
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Keep provider implementations focused (one provider per file) and prefer small, composable modules.
- Let Ruff manage import ordering and common style rules; run it before opening a PR.

## Testing Guidelines
- Framework: `pytest` with `pytest-asyncio` (`asyncio_mode = auto`).
- Test files use `test_*.py`; test functions use `test_*`.
- Put tests near related domain folders (audio, session, transports, providers).
- For live API tests, use `@pytest.mark.integration` and skip when credentials are missing.
- No fixed coverage gate is enforced; add or update tests for every behavior change.

## Commit & Pull Request Guidelines
- Recent history shows short, imperative subjects (for example: `add smart turn`, `fix test cases`). Keep that style, but be specific.
- Recommended format: `<scope>: <imperative summary>` (example: `stt: normalize partial transcript events`).
- PRs should include: problem statement, change summary, test evidence (`uv run pytest` / targeted runs), and linked issue/workstream when applicable.
- If behavior changes user-visible flows (examples/transports/telephony), include a brief usage note or sample output.

## Security & Configuration Tips
- Use environment variables for secrets (`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`); never commit keys.
- Keep optional provider dependencies in extras and document any new env vars in `README.md`.
