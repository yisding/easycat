# EasyCat developer task runner. Install just: https://github.com/casey/just
# Run `just` (no args) to list every recipe. All recipes shell out via `uv run`
# so they work without a pre-activated virtualenv.
#
# COLUMNS is pinned so Rich-rendered CLI tables (e.g. `easycat bundles list`)
# keep full width: pytest-xdist workers have no TTY and would otherwise fall
# back to 80 cols and truncate filenames, failing tests/cli/test_bundles.py.
export COLUMNS := "200"

# Default: show the menu.
default:
    @just --list

# Install the project plus the dev dependency group.
sync:
    uv sync --group dev

# Install the project, dev group, and one or more optional extras.
# Usage: just sync-extra openai deepgram
# Install one or more optional provider or transport extras.
sync-extra *EXTRAS:
    uv sync --group dev {{ prepend('--extra ', EXTRAS) }}

# `loadscope` keeps each module on one worker. Direct-fork tests cannot safely
# run in an xdist worker at all, so the second command runs them serially with
# both watchdog threads (faulthandler and pytest-timeout) disabled.
# Run the full credential-free local test suite.
test:
    uv run pytest -n auto --dist loadscope -m "not integration_live and not integration_external and not serial"
    uv run pytest -o faulthandler_timeout=0 -o timeout=0 -m "serial and not integration_live and not integration_external"

# This is intentionally serial and may make billable provider API calls.
# Run every live-provider test explicitly (requires provider credentials).
test-live:
    uv run pytest -m integration_live

# Run the safe slice in parallel. Socket and timing-sensitive lanes are
# excluded, so xdist can balance individual tests across workers. Mirrors the
# `quick` validation slice marker expression (validation/runner.py).
# Run the deterministic quick validation slice in parallel.
test-fast:
    uv run pytest -n auto --dist load -m "not integration_socket and not integration_live and not integration_external and not contract and not latency and not slow and not stress and not serial and not flaky and not guard"

# Run a single file or node id. Usage: just test-one tests/core/test_cancel_token.py
# or: just test-one tests/core/test_cancel_token.py::TestCancelToken::test_cancel
# Run one test file or node id.
test-one TARGET:
    uv run pytest "{{ TARGET }}"

# Lint with Ruff's defaults plus extensions (E, F, I, W, UP, C901, PLR0912, PLR0915, ASYNC, B, RUF006, T201, A001, A003, LOG, PERF203, PERF403, TID251).
# Also runs the Import Linter contracts in pyproject.toml.
# Run source lint and architectural import contracts.
lint:
    uv run ruff check .
    uv run lint-imports

# Auto-fix lint findings where ruff can.
lint-fix:
    uv run ruff check --fix .

# Format the codebase.
fmt:
    uv run ruff format .

# Verify formatting without writing (matches CI's `ruff format --check`).
fmt-check:
    uv run ruff format --check .

# The vendored VAD runtime is excluded via a mypy override; core packages use
# the stricter overrides documented in pyproject.toml.
# Run the authoritative whole-package mypy gate.
typecheck:
    uv run mypy src/easycat

# Fast local-only type feedback via Astral ty (beta; not a CI gate).
# Runs on demand through uvx, so no dev-dependency install is needed.
# Run the fast advisory type checker.
typecheck-fast:
    uvx ty check src/easycat

# Uses pytest-cov because `coverage run -m pytest -n auto` reports 0% under xdist.
# Run coverage over the credential-free quick slice.
cov:
    uv run pytest -n auto --dist load --cov --cov-report=term-missing -m "not integration_socket and not integration_live and not integration_external and not contract and not latency and not slow and not stress and not serial and not flaky and not guard"

# Guard root onboarding docs, install guidance, docs routes, public API docs, CLI JSON envelopes, and maintained Markdown links and anchors.
guard-docs:
    uv run pytest tests/test_quickstart_e2e.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py tests/test_markdown_links.py

# Guard teaching ladder chapters, generated README blocks, and learner route hints.
guard-teaching:
    uv run pytest tests/teaching tests/docs/test_route_contracts.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/install/test_teaching_prerequisites.py

# Guard examples README, support files, script smoke checks, docs-route hints, and scaffold templates, init flows, catalog output, generated project smoke, and secret/artifact hygiene.
guard-examples:
    uv run pytest tests/examples tests/docs/test_route_contracts.py::test_examples_docs_route_matches_examples_fast_path tests/cli/test_scaffold_schema.py tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py -m 'not integration_external'

# Guard contributor guidance, agent guide contracts, validation state, and route hints.
guard-contributing:
    uv run pytest tests/test_contributing.py tests/docs/test_route_contracts.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_regen_guard_commands.py tests/install/test_agent_guides.py

# Guard validation workflow docs, validation reference docs, and validate CLI behavior.
guard-validation:
    uv run pytest tests/docs/test_route_contracts.py::test_validation_docs_route_matches_validation_workflow_commands tests/docs/test_command_hints.py::test_validation_workflow_command_hints_are_locally_valid tests/docs/test_route_contracts.py::test_validation_reference_docs_route_matches_json_commands tests/cli/test_validate_report_model.py tests/cli/test_validate_live.py tests/cli/test_validate_runner.py tests/cli/test_validate_cli.py tests/cli/test_validate_report_cli.py tests/cli/test_latency_selectors_artifacts.py tests/cli/test_latency_reliability_failures.py tests/cli/test_latency_runner.py tests/cli/test_latency_cli.py tests/cli/test_latency_baseline_budgets.py

# Guard provider contract docs, offline contract suite, contract kit, and provider wiring matrix.
guard-contracts:
    uv run pytest tests/docs/test_route_contracts.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/testing

# Guard operator docs, deployment guide, observability docs, journal CLI, and durability.
guard-ops:
    uv run pytest tests/docs/test_route_contracts.py::test_deployment_docs_route_matches_docker_commands tests/docs/test_route_contracts.py::test_observability_docs_route_matches_journal_cli_entry_points tests/docs/test_route_contracts.py::test_journal_durability_docs_route_matches_inspection_commands tests/examples/test_deploy_and_browser_docs.py tests/observability tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py

# Deterministic local validation slice (what CI's quick job runs).
validate-quick:
    uv run easycat validate quick

# Localhost socket integration slice.
validate-socket:
    uv run easycat validate socket

# Local stress validation and reliability artifacts.
validate-stress:
    uv run easycat validate stress

# Offline provider, protocol, and bridge contract validation.
validate-contracts:
    uv run easycat validate contracts

# Low-cost live latency probe. Requires live provider credentials.
validate-latency-smoke:
    uv run easycat validate latency --smoke

# OpenAI live provider canary. Requires OPENAI_API_KEY.
validate-live-openai:
    uv run easycat validate live --provider openai

# Strict installed-wheel release gate.
validate-release:
    uv run easycat validate release

# Render a saved validation report. Usage: just validate-report .easycat/validation/latest.json
validate-report REPORT=".easycat/validation/latest.json":
    uv run easycat validate report {{ quote(REPORT) }}

# Run all pre-commit hooks against the whole tree.
pre-commit:
    uv run pre-commit run --all-files

# CI additionally exercises version matrices, dependency floors, artifact lanes, and builds.
# Run the core local pre-PR gauntlet: pre-commit, mypy, and credential-free tests.
check: pre-commit typecheck test
