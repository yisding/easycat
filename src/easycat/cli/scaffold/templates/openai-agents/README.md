# $PROJECT_NAME

Voice agent built on the OpenAI Agents SDK. Listens on your local microphone,
speaks through your local speakers. Ships with one working tool (`current_time`)
so you can see tool use in action on the very first run.

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

Edit `.env` and set `OPENAI_API_KEY`. Run doctor with that file loaded:

```bash
uv run easycat doctor --env-file .env
```

Add `--json` (`uv run easycat doctor --env-file .env --json`) for parseable
environment/check rows.

## Run

```bash
uv run --env-file .env python agent.py
```

You'll see `🎤 Listening…`. Speak, pause, and the agent will reply aloud. Ask
"what time is it?" to see the `current_time` tool fire.

Ctrl-C to quit.

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

- **Change the personality:** edit the `instructions=...` in `agent.py`.
- **Add more tools:** decorate any function with `@function_tool` and pass it
  in the `tools=[...]` list. The agent will pick the right tool based on the
  user's request.
- **Swap STT providers:** add `stt="deepgram/flux"` to `VoiceApp(...)`, add
  `deepgram` to the `easycat[...]` dependency in `pyproject.toml`,
  run `uv sync`, and put `DEEPGRAM_API_KEY` in `.env`. Flux STT collapses
  VAD + STT + endpointing into one streaming connection for lower latency.
- **Try a different TTS voice:** pass `tts="openai"` with a specific voice via
  a typed `OpenAITTSConfig(voice="shimmer")`.
- **Debug a session:** `debug="full"` enriches the SQLite journal under
  `.easycat/journals/`; it does not create a timestamped `RunBundle` by itself.
  To record bundles under `.easycat/runs/`, configure both `debug="full"` and
  `record_to=".easycat/runs"`:

  ```python
  VoiceApp(
      config=EasyConfig.mic(
          agent=agent,
          debug="full",
          record_to=".easycat/runs",
      )
  ).run("local")
  ```

  Inspect a journal with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
  Debug bundles can contain raw transcripts, tool arguments, provider payloads,
  and artifacts; keep them in the gitignored `.easycat/` tree unless you
  redact them first.
- **Graduate to EasyConfig and Session:** when you need event subscriptions,
  text turns, custom recording paths, or caller-owned lifecycle, follow the
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
