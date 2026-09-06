# Agent Guide — $PROJECT_NAME

The provider lives in `custom_stt.py`; `agent.py` wires its registered
`stt="scripted"` shortcut in `make_config()` and selects it in a local
microphone demo. Keep `agent.py` import-safe: `make_agent()` /
`make_config()` build things, and only the `if __name__ == "__main__":`
guard runs one.

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

## Testing and evals

`tests/test_agent.py` runs offline: `register()` is confirmed to make the
custom STT selectable, `agent.py`'s wiring is asserted as built behind
`pytest.importorskip("agents")`, and `ScriptedReasoning` stands in for the
model while EasyCat's real turn machinery (send_text, the audio pipeline,
journal, and latency metrics) runs end to end. A green run means the app is
wired and the pipeline is healthy; it says nothing about live model quality.

## Conventions

- Use `easycat.debug.testing.run_text_turn` / `run_text_turns` /
  `run_scripted_audio_turn` for deterministic turn regressions; reserve
  `assert_llm_judge` for an explicitly credentialed evaluation lane.
- Keep the offline `STTProviderContractSuite` green while adapting the SDK.
- Emit provider-scoped `STTEvent` values; EasyCat maps them to app events.
- Keep secrets in `.env`, and never return them from `version_info()`.
- Mark real backend tests `integration_live`, `provider_custom`, and
  `surface_stt`; gate them on their credential. Bare pytest excludes them.
