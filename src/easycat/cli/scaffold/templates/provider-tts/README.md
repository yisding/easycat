# $PROJECT_NAME

External EasyCat TTS package: typed config, structural provider, named
registration, lazy package entry point, capability metadata, and offline
behavioral contracts. `ToneTTS` is deterministic so the starter works without
provider traffic; replace its marked backend seam with your SDK.

## Install

```bash
uv sync
```

This installs `easycat[$EXTRAS]>=$EASYCAT_VERSION_FLOOR` from
`pyproject.toml`, including the local demo extras. A scaffold made from an
editable EasyCat checkout may also contain `[tool.uv.sources]`; remove it once
you depend on a published EasyCat release. EasyCat is not on PyPI yet, so this
local source block keeps `uv sync` resolvable during pre-release development.
For a portable project that will move to CI or another developer, scaffold
with `--easycat-git URL --easycat-git-rev REV` instead. It writes a Git-backed
source with no generator-machine path. Git and `--easycat-source` are mutually
exclusive; keep credentials in a Git credential helper, never in the URL.

## Configure

```bash
cp .env.example .env
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
```

Set `OPENAI_API_KEY` for the demo's agent and default STT. The custom TTS is
offline. When you add a live backend, add its key to `.env.example`, its
config dataclass, and `register_tts_provider(env_var=...)` together.

## Run

The local demo selects the public `tts="tone"` shortcut:

```bash
uv run --env-file .env python agent.py
```

## Test

```bash
uv run pytest test_custom_tts.py
```

The installable `TTSProviderContractSuite` checks string and typed input,
normalized audio events, idempotent stop/cancel, and all four
`version_info()` fields without network access. Bare `uv run pytest` excludes
`integration_live`, even when provider credentials are present. Run the live
lane explicitly after adding it:

```bash
uv run pytest -m "integration_live and provider_custom and surface_tts"
```

## Check

```bash
uv run ruff check agent.py custom_tts.py test_custom_tts.py
```

Apply safe fixes with
`uv run ruff check --fix agent.py custom_tts.py test_custom_tts.py`, then run
the primary check again.

## Next steps

- Replace the `LIVE TODO` seam with an async SDK client. Add
  `api_key: str | None`, `env_var`, `probe_module`, and `api_domains`; keep
  `capabilities` accurate and preserve prompt cancellation during barge-in.
- Add a separate suite marked `integration_live`, `provider_custom`, and
  `surface_tts`, with `live = True` and `credential_env_var`; never make the
  offline contract class use the network.
- Publish the `easycat.tts_providers` entry point from `pyproject.toml`; users
  can then select your registered name without importing your package first.
- Debug with `EasyConfig.mic(debug="light", record_to=".easycat/runs")`.
  Journals stay in `.easycat/journals/`; inspect one with
  `uv run easycat inspect .easycat/journals/<session_id>.sqlite`.
- Explore maintained routes with `uv run easycat docs`,
  `uv run easycat docs --audience provider-maintainers`,
  `uv run easycat docs --audience provider-maintainers --json`, and
  `uv run easycat docs --json`.
- Compare starters with `uv run easycat init --list-templates` or
  `uv run easycat init --list-templates --json`. Inspect machine envelopes
  with `uv run easycat explain json-schema`.
