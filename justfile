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
sync-extra *EXTRAS:
    uv sync --group dev {{ prepend('--extra ', EXTRAS) }}

# Run the full test suite (serial, deterministic). Source of truth.
test:
    uv run pytest

# Run the safe slice in parallel. `loadscope` keeps each module's tests
# (async event-loop / socket / port tests) pinned to one worker. Mirrors the
# `quick` validation slice marker expression (validation/runner.py).
test-fast:
    uv run pytest -n auto --dist loadscope -m "not integration_socket and not integration_live and not slow and not stress and not flaky"

# Run a single file or node id. Usage: just test-one tests/test_cancel.py
# or: just test-one tests/test_cancel.py::TestCancelToken::test_cancel
test-one TARGET:
    uv run pytest "{{ TARGET }}"

# Lint with ruff (E, F, I, W, UP).
lint:
    uv run ruff check .

# Auto-fix lint findings where ruff can.
lint-fix:
    uv run ruff check --fix .

# Format the codebase.
fmt:
    uv run ruff format .

# Verify formatting without writing (matches CI's `ruff format --check`).
fmt-check:
    uv run ruff format --check .

# Authoritative type gate: the clean core CI gates on (must stay green).
typecheck:
    uv run mypy --follow-imports=silent src/easycat/debug

# Advisory whole-repo mypy report (mirrors the non-blocking CI step).
typecheck-all:
    uv run mypy src/easycat

# Fast local-only type feedback via Astral ty (beta; not a CI gate).
# Runs on demand through uvx, so no dev-dependency install is needed.
typecheck-fast:
    uvx ty check src/easycat

# Coverage over the safe slice (pytest --cov is xdist-safe; never use
# `coverage run -m pytest -n auto`, which reports 0% under xdist).
cov:
    uv run pytest -n auto --dist loadscope --cov --cov-report=term-missing -m "not integration_socket and not integration_live and not slow and not stress and not flaky"

# Guard root onboarding docs, the docs route map, and docs CLI JSON.
guard-docs:
    uv run pytest tests/test_quickstart_e2e.py::test_readme_choose_your_path_routes_primary_onboarding_surfaces tests/test_docs_index.py tests/cli/test_app.py::test_docs_command tests/cli/test_app.py::test_docs_command_json

# Guard teaching ladder chapters, generated README blocks, and learner route hints.
guard-teaching:
    uv run pytest tests/teaching tests/test_docs_index.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/test_install_guidance.py::test_teaching_ladder_prerequisites_run_doctor_after_setup tests/test_install_guidance.py::test_teaching_chapter_key_prerequisites_run_doctor tests/test_install_guidance.py::test_teaching_provider_key_setup_names_required_extras

# Guard the examples chooser, README command hints, and docs-route example hints.
guard-examples:
    uv run pytest tests/test_examples.py::test_examples_readme_choose_example_table_tracks_matrix tests/test_examples.py::test_examples_readme_command_hints_are_locally_valid tests/test_docs_index.py::test_examples_docs_route_matches_examples_fast_path

# Guard scaffold template READMEs and template catalog output.
guard-templates:
    uv run pytest tests/cli/test_templates.py tests/cli/test_init.py::test_list_templates tests/cli/test_init.py::test_list_templates_json

# Guard contributor guidance, validation plan state, and contributor docs route hints.
guard-contributing:
    uv run pytest tests/test_contributing.py tests/test_docs_index.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_validation_plan.py

# Guard maintained Markdown links and anchors.
guard-markdown:
    uv run pytest tests/test_markdown_links.py

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
    uv run easycat validate report "{{ REPORT }}"

# The pre-PR gauntlet: format check + lint + full serial test suite.
check: fmt-check lint test

# Run all pre-commit hooks against the whole tree.
pre-commit:
    uv run pre-commit run --all-files
