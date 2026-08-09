"""Guards for the pytest harness settings that keep a hung test cheap.

These are easy to mistake for tuning knobs and quietly drop. They are not:
together they decide whether one wedged test costs the run a few seconds or
half an hour.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

from tests.conftest import _MAX_AUTO_XDIST_WORKERS, _xdist_auto_worker_count

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ini_options() -> dict:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["pytest"]["ini_options"]


def test_xdist_allows_one_replacement_without_a_restart_cycle() -> None:
    """A single crashed worker is replaced, but repeated crashes stop the run.

    ``timeout_method = "thread"`` force-exits the worker PROCESS, and xdist's
    default restart budget is 4x the node count. One replacement lets the
    remaining queue continue after an isolated worker crash; stopping after a
    second crash prevents a long cycle of full-timeout worker replacements.
    """
    addopts = shlex.split(_ini_options()["addopts"])

    assert "--max-worker-restart=1" in addopts, (
        "pytest addopts must keep --max-worker-restart=1 so one crashed worker "
        "can be replaced without permitting a long restart cycle"
    )


def test_bare_pytest_excludes_live_external_and_serial_tests() -> None:
    """Bare runs stay non-billable and never fork a threaded pytest process."""
    addopts = shlex.split(_ini_options()["addopts"])
    marker_expression = addopts[addopts.index("-m") + 1]

    assert marker_expression == "not integration_live and not integration_external and not serial"


def test_timeout_settings_stay_ordered() -> None:
    """faulthandler must dump the stuck stack BEFORE the timeout kills it.

    If ``faulthandler_timeout`` ever meets or exceeds ``timeout``, the process
    is gone before the traceback is written and a hang becomes undebuggable --
    the failure says only that a worker crashed.
    """
    ini = _ini_options()

    assert ini["faulthandler_timeout"] < ini["timeout"], (
        "faulthandler_timeout must fire before timeout, or hung tests are "
        "reported with no stack to diagnose them from"
    )


def test_xdist_auto_workers_are_capped_on_large_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr("tests.conftest.os.cpu_count", lambda: 128)

    assert _xdist_auto_worker_count() == _MAX_AUTO_XDIST_WORKERS


def test_xdist_auto_workers_respect_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "3")

    assert _xdist_auto_worker_count() == 3


@pytest.mark.parametrize("override", ["four", "", "2.5", "0", "-1"])
def test_xdist_auto_workers_reject_invalid_overrides(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", override)

    with pytest.raises(pytest.UsageError, match="positive integer"):
        _xdist_auto_worker_count()


def test_ci_serial_lanes_list_every_serial_test_file() -> None:
    """The CI serial commands enumerate files; a new serial file must be added.

    Both workflows run the serial slice as an explicit file list (to skip a
    full collection pass), so a ``serial``-marked test in a file missing from
    those lists would silently never run in CI.
    """
    marker = re.compile(r"^\s*@pytest\.mark\.serial\b", re.MULTILINE)
    serial_files = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if marker.search(path.read_text(encoding="utf-8"))
    )
    assert serial_files, "expected at least one serial-marked test file"

    for workflow in (".github/workflows/ci.yml", ".github/workflows/nightly-validation.yml"):
        text = (REPO_ROOT / workflow).read_text(encoding="utf-8")
        missing = [name for name in serial_files if name not in text]
        assert missing == [], f"{workflow} serial lane is missing {missing}"


def test_sync_tests_do_not_start_an_asyncio_runner(request: pytest.FixtureRequest) -> None:
    assert "_function_scoped_runner" not in request.fixturenames
    assert "fail_on_leaked_asyncio_tasks" not in request.fixturenames


@pytest.mark.asyncio
async def test_async_tests_enable_task_leak_check(request: pytest.FixtureRequest) -> None:
    assert "_function_scoped_runner" in request.fixturenames
    assert "fail_on_leaked_asyncio_tasks" in request.fixturenames
