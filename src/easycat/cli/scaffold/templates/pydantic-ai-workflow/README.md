# $PROJECT_NAME

Voice workflow built on PydanticAI. It keeps two specialist agents in one
workflow object, routes each spoken turn to billing or technical support, and
lets EasyCat adapt the workflow through its `on_user_turn(...)` method.

## Install

```bash
uv sync
```

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY`. Run `uv run easycat doctor` to verify:

```bash
uv run easycat doctor
```

## Run

```bash
uv run --env-file .env python agent.py
```

You'll see `🎤 Listening…`. Try "I need a refund" and then "my browser audio
is broken" to hear the workflow switch specialists.

Ctrl-C to quit.

## Check

After editing `agent.py`, run a quick syntax check:

```bash
uv run python -m py_compile agent.py
```

## Next steps

- **Change the routing:** edit the keyword checks in `on_user_turn(...)`.
- **Add a specialist:** add another `Agent(...)` to `self._agents` and a
  matching branch in the router.
- **Persist richer state:** store per-specialist domain fields alongside
  `_history`; EasyCat will keep calling the same workflow object each turn.
- **Swap STT providers:** add `stt="deepgram/flux"` to the
  `EasyConfig.mic(...)` call, add `deepgram` to the `easycat[...]`
  dependency in `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY`
  in `.env`.
- **Debug a session:** pass `debug="full"` to `EasyConfig.mic(...)`. EasyCat
  writes a SQLite journal under `.easycat/journals/`; inspect it with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
