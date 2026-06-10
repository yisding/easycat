# $PROJECT_NAME

External EasyCat provider package — a Protocol-conforming `EnergyVAD`
provider with a config dataclass, a conformance test, and a live mic demo
that injects the provider through `EasyConfig`.

EasyCat providers are duck-typed: `custom_vad.py` implements the
`VADProvider` Protocol structurally, so this package needs no EasyCat
registry entry and no base class. The same recipe works for STT, TTS, and
transport providers — see the extending guides under `docs/extending/` in
the EasyCat repository.

## Install

```bash
uv sync
```

This installs `easycat[$EXTRAS]>=$EASYCAT_VERSION_FLOOR` from
`pyproject.toml`, including the extras the live demo needs.

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

Use `uv run easycat doctor --env-file .env --json` when a script or coding
agent needs parseable environment/check rows.

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
  fill in `pyproject.toml` metadata, and ship it; applications inject it
  with `EasyConfig.mic(vad=YourProvider())`.
- **Debug a session:** pass `debug="light", record_to="runs"` to
  `EasyConfig.mic(...)` in `agent.py`. EasyCat writes a SQLite journal under
  `.easycat/journals/` and a timestamped `RunBundle` under `runs/`; inspect
  the journal with `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- **Explore docs and routes:** run `uv run easycat docs` to find learning,
  maintenance, validation, and operations routes. Use
  `uv run easycat docs --audience app-builders` to narrow the map to
  app-building routes. Use
  `uv run easycat docs --audience app-builders --json` when automation needs
  that smaller route map, or `uv run easycat docs --json` when a script or
  coding agent needs the full route map with command hints and audience labels.
  If this is not the right starter, run `uv run easycat init --list-templates`; use
  `uv run easycat init --list-templates --json` when automation needs the
  template catalog. Replace uppercase or angle-bracket placeholders such as
  `PATH` or `<session_id>` before running those hints. Run
  `uv run easycat explain json-schema` for the JSON envelope and field
  contract.
