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

# Deterministic local validation slice (what CI's quick job runs).
validate-quick:
    uv run easycat validate quick

# Localhost socket integration slice.
validate-socket:
    uv run easycat validate socket

# The pre-PR gauntlet: format check + lint + full serial test suite.
check: fmt-check lint test

# Run all pre-commit hooks against the whole tree.
pre-commit:
    uv run pre-commit run --all-files
