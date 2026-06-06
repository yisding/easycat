# $PROJECT_NAME

Browser voice agent built on the OpenAI Agents SDK. It serves EasyCat's
bundled WebRTC browser client, sends microphone audio from the page, and plays
the agent's voice response back through the browser.

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

Open `http://localhost:8080` in your browser and allow microphone access.
Ask "how do I connect?" to see the `connection_help` tool fire.

Ctrl-C to quit.

## Check

After editing `agent.py`, run a quick syntax check:

```bash
uv run python -m py_compile agent.py
```

## Next steps

- **Change the personality:** edit the `instructions=...` in `agent.py`.
- **Add more tools:** decorate any function with `@function_tool` and pass it
  in the `tools=[...]` list.
- **Swap STT providers:** add `stt="deepgram/flux"` to the
  `EasyConfig.browser(...)` call, add `deepgram` to the `easycat[...]`
  dependency in `pyproject.toml`, run `uv sync`, and put `DEEPGRAM_API_KEY`
  in `.env`.
- **Deploy beyond localhost:** start from `examples/webrtc_server.py` for
  TURN server and HTTPS reverse-proxy settings.
- **Debug a session:** pass `debug="full"` to `EasyConfig.browser(...)`.
  EasyCat writes a SQLite journal under `.easycat/journals/`; inspect it with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and examples:** run `uv run easycat docs`.
