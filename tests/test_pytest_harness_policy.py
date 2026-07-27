"""Guards for the pytest harness settings that keep a hung test cheap.

These are easy to mistake for tuning knobs and quietly drop. They are not:
together they decide whether one wedged test costs the run a few seconds or
half an hour.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

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
