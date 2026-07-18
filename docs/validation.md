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
just guard-docs          # root onboarding docs, install guidance, docs routes, public API docs, CLI JSON envelopes, and maintained Markdown links and anchors
just guard-teaching      # teaching ladder chapters, generated README blocks, and learner route hints
just guard-examples      # examples README, support files, script smoke checks, docs-route hints, and scaffold templates, init flows, catalog output, generated project smoke, and secret/artifact hygiene
just guard-contributing  # contributor guidance, agent guide contracts, validation state, and route hints
just guard-validation    # validation workflow docs, validation reference docs, and validate CLI behavior
just guard-contracts     # provider contract docs, offline contract suite, contract kit, and provider wiring matrix
just guard-ops           # operator docs, deployment guide, observability docs, journal CLI, and durability
```
<!-- END auto:guard-commands -->

If `just` is not installed, use the raw command table in
[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop) for the equivalent
`uv run pytest ...` command behind each guard, or run the matching command
directly:

<!-- BEGIN auto:guard-commands format=raw-bash -->
```bash
uv run pytest tests/test_quickstart_e2e.py tests/test_command_hints.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py tests/test_markdown_links.py
uv run pytest tests/teaching tests/docs/test_route_contracts.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/install/test_teaching_prerequisites.py
uv run pytest tests/examples tests/docs/test_route_contracts.py::test_examples_docs_route_matches_examples_fast_path && uv run pytest tests/cli/test_scaffold_schema.py tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py -m 'not integration_external'
uv run pytest tests/test_contributing.py tests/docs/test_route_contracts.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_regen_guard_commands.py tests/install/test_agent_guides.py
uv run pytest tests/docs/test_route_contracts.py::test_validation_docs_route_matches_validation_workflow_commands tests/docs/test_command_hints.py::test_validation_workflow_command_hints_are_locally_valid tests/docs/test_route_contracts.py::test_validation_reference_docs_route_matches_json_commands tests/cli/test_validate_report_model.py tests/cli/test_validate_live.py tests/cli/test_validate_runner.py tests/cli/test_validate_cli.py tests/cli/test_validate_report_cli.py tests/cli/test_latency_selectors_artifacts.py tests/cli/test_latency_reliability_failures.py tests/cli/test_latency_runner.py tests/cli/test_latency_cli.py tests/cli/test_latency_baseline_budgets.py
uv run pytest tests/docs/test_route_contracts.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/testing
uv run pytest tests/docs/test_route_contracts.py::test_deployment_docs_route_matches_docker_commands tests/docs/test_route_contracts.py::test_observability_docs_route_matches_journal_cli_entry_points tests/docs/test_route_contracts.py::test_journal_durability_docs_route_matches_inspection_commands tests/examples/test_deploy_and_browser_docs.py tests/observability tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py
```
<!-- END auto:guard-commands -->

The quick validation lane runs deterministic local tests only: no live
credentials, no localhost socket lane, no external binary/service lane, no
contract lane, no slow tests, no flaky quarantine, and no `guard` tests. The
`guard` marker tags the prose-only overlay (Markdown, routes, generated blocks,
and documentation-to-code drift checks) within the broader `guard-*` lanes;
the fast dev loop (`just test-fast`, `just cov`, and
`uv run easycat validate quick`) skips them, while `just test` and `just check`
still run them. Some named guard lanes also own behavioral CLI and runtime
tests, so `uv run pytest -m guard` is useful for the prose overlay but is not a
replacement for the relevant named guard command above.
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

In GitHub Actions, pass `--show-output` to validation lanes. Validation still
writes `report.json`, `latest.json`, JUnit XML, and stdout/stderr logs, but
also prints the captured validation stdout/stderr to the job log so failures
are visible on github.com without downloading artifacts.

Flaky quarantine is explicit debt. Use
`@pytest.mark.flaky(issue="...", owner="...", review_by="YYYY-MM-DD")`; missing
metadata, stale `review_by` dates, or release-scoped flaky tests fail
collection. Quick and socket validation exclude flaky tests.

Provider validation scope is tracked with provider and surface markers such as
`provider_openai` and `surface_stt`. See
[`plan/validation/reference.md`](../plan/validation/reference.md) for the
provider-surface matrix vocabulary covering extras, credential env vars,
contract status, cassette status, and live canaries.
