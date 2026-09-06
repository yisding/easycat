# Error Code Reference

Every EasyCat failure with a stable identity carries an `EASYCAT_Exxx` code.
This page is **generated** from the registry in
[`src/easycat/errors.py`](../../src/easycat/errors.py) by
`scripts/regen_error_codes.py` — the same registry `easycat explain` reads, so
the two can never disagree. Do not edit it by hand; edit the `register(...)`
call and re-run the script.

For one code in the terminal, with your own context substituted into the
headline:

```bash
uv run easycat explain EASYCAT_E304
uv run easycat explain --list
```

Maintainers: after editing a `register(...)` call, refresh this page and verify
it is current with

```bash
uv run python scripts/regen_error_codes.py
uv run python scripts/regen_error_codes.py --check
```

Several fixes below tell you to verify credentials with
`uv run easycat doctor --env-file .env`; add `--json`
(`uv run easycat doctor --env-file .env --json`) when a script needs to read
the result instead of a person.

Codes are namespaced by range, and every entry below lists what the code
means, what causes it, and how to fix it. Headlines are `str.format` templates:
the raising code substitutes its own context, which `easycat explain` also does
for the fix text.

## E1xx — Scaffolding

`easycat init`, templates, and config JSON.

### EASYCAT_E101

**Target {target!r} already exists and would be clobbered by scaffolding.**

- **Cause:** `easycat init` refuses to write into an existing non-empty directory, regular file, or symlink to avoid clobbering work in progress.
- **Fix:** Choose a new name, or remove the target first (`rm -rf <target>`). For non-empty directories only, `--force` will write into the existing directory without removing its files.
- **Example:** `easycat init my-agent --force`
- **Related:** [EASYCAT_E102](#easycat_e102)

### EASYCAT_E102

**Invalid --config JSON: {problem}**

- **Cause:** The --config payload is not valid JSON, is missing `schema_version`, or contains an unknown key. The init schema rejects unknown keys on purpose so coding agents (Claude Code, Cursor, Codex) get loud feedback on typos.
- **Fix:** Run `easycat explain init-schema` for the full schema. If the problem is an unknown key, check for typos — a fuzzy suggestion is usually printed alongside this error.
- **Example:** `easycat init demo --config '{"schema_version": 1, "template": "openai-agents"}'`
- **Related:** [EASYCAT_E101](#easycat_e101)

### EASYCAT_E103

**Unknown template {template!r}. Available: {available}**

- **Cause:** The requested template is not in the shipped template catalog.
- **Fix:** Run `easycat init --list-templates` to see the full list. Check spelling — the CLI accepts hyphenated names only (e.g., `openai-agents`, not `openai_agents`).
- **Example:** `easycat init demo --template openai-agents`
- **Related:** [EASYCAT_E102](#easycat_e102)

### EASYCAT_E104

**Unknown provider {provider!r}. Available: {available}.{hint}**

- **Cause:** The requested provider is not registered in the STT/TTS factory. Either the name is misspelled or the provider requires an optional extra that is not installed.
- **Fix:** Check spelling — provider names are lowercased with hyphens (`deepgram`, `openai-realtime`). Install the provider extra if needed: `uv add 'easycat[deepgram]'`. From the EasyCat repo, use `uv sync --extra deepgram --group dev`.
- **Example:** `stt="deepgram/flux"`
- **Related:** [EASYCAT_E203](#easycat_e203)

### EASYCAT_E105

**Invalid application configuration: {problem}**

- **Cause:** An EasyConfig, TextSessionConfig, or low-level SessionConfig value is missing a mode-required collaborator or contains an unsupported policy value.
- **Fix:** Use EasyConfig plus create_session/run for descriptor-based setup, or provide all live collaborators when constructing SessionConfig directly.
- **Example:** `run(EasyConfig.mic(agent=my_agent))`
- **Related:** [EASYCAT_E104](#easycat_e104), [EASYCAT_E203](#easycat_e203)

## E2xx — Environment

`easycat doctor` checks: credentials, extras, reachability.

### EASYCAT_E201

**Python {found} detected — EasyCat requires Python >= 3.11.**

- **Cause:** EasyCat uses typing features and asyncio semantics that only landed in Python 3.11 (PEP 654 ExceptionGroup, PEP 678 exception notes, TaskGroup).
- **Fix:** Install Python 3.11 or newer. With uv: `uv python install 3.12`. From the EasyCat repo, use `uv sync --python 3.12 --group dev`.
- **Example:** `uv python install 3.12  # repo: uv sync --python 3.12 --group dev`

### EASYCAT_E202

**Missing required extra: {extra}**

- **Cause:** The agent or template needs a Python package that is in one of EasyCat's optional extras, but that extra is not installed.
- **Fix:** Install the extra: `uv add 'easycat[{extra}]'`. From the EasyCat repo, use `uv sync --extra {extra} --group dev`.
- **Example:** `uv add 'easycat[openai-agents]'  # or: uv sync --extra openai-agents --group dev`
- **Related:** [EASYCAT_E203](#easycat_e203)

### EASYCAT_E203

**Missing API key: {var}**

- **Cause:** The provider you selected needs an API key in an environment variable, but the variable is unset, empty, or still contains an obvious example placeholder.
- **Fix:** Set the env var: `export {var}=...`. If the project uses a `.env` file, copy `.env.example` to `.env`, fill in keys there, and verify it with `easycat doctor --env-file .env`.
- **Example:** `export OPENAI_API_KEY=sk-...  # or: easycat doctor --env-file .env`
- **Related:** [EASYCAT_E202](#easycat_e202)

### EASYCAT_E204

**Provider {provider!r} unreachable: {detail}**

- **Cause:** `easycat doctor` sent an unauthenticated HEAD probe to the provider's API endpoint and received no response. This only checks network/DNS liveness; it does not validate the configured credential.
- **Fix:** Check internet connectivity and DNS, then re-run. If the host still cannot be reached, check the provider's status page. Validate credentials with an explicitly selected live/provider workflow.
- **Example:** `easycat doctor --provider openai`
- **Related:** [EASYCAT_E203](#easycat_e203)

### EASYCAT_E205

**onnxruntime is not importable (smart-turn extra requested).**

- **Cause:** Smart Turn endpoint detection needs `onnxruntime`, which ships in the `smart-turn` extra but is not currently installed in this environment.
- **Fix:** Install Smart Turn support: `uv add 'easycat[smart-turn]'`. From the EasyCat repo, use `uv sync --extra smart-turn --group dev`.
- **Example:** `uv add 'easycat[smart-turn]'  # or: uv sync --extra smart-turn --group dev`
- **Related:** [EASYCAT_E202](#easycat_e202)

### EASYCAT_E206

**No default microphone device detected.**

- **Cause:** `easycat doctor` queried `sounddevice` for the default input device and none was present. On macOS this usually means the terminal application has not been granted microphone access.
- **Fix:** On macOS: System Settings → Privacy & Security → Microphone, grant access to your terminal. On Linux: check PulseAudio or PipeWire is running. On Windows: check Sound settings.

### EASYCAT_E207

**Journal directory is not writable: {path}**

- **Cause:** EasyCat writes crash-durable session journals to `.easycat/journals/` by default. That directory is either missing, read-only, or on a filesystem that does not support SQLite WAL mode. Set `EASYCAT_DATA_DIR` to move the journal root.
- **Fix:** mkdir -p .easycat/journals && chmod u+w .easycat/journals

### EASYCAT_E208

**Low disk space at {path}: {free_mb}MB free (need >= 500MB).**

- **Cause:** Journals and bundles can grow to tens of megabytes per session; a machine running low on disk will silently fail to persist recordings.
- **Fix:** Free up disk space or point EasyCat at a larger filesystem with EASYCAT_DATA_DIR.
- **Related:** [EASYCAT_E207](#easycat_e207)

### EASYCAT_E209

**PortAudio runtime library is unavailable.**

- **Cause:** The `sounddevice` Python package is installed, but it could not load the native PortAudio library required by local microphone and speaker I/O.
- **Fix:** Install PortAudio first (Debian/Ubuntu: `sudo apt-get install libportaudio2`; macOS: `brew install portaudio`), then retry.
- **Example:** `sudo apt-get install libportaudio2  # macOS: brew install portaudio`
- **Related:** [EASYCAT_E202](#easycat_e202), [EASYCAT_E206](#easycat_e206)

### EASYCAT_E210

**Required project environment variable is missing or invalid: {var}**

- **Cause:** The selected EasyCat project (its `[tool.easycat.scaffold]` table or its `easycat.toml` profile) declares this environment variable as a startup requirement, but doctor found it unset, still set to an example placeholder, or invalid for its declared use.
- **Fix:** Copy `.env.example` to `.env`, replace every required placeholder with the real project value, and rerun `easycat doctor --env-file .env` (or `easycat doctor --manifest easycat.toml --env-file .env`).
- **Example:** `easycat doctor --env-file .env`
- **Related:** [EASYCAT_E203](#easycat_e203)

## E3xx — Runtime

Session execution: providers, transports, turns.

### EASYCAT_E301

**STT provider {provider!r} timed out after {timeout:.1f}s.**

- **Cause:** The speech-to-text provider did not produce a transcript within the configured `stt_timeout`. The provider may be slow, unreachable, or the audio stream may have stalled.
- **Fix:** Increase `TimeoutConfig.stt_timeout` if the provider is simply slow, or check network connectivity to the STT provider. Inspect the session journal Error record (tagged with this code) for the failing turn.
- **Example:** `TimeoutConfig(stt_timeout=20.0)`
- **Related:** [EASYCAT_E204](#easycat_e204)

### EASYCAT_E302

**Agent timed out after {timeout:.1f}s.**

- **Cause:** The agent did not return a response within the configured `agent_timeout`. A tool call, model call, or downstream service is likely hanging.
- **Fix:** Increase `TimeoutConfig.agent_timeout` for long-running agents, or add per-tool timeouts inside the agent. Inspect the session journal Error record (tagged with this code) for the turn that stalled.
- **Example:** `TimeoutConfig(agent_timeout=60.0)`
- **Related:** [EASYCAT_E301](#easycat_e301), [EASYCAT_E303](#easycat_e303)

### EASYCAT_E303

**TTS provider {provider!r} timed out after {timeout:.1f}s.**

- **Cause:** The text-to-speech provider did not produce its first audio frame within the configured `tts_first_byte_timeout`. The provider may be slow, unreachable, or rejected the request.
- **Fix:** Increase `TimeoutConfig.tts_first_byte_timeout` if the provider is slow to start, or check network connectivity to the TTS provider. Inspect the session journal Error record (tagged with this code).
- **Example:** `TimeoutConfig(tts_first_byte_timeout=8.0)`
- **Related:** [EASYCAT_E302](#easycat_e302)

### EASYCAT_E304

**Provider {provider!r} became unreachable mid-call: {detail}**

- **Cause:** A live provider connection dropped during an active session (network blip, server-side disconnect, or the provider closed the stream). Unlike `easycat doctor` probes, this happens while audio is flowing.
- **Fix:** EasyCat will attempt to reconnect automatically; persistent failures surface as EASYCAT_E305. Check network stability and the provider's status page. The session journal Error record carries this code for correlation.
- **Related:** [EASYCAT_E204](#easycat_e204), [EASYCAT_E305](#easycat_e305)

### EASYCAT_E305

**Provider {provider!r} exhausted {reason} after {attempts} attempt(s).**

- **Cause:** EasyCat either exhausted the failed-attempt retry limit or the successful reconnect-cycle budget for a dropped provider connection. The session can no longer reach the provider.
- **Fix:** Check sustained network connectivity and the provider's status page, then restart the session. Raise the reconnect attempt limit only if the outage is expected to be transient.
- **Related:** [EASYCAT_E304](#easycat_e304)

## E4xx — Bundle and replay

Debug bundles, journals, and replay inputs.

### EASYCAT_E401

**Failed to write debug bundle to {path}: {detail}**

- **Cause:** Serializing the session run bundle to disk failed — usually a read-only path, a full disk, or a permissions problem.
- **Fix:** Verify the target directory is writable and has free space (see EASYCAT_E207 / EASYCAT_E208), then re-export the bundle.
- **Example:** `session.export_debug_bundle('/tmp/run.zip')`
- **Related:** [EASYCAT_E207](#easycat_e207), [EASYCAT_E208](#easycat_e208), [EASYCAT_E402](#easycat_e402)

### EASYCAT_E402

**Failed to load debug bundle from {path}: {detail}**

- **Cause:** The bundle could not be read or parsed — the file is missing, truncated, not a valid EasyCat bundle, or was produced by an incompatible schema version.
- **Fix:** Confirm the path points at a complete bundle produced by a compatible EasyCat version. Re-export from the source session if the file is corrupt.
- **Example:** `load_bundle('/tmp/run.zip')`
- **Related:** [EASYCAT_E401](#easycat_e401), [EASYCAT_E403](#easycat_e403)

### EASYCAT_E403

**Replay diverged from recorded bundle: {detail}**

- **Cause:** Replaying a recorded bundle produced output that no longer matches the recording — pipeline behavior changed, or the bundle was recorded with a different configuration.
- **Fix:** Inspect the divergence detail and the bundle's recorded config. If the change is intentional, re-record the bundle; otherwise treat the divergence as a regression.
- **Related:** [EASYCAT_E402](#easycat_e402)

### EASYCAT_E404

**Not an EasyCat journal: {path}**

- **Cause:** The file is not a SQLite database, or it is a SQLite database with no `journal` table. `easycat tail` retries a missing table briefly — a live session creates it moments after the file appears — then reports the target as unusable rather than waiting forever.
- **Fix:** Point at a `.sqlite` journal written by a session with `debug="light"` or `debug="full"` (by default under `.easycat/journals/`). Use `easycat bundles show <path>` for an exported ZIP bundle.
- **Example:** `easycat tail .easycat/journals/session-abc123.sqlite`
- **Related:** [EASYCAT_E207](#easycat_e207), [EASYCAT_E402](#easycat_e402)

## E5xx — CLI usage

Command invocation and argument problems.

### EASYCAT_E501

**Unknown error code {code!r}.**

- **Cause:** `easycat explain` could not find this code in the registry.
- **Fix:** Run `easycat explain --list` to see every registered code. Common codes: E101 (init target exists), E203 (missing API key), E204 (provider unreachable).
- **Example:** `easycat explain --list`

## E6xx — Project manifest

`easycat.toml` loading and validation.

### EASYCAT_E601

**Manifest file not found: {path}**

- **Cause:** `easycat serve --manifest` (or `VoiceServer.from_manifest`) could not find an `easycat.toml`. The loader looks at `--manifest`, then `EASYCAT_MANIFEST`, then `easycat.toml` in the working directory.
- **Fix:** Create an `easycat.toml`, pass `--manifest path/to/easycat.toml`, or set `EASYCAT_MANIFEST`.
- **Example:** `easycat serve --manifest easycat.toml`
- **Related:** [EASYCAT_E602](#easycat_e602)

### EASYCAT_E602

**Invalid manifest {path}: {problem}**

- **Cause:** The manifest is not valid TOML, is missing a required table/field, uses an unknown profile, or names an unknown transport. The schema rejects unknown keys so typos fail loudly.
- **Fix:** Check the manifest against `docs/deployment/production-servers.md`. Each `[voice.<profile>]` needs a known `transport` (`webrtc`/`websocket`/`twilio`/`telnyx`/`local`).
- **Example:** `[voice.default]
transport = "webrtc"`
- **Related:** [EASYCAT_E601](#easycat_e601), [EASYCAT_E603](#easycat_e603)

### EASYCAT_E603

**Manifest field {field!r} must be an env reference (bearer-env:NAME), not a literal secret.**

- **Cause:** A `auth`/`token` field carried a literal-looking secret. Secrets must never be committed to `easycat.toml`; the loader requires the `bearer-env:NAME` grammar and resolves the value from the environment at load time so a token never appears in the manifest, logs, or `--json`/`/manifest` dumps.
- **Fix:** Replace the literal with an env reference, e.g. `auth = "bearer-env:EASYCAT_SERVE_TOKEN"`, and export the secret in the environment (`export EASYCAT_SERVE_TOKEN=...`).
- **Example:** `auth = "bearer-env:EASYCAT_SERVE_TOKEN"`
- **Related:** [EASYCAT_E602](#easycat_e602)

### EASYCAT_E604

**Manifest env reference {reference!r} points at an unset variable {var!r}.**

- **Cause:** An `auth`/`token` field used the `bearer-env:NAME` grammar, but the named environment variable is unset or empty at load time.
- **Fix:** Export the variable before serving (`export {var}=...`), or use a `.env` file and verify with `easycat doctor --env-file .env`.
- **Example:** `export EASYCAT_SERVE_TOKEN=...`
- **Related:** [EASYCAT_E603](#easycat_e603)

### EASYCAT_E605

**Manifest agent reference {reference!r} could not be resolved: {detail}**

- **Cause:** A `[voice.<profile>] agent` used the `python:module:function` grammar, but the module or attribute could not be imported, or it was not callable. The resolver imports lazily so a typo or a missing extra surfaces here.
- **Fix:** Check the dotted path resolves (`python -c 'import module'`), that the attribute exists and is callable, and that any provider extra the agent needs is installed.
- **Example:** `agent = "python:app:create_agent"`
- **Related:** [EASYCAT_E602](#easycat_e602)
