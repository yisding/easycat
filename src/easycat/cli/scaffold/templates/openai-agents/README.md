# $PROJECT_NAME

Voice agent built on the OpenAI Agents SDK. Listens on your local microphone,
speaks through your local speakers. Ships with one working tool (`current_time`)
so you can see tool use in action on the very first run.

## Install

```bash
uv sync
```

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY`. Run `easycat doctor` to verify:

```bash
uvx easycat doctor
```

## Run

```bash
uv run --env-file .env python agent.py
```

You'll see `🎤 Listening…`. Speak, pause, and the agent will reply aloud. Ask
"what time is it?" to see the `current_time` tool fire.

Ctrl-C to quit.

## Next steps

- **Change the personality:** edit the `instructions=...` in `agent.py`.
- **Add more tools:** decorate any function with `@function_tool` and pass it
  in the `tools=[...]` list. The agent will pick the right tool based on the
  user's request.
- **Swap STT providers:** add `stt="deepgram/flux"` to the `EasyCatConfig(...)`
  call, put `DEEPGRAM_API_KEY` in `.env`. Flux STT collapses VAD + STT +
  endpointing into one streaming connection for lower latency.
- **Try a different TTS voice:** pass `tts="openai"` with a specific voice via
  a typed `OpenAITTSConfig(voice="shimmer")`.
- **Debug a session:** pass `debug="full"` to `EasyCatConfig(...)`. EasyCat
  writes a RunBundle journal you can export with
  `easycat bundles export --for=claude-code` and pipe into your coding agent.
