# Repository Guidelines

For coding work, this file is the primary repository guide. Use
[llms.txt](llms.txt) only when you need the generated machine-readable docs
route map, and [llms-full.txt](llms-full.txt) when automation needs every docs
route command hint. Both are generated from the `easycat docs --json` route
table; regenerate with `uv run python scripts/regen_llms_txt.py` after editing
the docs route map.

New to the codebase? Read the
[developer textbook](docs/development/) for the guided architecture, runtime,
testing, decision, and change-recipe tour before making a cross-cutting change.

## Project Structure & Module Organization
- `src/easycat/`: core library code.
- Key subpackages: `session/`, `stages/`, `stt/`, `tts/`, `vad/`, `transports/`, `telephony/`, `integrations/agents/`, `runtime/`, `validation/`, `server/`, `debug/`, `debugger/`, `cli/`.
- Core orchestrators/utilities live alongside: `config/`, `events.py`, `turn_manager.py`, `smart_turn.py`, `timeouts.py`.
- Provider interfaces are centralized in `providers.py`; STT/TTS factory registries live in `stt/factory.py` and `tts/factory.py`.
- Agent framework bridges live in `src/easycat/integrations/agents/` (`OpenAIAgentsBridge`, `PydanticAIBridge`, `GenericWorkflowBridge`, `RemoteResponsesAPIBridge`, `LlamaAgentsBridge`, `LangChainBridge`, `LangGraphBridge`, plus `AgentRunner`).
- `src/easycat/models/`: runtime model assets (for example ONNX smart-turn model).
- `tests/`: pytest suite mirroring domains (`tests/stt/`, `tests/tts/`, `tests/vad/`, `tests/session/`, `tests/stages/`, `tests/transports/`, `tests/websocket/`, `tests/integrations/agents/`, `tests/telephony/`, `tests/runtime/`, `tests/validation/`, `tests/cli/`, `tests/debug/`, `tests/debugger/`).
- `examples/`: runnable reference apps covering local microphone, WebSocket, WebRTC, Twilio, and Cartesia/Deepgram/ElevenLabs provider swaps.

## Build, Test, and Development Commands
Prefer the `just` recipes when available; they shell out through `uv` and mirror
the raw commands below. For raw docs/onboarding guard commands, use the
[`CONTRIBUTING.md`](CONTRIBUTING.md#the-development-loop) command table.

- `uv sync --group dev`: install project + dev tools.
- `uv sync --extra <name> --group dev`: install optional provider/transport extras while keeping dev tools (for example `openai`, `openai-agents`, `webrtc`, `telephony`, `local`, `rnnoise`).
- `just`: list every developer task.
- `just check`: run the core local pre-PR gauntlet (pre-commit, mypy,
  credential-free local tests); CI additionally runs matrices, dependency
  floors, validation artifacts, and build smoke.
- `just test-live`: explicitly run live-provider tests serially; credentials
  may make these tests billable.
- `just validate-quick`: run the deterministic local validation slice.
<!-- BEGIN auto:guard-commands format=bullets -->
- `just guard-docs`: guard root onboarding docs, install guidance, docs routes, public API docs, CLI JSON envelopes, and maintained Markdown links and anchors.
- `just guard-teaching`: guard teaching ladder chapters, generated README blocks, and learner route hints.
- `just guard-examples`: guard examples README, support files, script smoke checks, docs-route hints, and scaffold templates, init flows, catalog output, generated project smoke, and secret/artifact hygiene.
- `just guard-contributing`: guard contributor guidance, agent guide contracts, validation state, and route hints.
- `just guard-validation`: guard validation workflow docs, validation reference docs, and validate CLI behavior.
- `just guard-contracts`: guard provider contract docs, offline contract suite, contract kit, and provider wiring matrix.
- `just guard-ops`: guard operator docs, deployment guide, observability docs, journal CLI, and durability.
- Raw fallback for `just guard-docs`: `uv run pytest tests/test_quickstart_e2e.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py tests/test_markdown_links.py`.
- Raw fallback for `just guard-teaching`: `uv run pytest tests/teaching tests/docs/test_route_contracts.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/install/test_teaching_prerequisites.py`.
- Raw fallback for `just guard-examples`: `uv run pytest tests/examples tests/docs/test_route_contracts.py::test_examples_docs_route_matches_examples_fast_path tests/cli/test_scaffold_schema.py tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py -m 'not integration_external'`.
- Raw fallback for `just guard-contributing`: `uv run pytest tests/test_contributing.py tests/docs/test_route_contracts.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_regen_guard_commands.py tests/install/test_agent_guides.py`.
- Raw fallback for `just guard-validation`: `uv run pytest tests/docs/test_route_contracts.py::test_validation_docs_route_matches_validation_workflow_commands tests/docs/test_command_hints.py::test_validation_workflow_command_hints_are_locally_valid tests/docs/test_route_contracts.py::test_validation_reference_docs_route_matches_json_commands tests/cli/test_validate_report_model.py tests/cli/test_validate_live.py tests/cli/test_validate_runner.py tests/cli/test_validate_cli.py tests/cli/test_validate_report_cli.py tests/cli/test_latency_selectors_artifacts.py tests/cli/test_latency_reliability_failures.py tests/cli/test_latency_runner.py tests/cli/test_latency_cli.py tests/cli/test_latency_baseline_budgets.py`.
- Raw fallback for `just guard-contracts`: `uv run pytest tests/docs/test_route_contracts.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/testing`.
- Raw fallback for `just guard-ops`: `uv run pytest tests/docs/test_route_contracts.py::test_deployment_docs_route_matches_docker_commands tests/docs/test_route_contracts.py::test_observability_docs_route_matches_journal_cli_entry_points tests/docs/test_route_contracts.py::test_journal_durability_docs_route_matches_inspection_commands tests/examples/test_deploy_and_browser_docs.py tests/observability tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py`.
<!-- END auto:guard-commands -->
- `uv run pytest`: run the credential-free local suite; `integration_live`,
  `integration_external`, and `serial` tests are excluded by the default
  `addopts` and require explicit `-m` selection.
- `uv run pytest tests/tts/test_tts_openai.py`: run a focused test file.
- `uv run pytest tests/transports/test_webrtc_config.py tests/transports/test_webrtc_lifecycle_server.py tests/transports/test_webrtc_stats_artifacts.py tests/transports/test_webrtc_outbound_audio.py tests/transports/test_webrtc_auth_browser_playground.py`: run focused WebRTC transport tests.
- `uv run easycat docs`: show the compact route-label and audience index.
- `uv run easycat docs --verbose`: expand every maintained docs route with
  descriptions and command hints.
- `uv run easycat docs --audience coding-agents`: show the coding-agent
  route slice without scanning the full map.
- `uv run easycat docs --audience coding-agents --json`: emit the
  coding-agent route slice with command hints for automation.
- `uv run easycat docs --json`: emit the same route map with audience labels
  and command hints for automation.
- `uv run easycat doctor --json`: emit parseable first-run environment checks.
- `uv run easycat doctor --env-file .env --json`: emit the same checks after
  loading project `.env` keys.
- `uv run easycat explain json-schema`: inspect the CLI JSON envelope and
  command-specific fields.
- `uv run easycat bundles show PATH --json`: emit a parseable debug
  bundle/journal summary.
- `uv run easycat bundles export PATH --output DIR --json`: write a redacted
  coding-agent context pack and emit export metadata.
- `uv run easycat replay PATH --json`: replay a bundle or journal and emit a
  parseable replay summary.
- `uv run easycat validate quick`: run the same deterministic validation lane without `just`.
- `uv run easycat validate quick --json`: emit the quick lane in the standard CLI JSON envelope.
- `uv run easycat validate contracts --json`: emit contract validation in the standard CLI JSON envelope.
- `uv run easycat validate release --json`: emit release validation in the standard CLI JSON envelope.
- `uv run easycat validate report .easycat/validation/latest.json`: inspect the
  latest saved validation report.
- `uv run easycat validate report .easycat/validation/latest.json --json`: emit
  that saved report inside the standard CLI JSON envelope.
- `uv run ruff check .`: lint (imports, style, correctness rules).
- `uv run ruff format .`: apply formatting.
- `uv run easycat doctor`: check API keys, optional extras, and provider
  reachability before running credentialed examples.
- `uv run python examples/ws_server.py`: run a local example.
- `uv run python examples/webrtc_server.py`: run the WebRTC example server.

## Coding Style & Naming Conventions
- Python `>=3.11`; match existing typing-first style.
- Prefer async-first code paths and typed protocols/interfaces for provider boundaries.
- Use 4-space indentation and keep lines within Ruff’s configured limit (`99`).
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Keep provider implementations focused (one provider per file) and prefer small, composable modules.
- When adding built-in STT/TTS providers, add the config dataclass to the
  matching typing union and add one `ProviderSpec` to the central catalog;
  do not edit derived provider maps directly.
- Let Ruff manage import ordering and common style rules; run it before opening a PR.

## Testing Guidelines
- Framework: `pytest` with `pytest-asyncio` (`asyncio_mode = auto`).
- In the workspace sandbox, async tests that call `asyncio.to_thread` can pass
  their assertions and then hang during pytest teardown in
  `asyncio.Runner.close()` while the default executor shuts down. When that
  exact stack appears with an idle executor worker, rerun the focused suite
  with the prescribed isolated `uv run` command outside the sandbox
  (escalated) before treating it as a product failure.
- Test files use `test_*.py`; test functions use `test_*`.
- Put tests near related domain folders (audio, session, turns, transports, providers, agents, websocket, telephony, VAD, validation, CLI, debugger).
- For live API tests, use `@pytest.mark.integration_live`, pair it with provider
  and surface markers, and skip when credentials are missing. Run them
  explicitly with `just test-live` or `uv run pytest -m integration_live`;
  ordinary pytest runs exclude them even when credentials are present.
- For tests that need external local binaries, SDKs, or services without live
  provider API credentials, use `@pytest.mark.integration_external`.
- No fixed coverage gate is enforced; add or update tests for every behavior change.

## Commit & Pull Request Guidelines
- Recent history shows short, imperative subjects (for example: `add smart turn`, `fix test cases`). Keep that style, but be specific.
- Recommended format: `<scope>: <imperative summary>` (example: `stt: normalize partial transcript events`).
- PRs should include: problem statement, change summary, and test evidence (`uv run pytest` / targeted runs).
- If behavior changes user-visible flows (examples/transports/telephony), include a brief usage note or sample output.

## Security & Configuration Tips
- Use environment variables for secrets (`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`); never commit keys.
- Example apps may also require deployment/runtime env vars such as `TWILIO_STREAM_URL`, `TURN_SERVER_URL`, `TURN_USERNAME`, and `TURN_CREDENTIAL`.
- Keep optional provider dependencies in extras and document any new env vars in `README.md`.

## Session Lifecycle Notes
- `await session.stop()` is the single public teardown verb: `force=False` (default) drains in-flight work gracefully, while `force=True` cancels it first. `async with session:` is the preferred scoped idiom and calls `stop(force=True)` on exit.
- Backend teardown (SQLite/Litestream/libSQL/artifact stores), the journal clean-close marker, and the preserved read-only postmortem view are handled internally by `stop()`.
- After a clean `stop()`, postmortem inspection is still valid: `session.journal.read()` and `session.export_debug_bundle(...)` continue to work through the preserved read-only view.
