"""Global pytest configuration for integration test capability gating."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from tests._hypothesis_profiles import register_hypothesis_profiles
from tests._marker_lint import validate_flaky_marker, validate_provider_surface_markers

# Typer bakes FORCE_TERMINAL at import time when GITHUB_ACTIONS / FORCE_COLOR /
# PY_COLORS is set, switching --help to ANSI panel rendering that the CLI help
# assertions (and scaffold smoke subprocesses) don't expect. Pin plain-text
# rendering before typer loads — and re-pin the baked constant if it already
# did. Subprocess-spawned CLI calls inherit the scrubbed environment.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
for _ci_color_var in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_ci_color_var, None)
if "typer.rich_utils" in sys.modules:
    sys.modules["typer.rich_utils"].FORCE_TERMINAL = False

register_hypothesis_profiles()

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Near-pure prose/route-scanning trees whose whole directory is guard coverage.
GUARD_DIRS = {"tests/docs", "tests/install", "tests/examples"}
# Individual root-level guard modules that scan Markdown, routes, or generated
# blocks rather than exercising product runtime.
GUARD_FILES = {
    "tests/observability/test_docs.py",
    "tests/test_markdown_links.py",
    "tests/test_llms_txt.py",
    "tests/test_command_hints.py",
    "tests/test_regen_guard_commands.py",
    "tests/test_contributing.py",
    # Teaching prose/generated-block scanners; the rest of tests/teaching is
    # behavioral (executes chapter scripts) and stays in the fast loop.
    "tests/teaching/test_regen_teaching_chapters.py",
    "tests/teaching/test_ladder_index.py",
    "tests/teaching/test_diagrams.py",
}
# Behavioral modules that live in a guard dir but must stay in the fast loop.
GUARD_EXEMPT = {
    "tests/docs/test_command_hint_validator.py",
    "tests/examples/test_example_imports.py",
    "tests/examples/test_script_execution.py",
    "tests/examples/test_deploy_and_browser_docs.py",
    "tests/examples/test_timezone_tools.py",
}


def _guard_rel_path(item: pytest.Item) -> str | None:
    """Return the repo-relative POSIX path of a collected test file."""
    try:
        return item.path.relative_to(_REPO_ROOT).as_posix()
    except (ValueError, AttributeError):
        return None


def _can_bind_localhost() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return True
    except OSError:
        return False


_HAS_LOCALHOST_SOCKET_ACCESS = _can_bind_localhost()


def _format_task(task: asyncio.Task[object]) -> str:
    coroutine = task.get_coro()
    name = getattr(coroutine, "__qualname__", None) or getattr(coroutine, "__name__", None)
    return f"{task.get_name()} ({name or coroutine!r})"


@pytest.fixture(autouse=True)
def _restore_easycat_logger_state():
    """Restore the ``easycat`` logger after each test.

    ``enable_console_logging`` (reached via ``run()``, ``EasyConfig`` debug
    modes, and the console/serve CLI paths) attaches a handler and flips
    ``propagate = False`` on the ``easycat`` logger. Left in place, that state
    leaks across tests and blinds ``caplog`` (which relies on root-handler
    propagation) for every later test in a serial full-suite run. Snapshot and
    restore handlers, level, and propagate so each test sees pristine state.
    """
    logger = logging.getLogger("easycat")
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    yield
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


@pytest_asyncio.fixture(autouse=True)
async def fail_on_leaked_asyncio_tasks(request: pytest.FixtureRequest):
    """Fail async tests that leave new pending tasks on the pytest event loop."""
    if request.node.get_closest_marker("allow_task_leak") is not None:
        yield
        return

    loop = asyncio.get_running_loop()
    before = set(asyncio.all_tasks(loop))
    yield

    # Let callbacks scheduled by the test and by dependent fixture teardowns run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    current = asyncio.current_task(loop)
    leaked = [
        task
        for task in asyncio.all_tasks(loop)
        if task not in before and task is not current and not task.done()
    ]
    if not leaked:
        return

    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
    leaked_summary = ", ".join(sorted(_format_task(task) for task in leaked))
    pytest.fail(f"asyncio task leak detected: {leaked_summary}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marker_errors: list[str] = []
    for item in items:
        marker_names = {marker.name for marker in item.iter_markers()}
        marker_errors.extend(validate_provider_surface_markers(item.nodeid, marker_names))
        flaky_marker = item.get_closest_marker("flaky")
        marker_errors.extend(
            validate_flaky_marker(
                item.nodeid,
                marker_names,
                flaky_marker.kwargs if flaky_marker is not None else {},
            )
        )

    if marker_errors:
        errors = "\n- ".join(marker_errors)
        raise pytest.UsageError(f"validation marker metadata errors:\n- {errors}")

    for item in items:
        rel = _guard_rel_path(item)
        if rel is None or rel in GUARD_EXEMPT:
            continue
        if any(rel.startswith(d + "/") for d in GUARD_DIRS) or rel in GUARD_FILES:
            item.add_marker(pytest.mark.guard)

    if _HAS_LOCALHOST_SOCKET_ACCESS:
        return

    skip_socket = pytest.mark.skip(
        reason="integration_socket tests require localhost socket access in this environment"
    )
    for item in items:
        if item.get_closest_marker("integration_socket") is not None:
            item.add_marker(skip_socket)
