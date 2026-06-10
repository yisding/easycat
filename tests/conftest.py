"""Global pytest configuration for integration test capability gating."""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest
import pytest_asyncio

from tests._hypothesis_profiles import register_hypothesis_profiles
from tests._marker_lint import validate_flaky_marker, validate_provider_surface_markers

register_hypothesis_profiles()


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

    if _HAS_LOCALHOST_SOCKET_ACCESS:
        return

    skip_socket = pytest.mark.skip(
        reason="integration_socket tests require localhost socket access in this environment"
    )
    for item in items:
        if item.get_closest_marker("integration_socket") is not None:
            item.add_marker(skip_socket)
