# $PROJECT_NAME

Text-mode EasyCat agent — iterate on prompts without audio infrastructure.
`agent.py` builds the agent in `make_agent()`; importing it starts nothing,
so the tests can import the real app.

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

Copy the example env file and fill in your API key:

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

You'll get a `you:` prompt. Type something, hit Enter, and the agent responds.
Hit Enter on a blank line to exit.

## Check

After editing `agent.py`, run the local lint/syntax check:

```bash
uv run ruff check agent.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py` and then re-run the check.

Then run the offline agent tests — no API keys, no network, no microphone
(`tests/test_agent.py` asserts `agent.py`'s wiring and drives EasyCat's real
text and audio pipelines with a scripted stand-in for the model; see
`AGENTS.md` for the testing-and-evals ladder):

```bash
uv run pytest
```

## Next steps

- **Change the agent's personality:** edit the `INSTRUCTIONS` constant that
  `make_agent()` passes to the agent in `agent.py`.
- **Add tools:** see the OpenAI Agents SDK docs and pass `tools=[...]` to the
  `Agent(...)` constructor in `make_agent()`.
- **Swap to a voice agent:** replace `create_text_session` with
  `easycat.run(EasyConfig.mic(agent=make_agent()))` and add `stt=` / `tts=`.
  Or run `uv run easycat init my-voice-agent --template openai-agents` for a
  voice starter.
- **Debug a session:** pass `debug="full", record_to=".easycat/runs"` to
  `create_text_session(agent=make_agent(), ...)`. EasyCat writes a SQLite journal under
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
