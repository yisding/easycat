# $PROJECT_NAME

Inbound phone agent for Twilio Media Streams. The FastAPI app serves TwiML at
`/twiml` and starts a WebSocket listener for each call; every call gets its own
EasyCat session and the agent from `agent.py`. `agent.py` builds the agent in
`make_agent()`; importing it starts nothing, so the tests can import the
real app.

## Install

```bash
uv sync
```

This installs `easycat[$EXTRAS]>=$EASYCAT_VERSION_FLOOR` from
`pyproject.toml`, including the extras this generated project needs.

EasyCat is not on PyPI yet. If this project was scaffolded from a local
EasyCat checkout (the default for repo/editable installs, or via
`--easycat-source`), `pyproject.toml` also carries a `[tool.uv.sources]`
block so `uv sync` resolves `easycat` from that checkout. Delete the
block and re-run `uv sync` once you depend on the published package.
For a portable project that will move to CI or another developer, scaffold
with `--easycat-git URL --easycat-git-rev REV` instead. It writes a Git-backed
source with no generator-machine path. Git and `--easycat-source` are mutually
exclusive; keep credentials in a Git credential helper, never in the URL.

## Configure

```bash
cp .env.example .env
```

Edit `.env`, set `OPENAI_API_KEY`, set `TWILIO_AUTH_TOKEN` to your Twilio
auth token, and set `TWILIO_STREAM_URL` to the public `wss://...` URL Twilio
should connect to. Run doctor with that file loaded:

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
point `TWILIO_STREAM_URL` at the public `wss://` forwarding URL. The `/twiml`
webhook validates `X-Twilio-Signature`, generates a one-time stream token, and
the WebSocket listener rejects clients that do not present that token in the
Twilio `start.customParameters` payload. Keep the TwiML webhook URL configured
only in Twilio, and set `TRUST_PROXY_HEADERS=true` only behind a trusted proxy
that overwrites `X-Forwarded-*` headers.

## Run

```bash
uv run --env-file .env uvicorn server:create_app --factory --host 0.0.0.0 --port 8000
```

Set your Twilio voice webhook to `https://<public-host>/twiml`. Call the number
and ask to leave a message to see the `take_message` tool fire. Use
`TWILIO_MAX_SESSIONS` to cap concurrent provider-backed calls for your
deployment and `TWILIO_START_TIMEOUT_S` to bound unauthenticated sockets that
never send a Twilio start frame. `TWILIO_DRAIN_TIMEOUT_S` controls the graceful
shutdown window, and `TWILIO_FORCE_SHUTDOWN_TIMEOUT_S` bounds forced cleanup.
Set `TWILIO_PUBLIC_TWIML_URL` to the exact public webhook URL when a proxy
rewrites the request path, host, or port.

Ctrl-C to quit.

## Check

After editing the scaffold, run the local lint/syntax check:

```bash
uv run ruff check agent.py server.py tools.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py server.py tools.py` and then re-run the
check.

Then run the offline agent tests — no API keys, no network, no microphone
(`tests/test_agent.py` calls `tools.py` for real, asserts `agent.py`'s wiring,
and drives EasyCat's real text and audio pipelines with a scripted stand-in for
the model; see `AGENTS.md` for the testing-and-evals ladder):

```bash
uv run pytest
```

## Next steps

- **Change the call behavior:** edit the `INSTRUCTIONS` constant that
  `make_agent()` passes to the agent in `agent.py`.
- **Add more tools:** add a plain function to `tools.py`, then pass it through
  `function_tool(...)` in `make_agent()`, so the generated test can call it
  directly.
- **Swap STT providers:** add `stt="deepgram/flux"` to `EasyConfig(...)` in
  `server.py`, add `deepgram` to the `easycat[...]` dependency in
  `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY` in `.env`.
- **Add outbound calls or status callbacks:** copy status callbacks and outbound
  call helpers from `examples/twilio_app.py`.
- **Debug a session:** pass `debug="full", record_to=".easycat/runs"` to
  `EasyConfig(...)`. EasyCat writes a SQLite journal under `.easycat/journals/`
  and a timestamped `RunBundle` under `.easycat/runs/`; inspect the journal with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
  Debug bundles can contain raw transcripts, tool arguments, provider payloads,
  and artifacts; keep them in the gitignored `.easycat/` tree unless you
  redact them first.
- **Graduate to the Session API:** when you need event subscriptions, text
  turns, or replayable debug bundles beyond `run(...)`, follow the
  from-EasyConfig-to-Session guide:
  <https://github.com/yisding/easycat/blob/main/docs/from-easyconfig-to-session.md>.
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
  Coding agent? Use this generated project's `AGENTS.md` for local coding
  rules; use EasyCat's
  [llms.txt](https://github.com/yisding/easycat/blob/main/llms.txt) for
  machine-readable docs route discovery or run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
