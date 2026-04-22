# $PROJECT_NAME

Text-mode EasyCat agent — iterate on prompts without audio infrastructure.

## Install

```bash
uv sync
```

## Configure

Copy the example env file and fill in your API key:

```bash
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY`.

## Run

```bash
uv run --env-file .env python agent.py
```

Or export the keys and skip `--env-file`:

```bash
export $(grep -v '^#' .env | xargs)
uv run python agent.py
```

You'll get a `you:` prompt. Type something, hit Enter, and the agent responds.
Hit Enter on a blank line to exit.

## Next steps

- **Change the agent's personality:** edit `instructions=...` in `agent.py`.
- **Add tools:** see the OpenAI Agents SDK docs and pass `tools=[...]` to the
  `Agent(...)` constructor.
- **Swap to a voice agent:** replace `create_text_session` with
  `easycat.run(EasyCatConfig(agent=agent))` and add `stt=` / `tts=`. Or run
  `easycat init my-voice-agent --template openai-agents` for a voice starter.
- **Debug a session:** pass `debug="full"` to `create_text_session` to write a
  RunBundle journal under `~/.cache/easycat/journals/`. Inspect it via
  `RunBundle.load(...)` or load into a coding agent for analysis.
