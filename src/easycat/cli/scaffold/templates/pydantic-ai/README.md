# $PROJECT_NAME

Voice agent built on PydanticAI. Listens on your local microphone, speaks
through your local speakers. Ships with one working tool (`current_time`) so
you can see tool use in action on the first run.

## Install

```bash
uv sync
```

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY`. Run `easycat doctor` to verify:

```bash
uv run easycat doctor
```

## Run

```bash
uv run --env-file .env python agent.py
```

You'll see `🎤 Listening…`. Speak, pause, and the agent will reply aloud. Ask
"what time is it?" to see the `current_time` tool fire.

Ctrl-C to quit.

## Next steps

- **Change the personality:** edit `system_prompt=...` in `agent.py`.
- **Add more tools:** decorate with `@voice_agent.tool_plain` (or
  `@voice_agent.tool` if you need the run context) and PydanticAI will
  dispatch based on the request.
- **Swap the model:** change `"openai:gpt-4.1-mini"` to another model string
  PydanticAI supports, then add the matching API key and provider extra if
  that provider is not part of the default PydanticAI install. For example:
  `uv add "pydantic-ai[groq]==2.0.0b2"`.
- **Need multiple agents?** Extend `agent.py` with PydanticAI's graph or
  handoff patterns; this template keeps the first run intentionally small.
- **Debug a session:** pass `debug="full"` to `EasyConfig(...)`. EasyCat
  writes a RunBundle journal under `~/.cache/easycat/journals/` that you can
  inspect via `RunBundle.load(...)` or load into a coding agent for analysis.
