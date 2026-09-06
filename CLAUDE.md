# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For coding work, use this file and [AGENTS.md](AGENTS.md) as the repository
guides. The root [llms.txt](llms.txt) file is a generated machine-readable docs
route map, and [llms-full.txt](llms-full.txt) is the same map expanded with
command hints for automation; they are not the primary coding instructions.

## Project Overview

EasyCat is a Python voice bot framework that runs idiomatic agents and
workflows from OpenAI Agents SDK, PydanticAI, LangChain, LangGraph,
LlamaAgents, Remote Responses API, or your own async workflow. It handles the
full audio pipeline: echo cancellation → noise reduction → VAD → STT → agent → TTS, with pluggable
providers at each stage.

## Commands

Prefer the `just` recipes when available; each recipe shells out through `uv`.
The raw `uv` commands below are the fallback when `just` is not installed. For
raw docs/onboarding guard commands, use the
[`CONTRIBUTING.md`](CONTRIBUTING.md#the-development-loop) command table.

```bash
uv sync --group dev              # Install project + dev tools
just                             # List every task
just check                       # Pre-commit + mypy + credential-free tests
just test-live                   # Explicit live-provider tests (may be billable)
just test-one tests/stt/test_stt_openai.py  # Run one test file
just validate-quick              # Deterministic local validation slice
```

Docs/onboarding guard recipes and their raw fallbacks below are generated
from the `justfile` by `uv run python scripts/regen_guard_commands.py`:

<!-- BEGIN auto:guard-commands format=bash -->
```bash
just guard-docs          # Guard root onboarding docs, install guidance, docs routes, public API docs, CLI JSON envelopes, and maintained Markdown links and anchors
just guard-teaching      # Guard teaching ladder chapters, generated README blocks, and learner route hints
just guard-examples      # Guard examples README, support files, script smoke checks, docs-route hints, and scaffold templates, init flows, catalog output, generated project smoke, and secret/artifact hygiene
just guard-contributing  # Guard contributor guidance, agent guide contracts, validation state, and route hints
just guard-validation    # Guard validation workflow docs, validation reference docs, and validate CLI behavior
just guard-contracts     # Guard provider contract docs, offline contract suite, contract kit, and provider wiring matrix
just guard-ops           # Guard operator docs, deployment guide, observability docs, journal CLI, and durability
uv run pytest tests/test_quickstart_e2e.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py tests/test_markdown_links.py  # Raw fallback for just guard-docs
uv run pytest tests/teaching tests/docs/test_route_contracts.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/install/test_teaching_prerequisites.py  # Raw fallback for just guard-teaching
uv run pytest tests/examples tests/docs/test_route_contracts.py::test_examples_docs_route_matches_examples_fast_path tests/cli/test_scaffold_schema.py tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py -m 'not integration_external'  # Raw fallback for just guard-examples
uv run pytest tests/test_contributing.py tests/docs/test_route_contracts.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_regen_guard_commands.py tests/install/test_agent_guides.py  # Raw fallback for just guard-contributing
uv run pytest tests/docs/test_route_contracts.py::test_validation_docs_route_matches_validation_workflow_commands tests/docs/test_command_hints.py::test_validation_workflow_command_hints_are_locally_valid tests/docs/test_route_contracts.py::test_validation_reference_docs_route_matches_json_commands tests/cli/test_validate_report_model.py tests/cli/test_validate_live.py tests/cli/test_validate_runner.py tests/cli/test_validate_cli.py tests/cli/test_validate_report_cli.py tests/cli/test_latency_selectors_artifacts.py tests/cli/test_latency_reliability_failures.py tests/cli/test_latency_runner.py tests/cli/test_latency_cli.py tests/cli/test_latency_baseline_budgets.py  # Raw fallback for just guard-validation
uv run pytest tests/docs/test_route_contracts.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/testing  # Raw fallback for just guard-contracts
uv run pytest tests/docs/test_route_contracts.py::test_deployment_docs_route_matches_docker_commands tests/docs/test_route_contracts.py::test_observability_docs_route_matches_journal_cli_entry_points tests/docs/test_route_contracts.py::test_journal_durability_docs_route_matches_inspection_commands tests/examples/test_deploy_and_browser_docs.py tests/observability tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py  # Raw fallback for just guard-ops
```
<!-- END auto:guard-commands -->

```bash
uv run pytest                    # Credential-free suite (live/external/serial excluded)
uv run pytest tests/stt/test_stt_openai.py              # Run one test file
uv run pytest tests/validation/test_latency_percentiles.py::test_latency_percentile_stats_from_values_empty_input  # Run one test
uv run pytest tests/install/test_install_guidance.py    # Verify onboarding/install guidance
uv run easycat docs              # Compact docs route/audience index
uv run easycat docs --verbose    # Full docs map with command hints
uv run easycat docs --audience maintainers  # Maintainer-focused docs map
uv run easycat docs --audience maintainers --json  # JSON maintainer-focused docs map
uv run easycat docs --json       # Docs route map with audiences and command hints
uv run easycat doctor --json     # Parseable first-run environment checks
uv run easycat doctor --env-file .env --json  # Parseable checks with project .env loaded
uv run easycat explain json-schema  # CLI JSON envelope and field contract
uv run python scripts/regen_llms_txt.py  # Regenerate llms.txt / llms-full.txt from the docs route map
uv run python scripts/regen_llms_txt.py --check  # Verify generated llms.txt / llms-full.txt are current
uv run easycat bundles show PATH --json  # Parseable bundle/journal summary
uv run easycat bundles export PATH --output DIR --json  # Redacted context pack metadata
uv run easycat replay PATH --json  # Parseable replay summary
uv run easycat validate quick    # Repo-local validation CLI
uv run easycat validate quick --json  # JSON quick validation envelope
uv run easycat validate contracts --json  # JSON contract validation envelope
uv run easycat validate release --json  # JSON release validation envelope
uv run easycat validate report .easycat/validation/latest.json  # Inspect latest report
uv run easycat validate report .easycat/validation/latest.json --json  # JSON report envelope
uv run ruff check .              # Lint
uv run ruff format .             # Format
uv run easycat doctor            # Check credentials/extras before examples
uv run python examples/ws_server.py  # Run an example
```

## Architecture

**Pipeline flow:** Transport (audio in) → EchoCanceller → NoiseReducer → VAD → STT → [SmartTurn] → Agent → TTS → Transport (audio out). AEC runs on the raw mic signal *before* NoiseReducer because NR's nonlinear processing breaks AEC convergence. The `EchoCanceller` also consumes transport-accepted bot playback as reference audio (fed in by `session/_audio_router.py`) so it can subtract the bot's own playback from the captured mic signal. The reference is playback accepted at the transport boundary, not raw TTS provider output — feeding generated audio would teach the canceller about sound the listener may never receive.

The full architecture explanation — the `session/` collaborator map
(`session/_builder.py`, `session/_wiring.py`, `session/_turn_runner.py`, …),
stage and provider layers, agent bridges, and the dual-backend fallback
chains — lives in [docs/architecture.md](docs/architecture.md). Update that
page when moving modules; this file keeps only the orientation map below.
New maintainers should use the
[developer textbook](docs/development/) for the chapter-by-chapter source,
test, decision, and pitfall tour.

**Package map:**
- `session/` — core orchestrator package: `Session` lifecycle plus per-concern collaborators (`session/_builder.py` constructs them, `session/_wiring.py` wires them, `session/_turn_runner.py` drives a turn).
- `config/` — `EasyConfig` (simplified, auto-wires OpenAI providers) and `SessionConfig` (advanced, explicit providers). `create_session()` factory builds a wired Session. Field reference: [docs/reference/easyconfig.md](docs/reference/easyconfig.md).
- `events.py` — `EventBus` pub/sub. Two event layers: provider-scoped (`STTEvent`, `TTSEvent`) emitted by providers, mapped to EasyCat-level events (`STTFinal`, `TTSAudio`, `TurnStarted`, etc.) by Session. Catalog: [docs/reference/events.md](docs/reference/events.md).
- `providers.py` — `@runtime_checkable` Protocol definitions for all provider interfaces. Providers use duck typing, not inheritance.
- `turn_manager.py` — 5-state turn FSM with pre-roll buffering and interruption detection; `smart_turn.py` — optional ONNX endpoint detection.
- `runtime/` — journal-based debug-first runtime; the journal is the single source of truth for all observability. `validation/` — report models and runners behind the `easycat validate` lanes.
- `stages/` — pipeline stages wrapping providers; `debug/` — `RunBundle` serialization; `debugger/` — aiohttp debugger UI; `cli/` — Typer command surface.
- `server/` — the process layer behind `run_webrtc_config_server()` and `VoiceServer.from_app(...)`: route stack, auth, health/readiness, metrics, and the WebSocket/WebRTC/WebTransport server helpers.
- Provider subpackages `stt/`, `tts/`, `vad/`, `transports/`, `telephony/` — one provider per file; `AudioQueueMixin`, `ServerTransportBase`, and `TransportDegraded` are re-exported from `easycat.transports` for out-of-tree transports (see `docs/extending/`); agent bridges in `integrations/agents/` behind the `ExternalAgentBridge` protocol.
- `_turn_context.py` (package root) — `TurnContext` per-turn state and the `TurnHandle` protocol; a leaf depending only on `cancel.py` so `session/` and `stages/` both import downward.

## Key Patterns

- **Protocol over inheritance** — all providers defined as `typing.Protocol` in `providers.py`
- **Async-first** — all I/O is async; providers are async iterators
- **Cooperative cancellation** — `CancelToken` (not exceptions) for turn/TTS cancellation
- **Factory functions** — `create_session()`, `create_vad()`, `create_noise_reducer()`
- **Provider registries** — `stt/factory.py` and `tts/factory.py` each build a `ProviderCatalog` (`_provider_catalog.py`) from one `ProviderSpec` per backend. The catalog derives `_PROVIDER_TO_CONFIG`, credential, install-extra, and API-domain views used by doctor, scaffolding, validation, and redaction. To add a built-in provider, add its config dataclass to the matching typing union and add one spec; do not edit the derived views directly.
- **Event bus injection** — a provider config that declares optional `event_bus` receives the session bus when unset; no provider requires it. Injected STT/TTS/VAD/noise/AEC/transport instances that emit provider-scoped events expose synchronous `set_event_bus(bus)`; private attribute probes are compatibility-only.
- **Noop stubs** (`stubs.py`) — `NoopSTT`, `NoopTTS`, `NoopVAD`, `NoopTransport` for test isolation

## Session Lifecycle

Reader-facing reference: [docs/reference/session-lifecycle.md](docs/reference/session-lifecycle.md).

- `await session.stop()` is the single public teardown verb: `force=False` (default) drains in-flight work gracefully, while `force=True` cancels it first. `async with session:` is the preferred scoped idiom and calls `stop(force=True)` on exit
- Backend teardown (SQLite/Litestream/libSQL/artifact stores), the journal clean-close marker, and the preserved read-only postmortem view are handled internally by `stop()`
- After a clean `stop()`, `session.journal.read()` and `session.export_debug_bundle(...)` must still work through the preserved read-only postmortem view

## Style

- Python ≥3.11, typing-first
- 4-space indent, 99-char line limit (ruff)
- Ruff extensions: E, F, I, W, UP, C901, PLR0912, PLR0915, ASYNC, B, RUF006, T201, A001, A003, LOG, PERF203, PERF403, TID251
- Commit format: `<scope>: <imperative summary>` (e.g., `stt: normalize partial transcript events`)

## Testing

- pytest with pytest-asyncio (`asyncio_mode = auto`)
- `@pytest.mark.integration_live` for live API tests (skipped without credentials);
  pair live/contract/latency tests with provider and surface markers
- `@pytest.mark.integration_external` for external local binaries, SDKs, or
  services that do not use live provider API credentials
- Tests mirror source structure: `tests/stt/`, `tests/tts/`, `tests/vad/`, `tests/session/`, `tests/transports/`, `tests/validation/`, `tests/cli/`, `tests/debugger/`, etc.
