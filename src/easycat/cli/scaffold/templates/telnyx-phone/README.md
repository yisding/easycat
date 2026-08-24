# $PROJECT_NAME

Inbound phone agent for Telnyx Call Control. The FastAPI app answers calls at
`/telnyx` and starts a WebSocket listener for each call's media stream; every
call gets its own EasyCat session and the agent from `agent.py`.

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

Edit `.env`: set `OPENAI_API_KEY`, set `TELNYX_API_KEY` to a Call Control API
key, set `TELNYX_PUBLIC_KEY` to the Ed25519 public key shown on your Telnyx
messaging/voice webhook profile, and set `TELNYX_STREAM_URL` to the public
`wss://...` URL Telnyx should connect to. Run doctor with that file loaded:

```bash
uv run easycat doctor --env-file .env
```

Add `--json` (`uv run easycat doctor --env-file .env --json`) for parseable
environment/check rows.

`TELNYX_WS_PORT` defaults to `8766` and controls the local WebSocket listener.
Change it when another process owns that port, and keep `TELNYX_STREAM_URL`
pointing at the public tunnel for the same listener.

Security model: Telnyx signs every webhook delivery with Ed25519 over
`{timestamp}|{raw_body}` (headers `telnyx-signature-ed25519` +
`telnyx-timestamp`, five-minute replay window), so `/telnyx` rejects any
delivery whose signature does not verify against `TELNYX_PUBLIC_KEY`. The media
WebSocket handshake is NOT signed — there is no Twilio-signature equivalent —
so its only credential is the one-time stream token this app embeds in the
answer command's `stream_url`. The transport consumes that token during the
`start` frame and binds it to the call's `call_control_id`; replayed or
forged sockets are rejected before an EasyCat session is built. The built-in
token store is in-memory and fits a single app process; for multiple workers
or replicas, route webhook and WebSocket traffic to the same process or replace
the validator with shared storage. `TELNYX_STREAM_TOKEN_SECRET` optionally pins
the signing key for the local store.

For local testing, expose both listeners behind a tunnel such as ngrok and set
`TELNYX_STREAM_URL` to the public `wss://` forwarding URL of the media
listener. Point your Call Control application's webhook at the public
`https://.../telnyx` route.

## Run

```bash
uv run --env-file .env uvicorn server:create_app --factory --host 0.0.0.0 --port 8000
```

Call the number bound to your Call Control application and ask to leave a
message to see the `take_message` tool fire. Use `TELNYX_MAX_SESSIONS` to cap
concurrent provider-backed calls for your deployment and
`TELNYX_START_TIMEOUT_S` to bound unauthenticated sockets that never send a
Telnyx start frame. `TELNYX_DRAIN_TIMEOUT_S` controls the graceful shutdown
window, and `TELNYX_FORCE_SHUTDOWN_TIMEOUT_S` bounds forced cleanup.

Ctrl-C to quit.

## Check

After editing the scaffold, run the local lint/syntax check:

```bash
uv run ruff check agent.py server.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py server.py` and then re-run the check.

Then run the offline agent tests — no API keys or network needed
(`tests/test_agent.py` exercises the real turn pipeline with a stub
agent; see `AGENTS.md` for the testing-and-evals ladder):

```bash
uv run pytest
```

## Next steps

- **Change the call behavior:** edit `instructions=...` in `agent.py`.
- **Add more tools:** decorate functions with `@function_tool` and pass them in
  the `tools=[...]` list.
- **Swap STT providers:** add `stt="deepgram/flux"` to `EasyConfig.phone(...)`
  in `server.py`, add `deepgram` to the `easycat[...]` dependency in
  `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY` in `.env`.
- **Add outbound calls or status callbacks:** copy the outbound helpers from
  `examples/telnyx_app.py`.
- **Debug a session:** pass `debug="full", record_to=".easycat/runs"` to
  `EasyConfig.phone(...)`. EasyCat writes a SQLite journal under
  `.easycat/journals/` and a timestamped `RunBundle` under `.easycat/runs/`;
  inspect the journal with
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
