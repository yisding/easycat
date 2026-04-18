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
uvx easycat doctor
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
- **Swap the model:** change `"openai:gpt-4.1-mini"` to any model string
  PydanticAI supports (`"anthropic:claude-sonnet-4-5"`,
  `"groq:llama-3.3-70b"`, etc.). Add the matching API key to `.env`.
- **Need multiple agents?** Check the `pydantic-ai-workflow` template for a
  starter with specialist handoffs.
- **Debug a session:** pass `debug="full"` to `EasyCatConfig(...)`. EasyCat
  writes a RunBundle journal you can export with
  `easycat bundles export --for=claude-code`.
