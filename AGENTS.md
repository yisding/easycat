# Repository Guidelines

## Project Structure & Module Organization
- `src/easycat/`: core library code.
- Key subpackages: `session/`, `stages/`, `stt/`, `tts/`, `vad/`, `transports/`, `telephony/`, `integrations/agents/`, `runtime/`, `validation/`, `debug/`, `debugger/`, `cli/`.
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
- `just check`: run the pre-PR gauntlet (format check, lint, full serial tests).
- `just validate-quick`: run the deterministic local validation slice.
- `just guard-docs`: guard root onboarding docs, README command hints, `easycat docs`, and docs JSON.
- `just guard-teaching`: guard teaching ladder chapters and generated README blocks.
- `just guard-examples`: guard the examples chooser, README command hints, and docs-route hints.
- `just guard-templates`: guard scaffold READMEs, catalog output, and catalog next steps.
- `just guard-contributing`: guard contributor docs, agent guide commands, validation state, and route hints.
- `just guard-markdown`: guard maintained Markdown links and anchors.
- Raw fallback for `just guard-docs`: `uv run pytest tests/test_quickstart_e2e.py::test_readme_choose_your_path_routes_primary_onboarding_surfaces tests/test_command_hints.py tests/test_install_guidance.py::test_readme_cli_command_examples_are_locally_valid tests/test_docs_index.py tests/cli/test_app.py::test_docs_command tests/cli/test_app.py::test_docs_command_json`.
- Raw fallback for `just guard-teaching`: `uv run pytest tests/teaching tests/test_docs_index.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/test_install_guidance.py::test_teaching_ladder_prerequisites_run_doctor_after_setup tests/test_install_guidance.py::test_teaching_chapter_key_prerequisites_run_doctor tests/test_install_guidance.py::test_teaching_provider_key_setup_names_required_extras`.
- Raw fallback for `just guard-examples`: `uv run pytest tests/test_examples.py::test_examples_readme_choose_example_table_tracks_matrix tests/test_examples.py::test_examples_readme_command_hints_are_locally_valid tests/test_docs_index.py::test_examples_docs_route_matches_examples_fast_path`.
- Raw fallback for `just guard-templates`: `uv run pytest tests/cli/test_templates.py tests/cli/test_init.py::test_list_templates tests/cli/test_init.py::test_list_templates_json tests/cli/test_init.py::test_init_list_templates_json_catalog_includes_next_step_commands`.
- Raw fallback for `just guard-contributing`: `uv run pytest tests/test_contributing.py tests/test_install_guidance.py::test_agent_guide_command_hints_are_locally_valid tests/test_docs_index.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_validation_plan.py`.
- Raw fallback for `just guard-markdown`: `uv run pytest tests/test_markdown_links.py`.
- `uv run pytest`: run full test suite.
- `uv run pytest tests/tts/test_tts_openai.py`: run a focused test file.
- `uv run pytest tests/transports/test_webrtc.py`: run focused WebRTC transport tests.
- `uv run easycat docs`: show the maintained docs map for quickstart,
  examples, teaching, architecture and maintenance, validation, and operations.
- `uv run easycat docs --audience coding-agents`: show the coding-agent
  route slice without scanning the full map.
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
- `uv run python examples/ws_server.py`: run a local example.
- `uv run python examples/webrtc_server.py`: run the WebRTC example server.

## Coding Style & Naming Conventions
- Python `>=3.11`; match existing typing-first style.
- Prefer async-first code paths and typed protocols/interfaces for provider boundaries.
- Use 4-space indentation and keep lines within Ruff’s configured limit (`99`).
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Keep provider implementations focused (one provider per file) and prefer small, composable modules.
- When adding STT/TTS providers, update both config dataclasses and central factory registries.
- Let Ruff manage import ordering and common style rules; run it before opening a PR.

## Testing Guidelines
- Framework: `pytest` with `pytest-asyncio` (`asyncio_mode = auto`).
- Test files use `test_*.py`; test functions use `test_*`.
- Put tests near related domain folders (audio, session, turns, transports, providers, agents, websocket, telephony, VAD, validation, CLI, debugger).
- For live API tests, use `@pytest.mark.integration_live`, pair it with provider
  and surface markers, and skip when credentials are missing.
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
- `await session.stop()` is the single public teardown verb: `force=False` (default) drains in-flight work gracefully, `force=True` cancels it first. `async with session:` is the preferred idiom (it calls `stop(force=True)` on exit); `session.shutdown()` remains a thin alias for `stop(force=True)`.
- Backend teardown (SQLite/Litestream/libSQL/artifact stores) and the journal clean-close marker are handled internally by `stop()` via the private `Session._destroy()` / `Session._close()` primitives — these are not public entry points.
- After a clean `stop()`, postmortem inspection is still valid: `session.journal.read()` and `session.export_debug_bundle(...)` continue to work through the preserved read-only view.
