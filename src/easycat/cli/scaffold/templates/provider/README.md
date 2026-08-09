# $PROJECT_NAME

External EasyCat provider package — a Protocol-conforming, name-registrable
`EnergyVAD` provider with a config dataclass, a conformance test, and a live
mic demo that selects the provider through `EasyConfig`.

EasyCat providers are duck-typed: `custom_vad.py` implements the
`VADProvider` Protocol structurally, with no base class. Its `register()`
function makes `vad="energy"` available to `EasyConfig`, `easycat.toml`,
and the provider planner. The `easycat.vad_providers` entry point in
`pyproject.toml` performs that registration automatically once this package
is installed.

## Install

```bash
uv sync
```

This installs `easycat[$EXTRAS]>=$EASYCAT_VERSION_FLOOR` from
`pyproject.toml`, including the extras the live demo needs.

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

Edit `.env` and set `OPENAI_API_KEY` (the demo session uses the default
OpenAI STT/TTS providers around your custom VAD). Run doctor with that file
loaded:

```bash
uv run easycat doctor --env-file .env
```

Add `--json` (`uv run easycat doctor --env-file .env --json`) for parseable
environment/check rows.

## Run

Speak into your microphone; the `EnergyVAD` gate decides when you are
talking:

```bash
uv run --env-file .env python agent.py
```

## Test

The conformance test pins the Protocol surface and the event grammar the
EasyCat session relies on:

```bash
uv run pytest test_custom_vad.py
```

Keep it green while you replace the RMS gate in `custom_vad.py` with your
real detector.

## Check

After editing the provider, run the local lint/syntax check:

```bash
uv run ruff check agent.py custom_vad.py test_custom_vad.py
```

If Ruff reports an auto-fixable issue, run
`uv run ruff check --fix agent.py custom_vad.py test_custom_vad.py` and then
re-run the check.

## Next steps

- **Swap in your real detector:** replace the RMS math in
  `EnergyVAD.process` and extend `EnergyVADConfig`; the conformance test
  keeps the surface honest.
- **Target a different stage:** the STT, TTS, and transport Protocols
  follow the same shape — duck-typed class plus config dataclass plus
  conformance test. The extending guides in the EasyCat repository walk
  through each surface.
- **Publish the package:** rename `custom_vad.py` to your package name,
  update the `easycat.vad_providers` entry point and `pyproject.toml`
  metadata, and ship it; applications select it with
  `EasyConfig.mic(vad="your-provider")` or inject a live instance directly.
- **Debug a session:** pass `debug="light", record_to=".easycat/runs"` to
  `EasyConfig.mic(...)` in `agent.py`. EasyCat writes a SQLite journal under
  `.easycat/journals/` and a timestamped `RunBundle` under `.easycat/runs/`; inspect
  the journal with `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
  Debug bundles can contain raw transcripts, tool arguments, provider payloads,
  and artifacts; keep them in the gitignored `.easycat/` tree unless you
  redact them first.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience provider-maintainers` to narrow the map to
  provider contracts and extension guides; add `--json`
  (`uv run easycat docs --audience provider-maintainers --json`,
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
