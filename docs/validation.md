# Validation Workflow

For normal PR work, run the public quick validation lane:

```bash
uv run easycat validate quick
```

For docs and onboarding-only edits, run the narrower guard that owns the
surface first, then run quick validation before a PR. The guard command
blocks below are generated from the `justfile` by
`uv run python scripts/regen_guard_commands.py`:

<!-- BEGIN auto:guard-commands format=just-bash -->
```bash
just guard-docs          # root onboarding docs, install guidance, docs routes, public API docs, and CLI JSON envelopes
just guard-teaching      # teaching ladder chapters, generated README blocks, and learner route hints
just guard-examples      # examples README, support files, script smoke checks, and docs-route hints
just guard-templates     # scaffold templates, init flows, catalog output, generated project smoke, and secret/artifact hygiene
just guard-contributing  # contributor guidance, agent guide contracts, validation state, and route hints
just guard-validation    # validation workflow docs, validation reference docs, and validate CLI behavior
just guard-contracts     # provider contract docs, offline contract suite, contract kit, and provider wiring matrix
just guard-ops           # operator docs, deployment guide, observability docs, journal CLI, and durability
just guard-markdown      # maintained Markdown links, anchors, and docs-route Markdown targets
```
<!-- END auto:guard-commands -->

If `just` is not installed, use the raw command table in
[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop) for the equivalent
`uv run pytest ...` command behind each guard, or run the matching command
directly:

<!-- BEGIN auto:guard-commands format=raw-bash -->
```bash
uv run pytest tests/test_quickstart_e2e.py tests/test_command_hints.py tests/test_install_guidance.py tests/test_docs_index.py tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py
uv run pytest tests/teaching tests/test_docs_index.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/test_install_guidance.py::test_teaching_ladder_prerequisites_run_doctor_after_setup tests/test_install_guidance.py::test_teaching_chapter_key_prerequisites_run_doctor tests/test_install_guidance.py::test_teaching_provider_key_setup_names_required_extras
uv run pytest tests/test_examples.py tests/test_docs_index.py::test_examples_docs_route_matches_examples_fast_path
uv run pytest tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py
uv run pytest tests/test_contributing.py tests/test_docs_index.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_regen_guard_commands.py tests/test_validation_plan.py && uv run pytest tests/test_install_guidance.py -k 'agent_guide or agent_guides or claude_'
uv run pytest tests/test_docs_index.py::test_validation_docs_route_matches_validation_workflow_commands tests/test_docs_index.py::test_validation_workflow_command_hints_are_locally_valid tests/test_docs_index.py::test_validation_reference_docs_route_matches_json_commands tests/test_validation_plan.py tests/cli/test_validate.py tests/cli/test_latency_validation.py
uv run pytest tests/test_docs_index.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/testing tests/integration/test_provider_contract_matrix.py
uv run pytest tests/test_docs_index.py::test_deployment_docs_route_matches_docker_commands tests/test_docs_index.py::test_observability_docs_route_matches_journal_cli_entry_points tests/test_docs_index.py::test_journal_durability_docs_route_matches_inspection_commands tests/test_examples.py::test_docker_compose_binds_ws_port_to_loopback_and_requires_token tests/test_examples.py::test_docker_guide_serves_browser_client_from_localhost tests/test_examples.py::test_docker_env_secret_file_is_ignored_but_templates_are_allowed tests/test_examples.py::test_docker_guide_tracks_default_dockerfile_extras tests/test_examples.py::test_dockerfile_default_extras_cover_ws_server_golden_path tests/test_examples.py::test_docker_provider_swap_guidance_uses_known_extras_and_easyconfig tests/test_observability.py tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py
uv run pytest tests/test_markdown_links.py tests/test_docs_index.py::test_cli_docs_routes_resolve_locally tests/cli/test_app.py::test_docs_route_paths_resolve_to_local_sources
```
<!-- END auto:guard-commands -->

The quick validation lane runs deterministic local tests only: no live
credentials, no localhost socket lane, no slow tests, and no flaky quarantine.
Each run writes an isolated report under
`.easycat/validation/runs/<run_id>/report.json`, plus JUnit and stdout/stderr
logs, and updates `.easycat/validation/latest.json` after the report is
complete. `.easycat/validation/` is ignored by git; remove old run directories
when you no longer need the artifacts.

Use the socket lane when touching WebSocket, transport, or localhost
integration behavior:

```bash
uv run easycat validate socket
```

Other validation lanes use the same repo-local `uv run easycat validate`
command:

```bash
uv run easycat validate quick      # deterministic local validation
uv run easycat validate socket     # localhost socket / transport integration validation
uv run easycat validate stress     # local stress validation and saturation-signal capture
uv run easycat validate contracts  # offline provider/protocol/bridge contracts
uv run easycat validate latency --smoke # low-cost live latency validation
uv run easycat validate live       # live provider canaries (filter with --provider / --surface)
uv run easycat validate release    # build, install, and run release validation
uv run easycat validate report .easycat/validation/latest.json # render latest report summary
uv run easycat validate report .easycat/validation/latest.json --json # emit latest report in the standard envelope
```

`easycat validate release` builds the sdist and wheel, checks package metadata,
installs the wheel into a clean temporary venv, clears `PYTHONPATH`, verifies
the installed package outside the source tree, smokes `easycat --help`,
`easycat init`, `python -m easycat`, and documented top-level API imports, then
runs quick, stress, contracts, live, and latency release gates through that
installed environment. Use `--python`, `--extra`, `--provider`, and `--surface`
to match the release target.

`scripts/validate.py` remains as a compatibility shim for pytest-backed slice
runs, but new docs and local workflows should use
`uv run easycat validate`.

`--json` emits the standard machine-readable stdout envelope for validation
lanes such as `quick`, `contracts`, and `release`; `--report PATH` writes a
persisted validation report JSON, and `--junit PATH` writes JUnit XML
(available on the `quick`, `socket`, `stress`, and `contracts` lanes). Common
automation entry points are `uv run easycat validate quick --json`,
`uv run easycat validate contracts --json`,
`uv run easycat validate release --json`, and
`uv run easycat validate report .easycat/validation/latest.json --json`, which
re-emits the latest saved validation report inside the same envelope for
coding-agent consumers. For the lower-level marker/direct entry points, see
[`plan/validation/README.md`](../plan/validation/README.md).

Flaky quarantine is explicit debt. Use
`@pytest.mark.flaky(issue="...", owner="...", review_by="YYYY-MM-DD")`; missing
metadata, stale `review_by` dates, or release-scoped flaky tests fail
collection. Quick and socket validation exclude flaky tests.

Provider validation scope is tracked with provider and surface markers such as
`provider_openai` and `surface_stt`. See
[`plan/validation/reference.md`](../plan/validation/reference.md) for the
provider-surface matrix vocabulary covering extras, credential env vars,
contract status, cassette status, and live canaries.
