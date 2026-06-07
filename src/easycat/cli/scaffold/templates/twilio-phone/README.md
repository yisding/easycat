# $PROJECT_NAME

Inbound phone agent for Twilio Media Streams. The FastAPI app serves TwiML at
`/twiml` and starts a WebSocket listener for each call; every call gets its own
EasyCat session and the agent from `agent.py`.

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

Edit `.env`, set `OPENAI_API_KEY`, and set `TWILIO_STREAM_URL` to the public
`wss://...` URL Twilio should connect to. Run doctor with that file loaded:

```bash
uv run easycat doctor --env-file .env
```

Use `uv run easycat doctor --env-file .env --json` when a script or coding
agent needs parseable environment/check rows.

`TWILIO_WS_PORT` defaults to `8766` and controls the local WebSocket listener.
Change it when another process owns that port, and keep `TWILIO_STREAM_URL`
pointing at the public tunnel for the same listener.

For local testing, expose the WebSocket port with a tunnel such as ngrok and
point `TWILIO_STREAM_URL` at the public `wss://` forwarding URL.

## Run

```bash
uv run --env-file .env uvicorn server:create_app --factory --host 0.0.0.0 --port 8000
```

Set your Twilio voice webhook to `https://<public-host>/twiml`. Call the number
and ask to leave a message to see the `take_message` tool fire.

Ctrl-C to quit.

## Check

After editing the scaffold, run a quick syntax check:

```bash
uv run python -m py_compile agent.py server.py
```

## Next steps

- **Change the call behavior:** edit `instructions=...` in `agent.py`.
- **Add more tools:** decorate functions with `@function_tool` and pass them in
  the `tools=[...]` list.
- **Swap STT providers:** add `stt="deepgram/flux"` to `EasyConfig(...)` in
  `server.py`, add `deepgram` to the `easycat[...]` dependency in
  `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY` in `.env`.
- **Harden production webhooks:** copy signature validation, status callbacks,
  and outbound call helpers from `examples/twilio_app.py`.
- **Debug a session:** pass `debug="full"` to `EasyConfig(...)`. EasyCat writes
  a SQLite journal under `.easycat/journals/`; inspect it with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience "app builders"` to narrow the map to
  app-building routes, or `uv run easycat docs --json` when a script or coding
  agent needs the route map with command hints and audience labels. If this is
  not the right starter, run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase placeholders such as `PATH` before
  running those hints. Run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
