# $PROJECT_NAME

Voice agent built on the OpenAI Agents SDK. Listens on your local microphone,
speaks through your local speakers. Ships with one working tool (`current_time`)
so you can see tool use in action on the very first run.

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

After editing `agent.py`, run the local lint/syntax check:

```bash
uv run ruff check agent.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py` and then re-run the check.

## Next steps

- **Change the personality:** edit the `instructions=...` in `agent.py`.
- **Add more tools:** decorate any function with `@function_tool` and pass it
  in the `tools=[...]` list. The agent will pick the right tool based on the
  user's request.
- **Swap STT providers:** add `stt="deepgram/flux"` to the `EasyConfig.mic(...)`
  call, add `deepgram` to the `easycat[...]` dependency in `pyproject.toml`,
  run `uv sync`, and put `DEEPGRAM_API_KEY` in `.env`. Flux STT collapses
  VAD + STT + endpointing into one streaming connection for lower latency.
- **Try a different TTS voice:** pass `tts="openai"` with a specific voice via
  a typed `OpenAITTSConfig(voice="shimmer")`.
- **Debug a session:** pass `debug="full"` to `EasyConfig.mic(...)`. EasyCat
  writes a SQLite journal under `.easycat/journals/`; inspect it with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience app-builders` to narrow the map to
  app-building routes, or `uv run easycat docs --json` when a script or coding
  agent needs the route map with command hints and audience labels. If this is
  not the right starter, run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase placeholders such as `PATH` before
  running those hints. Run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
