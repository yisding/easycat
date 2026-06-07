# $PROJECT_NAME

Voice agent built on PydanticAI. Listens on your local microphone, speaks
through your local speakers. Ships with one working tool (`current_time`) so
you can see tool use in action on the first run.

## Install

```bash
uv sync
```

This installs `easycat[$EXTRAS]>=$EASYCAT_VERSION_FLOOR` from
`pyproject.toml`, including the extras this generated project needs.

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY`. Run doctor with that file loaded:

```bash
uv run easycat doctor --env-file .env
```

Use `uv run easycat doctor --env-file .env --json` when a script or coding
agent needs parseable environment/check rows.

## Run

```bash
uv run --env-file .env python agent.py
```

You'll see `🎤 Listening…`. Speak, pause, and the agent will reply aloud. Ask
"what time is it?" to see the `current_time` tool fire.

Ctrl-C to quit.

## Check

After editing `agent.py`, run a quick syntax check:

```bash
uv run python -m py_compile agent.py
```

## Next steps

- **Change the personality:** edit `system_prompt=...` in `agent.py`.
- **Add more tools:** decorate with `@voice_agent.tool_plain` (or
  `@voice_agent.tool` if you need the run context) and PydanticAI will
  dispatch based on the request.
- **Swap the model:** change `"openai:gpt-4.1-mini"` to another model string
  PydanticAI supports, then add the matching API key and provider extra if
  that provider is not part of the default PydanticAI install. For example:
  `uv add "pydantic-ai[groq]<2"` for stable v1, or
  `uv add "pydantic-ai[groq]==2.0.0b3"` to opt into the v2 beta.
- **Need multiple agents?** Scaffold the workflow template:
  `uv run easycat init my-workflow --template pydantic-ai-workflow`.
  It shows a two-specialist `on_user_turn(...)` workflow that EasyCat
  adapts directly.
- **Debug a session:** pass `debug="full"` to `EasyConfig.mic(...)`. EasyCat
  writes a SQLite journal under `.easycat/journals/`; inspect it with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs`; use
  `uv run easycat docs --json` when a script or coding agent needs the route
  map with command hints and audience labels. If this is not the right starter,
  run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase placeholders such as `PATH` before
  running those hints. Run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
