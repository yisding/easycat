# Agent Guide — $PROJECT_NAME

The provider lives in `custom_tts.py`; `agent.py` selects its registered
`tts="tone"` shortcut in a local microphone demo.

## Commands

```bash
uv sync
cp .env.example .env
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
uv run --env-file .env python agent.py
uv run pytest
uv run pytest -m "integration_live and provider_custom and surface_tts"
uv run ruff check agent.py custom_tts.py test_custom_tts.py
uv run ruff check --fix agent.py custom_tts.py test_custom_tts.py
uv run easycat docs
```

## Conventions

- Use `easycat.debug.testing.run_text_turn` for deterministic turn regressions;
  reserve `assert_llm_judge` for an explicitly credentialed evaluation lane.
- Keep the offline `TTSProviderContractSuite` green while adapting the SDK.
- Emit provider-scoped `TTSEvent` values; EasyCat maps them to app events.
- Make `cancel()` prompt, keep secrets in `.env`, and exclude them from versions.
- Mark real backend tests `integration_live`, `provider_custom`, and
  `surface_tts`; gate them on their credential. Bare pytest excludes them.
