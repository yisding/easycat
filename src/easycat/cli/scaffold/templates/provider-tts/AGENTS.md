# Agent Guide — $PROJECT_NAME

The provider lives in `custom_tts.py`; `agent.py` wires its registered
`tts="tone"` shortcut in `make_config()` and selects it in a local
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
uv run pytest -m "integration_live and provider_custom and surface_tts"
uv run ruff check agent.py custom_tts.py test_custom_tts.py
uv run ruff check --fix agent.py custom_tts.py test_custom_tts.py
uv run easycat docs
```

## Testing and evals

`tests/test_agent.py` runs offline: `register()` is confirmed to make the
custom TTS selectable, `agent.py`'s wiring is asserted as built behind
`pytest.importorskip("agents")`, and `ScriptedReasoning` stands in for the
model while EasyCat's real turn machinery (send_text, the audio pipeline,
journal, and latency metrics) runs end to end. A green run means the app is
wired and the pipeline is healthy; it says nothing about live model quality.

## Conventions

- Use `easycat.debug.testing.run_text_turn` / `run_text_turns` /
  `run_scripted_audio_turn` for deterministic turn regressions; reserve
  `assert_llm_judge` for an explicitly credentialed evaluation lane.
- Keep the offline `TTSProviderContractSuite` green while adapting the SDK.
- Emit provider-scoped `TTSEvent` values; EasyCat maps them to app events.
- Make `cancel()` prompt, keep secrets in `.env`, and exclude them from versions.
- Mark real backend tests `integration_live`, `provider_custom`, and
  `surface_tts`; gate them on their credential. Bare pytest excludes them.
