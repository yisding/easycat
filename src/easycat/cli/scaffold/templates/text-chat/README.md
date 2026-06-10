# $PROJECT_NAME

Text-mode EasyCat agent — iterate on prompts without audio infrastructure.

## Install

```bash
uv sync
```

This installs `easycat[$EXTRAS]>=$EASYCAT_VERSION_FLOOR` from
`pyproject.toml`, including the extras this generated project needs.

## Configure

Copy the example env file and fill in your API key:

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

You'll get a `you:` prompt. Type something, hit Enter, and the agent responds.
Hit Enter on a blank line to exit.

## Check

After editing `agent.py`, run the local lint/syntax check:

```bash
uv run ruff check agent.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py` and then re-run the check.

Then run the offline agent tests — no API keys or network needed
(`tests/test_agent.py` exercises the real turn pipeline with a stub
agent; see `AGENTS.md` for the testing-and-evals ladder):

```bash
uv run pytest
```

## Next steps

- **Change the agent's personality:** edit `instructions=...` in `agent.py`.
- **Add tools:** see the OpenAI Agents SDK docs and pass `tools=[...]` to the
  `Agent(...)` constructor.
- **Swap to a voice agent:** replace `create_text_session` with
  `easycat.run(EasyConfig.mic(agent=agent))` and add `stt=` / `tts=`. Or run
  `uv run easycat init my-voice-agent --template openai-agents` for a voice
  starter.
- **Debug a session:** pass `debug="full", record_to="runs"` to
  `create_text_session`. EasyCat writes a SQLite journal under
  `.easycat/journals/` and a timestamped `RunBundle` under `runs/`; inspect
  the journal with `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience app-builders` to narrow the map to
  app-building routes. Use
  `uv run easycat docs --audience app-builders --json` when automation needs
  that smaller route map, or `uv run easycat docs --json` when a script or
  coding agent needs the full route map with command hints and audience labels.
  If this is not the right starter, run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase or angle-bracket placeholders such as
  `PATH` or `<session_id>` before running those hints. Run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
