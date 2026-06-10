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

Add `--json` (`uv run easycat doctor --env-file .env --json`) for parseable
environment/check rows.

`TWILIO_WS_PORT` defaults to `8766` and controls the local WebSocket listener.
Change it when another process owns that port, and keep `TWILIO_STREAM_URL`
pointing at the public tunnel for the same listener.

The generated `/twiml` route adds a signed, one-time stream token and the
WebSocket transport consumes it during Twilio's `start` event. The built-in
token store is in-memory and fits a single app process; for multiple workers or
replicas, route TwiML and WebSocket traffic to the same process or replace the
validator with shared storage. `TWILIO_STREAM_TOKEN_SECRET` optionally pins the
signing secret for the local store.

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

After editing the scaffold, run the local lint/syntax check:

```bash
uv run ruff check agent.py server.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py server.py` and then re-run the check.

## Next steps

- **Change the call behavior:** edit `instructions=...` in `agent.py`.
- **Add more tools:** decorate functions with `@function_tool` and pass them in
  the `tools=[...]` list.
- **Swap STT providers:** add `stt="deepgram/flux"` to `EasyConfig(...)` in
  `server.py`, add `deepgram` to the `easycat[...]` dependency in
  `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY` in `.env`.
- **Harden production webhooks:** copy signature validation, status callbacks,
  and outbound call helpers from `examples/twilio_app.py`.
- **Debug a session:** pass `debug="full", record_to="runs"` to
  `EasyConfig(...)`. EasyCat writes a SQLite journal under `.easycat/journals/`
  and a timestamped `RunBundle` under `runs/`; inspect the journal with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience app-builders` to narrow the map to
  app-building routes; add `--json`
  (`uv run easycat docs --audience app-builders --json`,
  `uv run easycat docs --json`) when automation needs the route map with
  command hints and audience labels.
  If this is not the right starter, run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase or angle-bracket placeholders such as
  `PATH` or `<session_id>` before running those hints.
  Coding agent? Start at EasyCat's
  [llms.txt](https://github.com/yisding/easycat/blob/main/llms.txt) or run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
