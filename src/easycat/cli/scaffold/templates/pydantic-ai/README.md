# $PROJECT_NAME

Voice agent built on PydanticAI. Listens on your local microphone, speaks
through your local speakers. Ships with one working tool (`current_time`, a
plain function in `tools.py`) so you can see tool use in action on the very
first run. `agent.py` builds the agent in `make_agent()` and wires it in
`make_config()`; importing it starts nothing, so the tests can import the
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

After editing `agent.py` or `tools.py`, run the local lint/syntax check:

```bash
uv run ruff check agent.py tools.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py tools.py` and then re-run the check.

Then run the offline agent tests — no API keys, no network, no microphone
(`tests/test_agent.py` calls `tools.py` for real, asserts `agent.py`'s wiring,
and drives EasyCat's real text and audio pipelines with a scripted stand-in for
the model; see `AGENTS.md` for the testing-and-evals ladder):

```bash
uv run pytest
```

## Next steps

- **Change the personality:** edit the `INSTRUCTIONS` constant that
  `make_agent()` passes as `system_prompt` in `agent.py`.
- **Add more tools:** add a plain function to `tools.py` and list it in the
  `tools=[...]` argument of `Agent(...)` inside `make_agent()`, so the
  generated test can call it directly. PydanticAI dispatches based on the
  request.
- **Swap the model:** change the `MODEL` constant (`"openai:gpt-4.1-mini"`,
  the default `make_agent()` argument) to another model string
  PydanticAI supports, then add the matching API key and provider extra if
  that provider is not part of the default PydanticAI install. For example:
  `uv add "pydantic-ai[groq]<2"` for stable v1. To move to the stable v2
  release instead, first switch `pydantic-ai` to `pydantic-ai-v2` inside the
  `easycat[...]` extras in `pyproject.toml` (the v1 extra pins
  `pydantic-ai<2`, so the two conflict), then
  `uv add "pydantic-ai[groq]>=2.24.0,<3.0.0"`.
- **Need multiple agents?** Scaffold the workflow template:
  `uv run easycat init my-workflow --template pydantic-ai-workflow`.
  It shows a two-specialist `on_user_turn(...)` workflow that EasyCat
  adapts directly.
- **Debug a session:** pass `debug="full", record_to=".easycat/runs"` to
  `EasyConfig.mic(agent=make_agent(), ...)`. EasyCat writes a SQLite journal under
  `.easycat/journals/` and a timestamped `RunBundle` under `.easycat/runs/`; inspect
  the journal with `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
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
