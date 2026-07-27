"""Guards for the pytest harness settings that keep a hung test cheap.

These are easy to mistake for tuning knobs and quietly drop. They are not:
together they decide whether one wedged test costs the run a few seconds or
half an hour.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ini_options() -> dict:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["pytest"]["ini_options"]


def test_xdist_does_not_restart_workers_after_a_crash() -> None:
    """A crashing test must not be re-run on a parade of fresh workers.

    ``timeout_method = "thread"`` force-exits the worker PROCESS, and xdist's
    default policy replaces a dead worker and reassigns it the test that just
    killed its predecessor. A deterministically-hanging test therefore kills
    up to 4x-node-count workers, each costing a full ``timeout``, emitting no
    output the whole time -- ~32 minutes of apparent hang on an 8-core box,
    ending in the same test reported failed 33 times.
    """
    addopts = _ini_options()["addopts"]

    assert "--max-worker-restart=0" in addopts, (
        "pytest addopts must keep --max-worker-restart=0; without it a single "
        "hanging test stalls the whole run behind repeated worker restarts"
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
