# Agent Guide — $PROJECT_NAME

The provider lives in `custom_stt.py`; `agent.py` selects its registered
`stt="scripted"` shortcut in a local microphone demo.

## Commands

```bash
uv sync
cp .env.example .env
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
uv run --env-file .env python agent.py
uv run pytest
uv run pytest -m "integration_live and provider_custom and surface_stt"
uv run ruff check agent.py custom_stt.py test_custom_stt.py
uv run ruff check --fix agent.py custom_stt.py test_custom_stt.py
uv run easycat docs
```

## Conventions

- Use `easycat.debug.testing.run_text_turn` for deterministic turn regressions;
  reserve `assert_llm_judge` for an explicitly credentialed evaluation lane.
- Keep the offline `STTProviderContractSuite` green while adapting the SDK.
- Emit provider-scoped `STTEvent` values; EasyCat maps them to app events.
- Keep secrets in `.env`, and never return them from `version_info()`.
- Mark real backend tests `integration_live`, `provider_custom`, and
  `surface_stt`; gate them on their credential. Bare pytest excludes them.
