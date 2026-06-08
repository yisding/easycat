# $PROJECT_NAME

Voice workflow built on PydanticAI. It keeps two specialist agents in one
workflow object, routes each spoken turn to billing or technical support, and
lets EasyCat adapt the workflow through its `on_user_turn(...)` method.

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

You'll see `🎤 Listening…`. Try "I need a refund" and then "my browser audio
is broken" to hear the workflow switch specialists.

Ctrl-C to quit.

## Check

After editing `agent.py`, run the local lint/syntax check:

```bash
uv run ruff check agent.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py` and then re-run the check.

## Next steps

- **Change the routing:** edit the keyword checks in `on_user_turn(...)`.
- **Add a specialist:** add another `Agent(...)` to the `agents` mapping and a
  matching branch in the router.
- **Persist richer state:** add fields to `SupportWorkflow`; EasyCat will keep
  calling the same workflow object each turn.
- **Swap STT providers:** add `stt="deepgram/flux"` to the
  `EasyConfig.mic(...)` call, add `deepgram` to the `easycat[...]`
  dependency in `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY`
  in `.env`.
- **Debug a session:** pass `debug="full", record_to="runs"` to
  `EasyConfig.mic(...)`. EasyCat writes a SQLite journal under
  `.easycat/journals/` and a timestamped `RunBundle` under `runs/`; inspect
  the journal with `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience app-builders` to narrow the map to
  app-building routes, or `uv run easycat docs --json` when a script or coding
  agent needs the route map with command hints and audience labels. If this is
  not the right starter, run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase or angle-bracket placeholders such as
  `PATH` or `<session_id>` before running those hints. Run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
