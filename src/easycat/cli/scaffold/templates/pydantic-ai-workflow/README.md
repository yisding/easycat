# $PROJECT_NAME

Voice workflow built on PydanticAI. It keeps two specialist agents in one
workflow object, routes each spoken turn to billing or technical support (the
keyword router lives in `tools.py`), and lets EasyCat adapt the workflow
through its `on_user_turn(...)` method. `agent.py` builds the workflow in
`make_workflow()` and wires it in `make_config()`; importing it starts
nothing, so the tests can import the real app.

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

You'll see `🎤 Listening…`. Try "I need a refund" and then "my browser audio
is broken" to hear the workflow switch specialists.

Ctrl-C to quit.

## Check

After editing `agent.py` or `tools.py`, run the local lint/syntax check:

```bash
uv run ruff check agent.py tools.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py tools.py` and then re-run the check.

Then run the offline agent tests — no API keys, no network, no microphone
(`tests/test_agent.py` calls `tools.py`'s router for real, asserts `agent.py`'s
wiring, and drives EasyCat's real text and audio pipelines with a scripted
stand-in for the specialists; see `AGENTS.md` for the testing-and-evals
ladder):

```bash
uv run pytest
```

## Next steps

- **Change the routing:** edit `pick_specialist(...)` in `tools.py`, so the
  generated test can call it directly.
- **Add a specialist:** add another `Agent(model, ...)` to the dict
  `make_specialists()` returns and a matching branch in `pick_specialist(...)`.
- **Swap the model:** change the `MODEL` constant (`"openai:gpt-4.1-mini"`, the
  default `make_specialists()` argument) to another model string PydanticAI
  supports, then add the matching API key and provider extra.
- **Persist richer state:** add fields to `SupportWorkflow`; EasyCat will keep
  calling the same workflow object each turn.
- **Swap STT providers:** add `stt="deepgram/flux"` to the
  `EasyConfig.mic(...)` call in `make_config()`, add `deepgram` to the
  `easycat[...]` dependency in `pyproject.toml`, run `uv sync`, and put
  `DEEPGRAM_API_KEY` in `.env`.
- **Debug a session:** pass `debug="full", record_to=".easycat/runs"` to
  `EasyConfig.mic(agent=make_workflow(), ...)`. EasyCat writes a SQLite journal under
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
