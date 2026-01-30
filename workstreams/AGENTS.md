# Agents Guide

This document explains how coding agents (human or AI) should use the workstream folders to complete EasyCat development tasks.

## Workstream Folder Structure

Each workstream folder (e.g., `ws1-core-and-audio-foundation/`) contains:

- **README.md** — Overview of the workstream: goals, deliverables, dependencies on other workstreams, what can run in parallel, and acceptance criteria.
- **PLAN.md** — Ordered task list broken into phases. Each task has a description, implementation details, and test expectations.

### How to Use These Docs

1. **Start with README.md** to understand the workstream's scope and what it depends on. If your workstream depends on WS1 interfaces, verify those interfaces exist before writing implementations against them.
2. **Work through PLAN.md tasks in phase order.** Tasks within a phase can often be parallelized (the PLAN.md notes where this is possible), but phases should be completed sequentially since later phases build on earlier ones.
3. **Check off acceptance criteria** in README.md as you complete tasks. Every acceptance criterion should be covered by at least one test.

## Tooling Requirements

All code in this project must use the following tools. Do not deviate from these choices.

### Python

- **Python 3.14** — use 3.14 features where appropriate (e.g., deferred evaluation of annotations is the default, new `type` statement syntax).

### Package & Project Management: uv

- Use **uv** for all package and project management.
- `uv init` to initialize the project (if not already done).
- `uv add <package>` to add dependencies.
- `uv run <command>` to run commands within the project environment.
- `uv sync` to install/sync dependencies.
- Do **not** use `pip install`, `pip freeze`, `poetry`, or `conda`.

### Linting & Formatting: ruff

- Use **ruff** for all linting and formatting.
- `uv run ruff check .` to lint.
- `uv run ruff format .` to format.
- Fix all ruff errors before considering a task complete.
- Configure ruff in `pyproject.toml` under `[tool.ruff]`.

### Testing: pytest

- Use **pytest** for all tests.
- `uv run pytest` to run the full test suite.
- `uv run pytest tests/path/to/test_file.py` to run a specific test file.
- Every task in a PLAN.md that mentions "unit tests" or "integration tests" must have corresponding test files.
- Integration tests that require API keys should be marked with `@pytest.mark.integration` and skipped when credentials are absent.
- Use `pytest-asyncio` for async test functions.

## Coding Conventions

- Use `async`/`await` throughout — the voice pipeline is fundamentally asynchronous.
- Type-hint all function signatures.
- Use `Protocol` classes (from `typing`) for provider interfaces rather than ABC where possible, to allow structural subtyping.
- Keep modules small and focused. One provider per file, one interface per file.
- Write docstrings on public classes and functions only. Do not add docstrings to private helpers or test functions.

## Working Across Workstreams

- **WS1 is the foundation.** If you are working on WS2–WS8 and the interface you need from WS1 doesn't exist yet, coordinate with the WS1 engineer or write a stub that matches the expected interface shape from WS1's PLAN.md.
- **Don't import across workstream boundaries at the implementation level.** All cross-workstream communication goes through the interfaces and event types defined in WS1. Provider implementations should never import from each other.
- **Integration testing across workstreams** happens at the session/pipeline level (WS1 owns this). Individual workstreams test their own components in isolation using mocks and stubs.
