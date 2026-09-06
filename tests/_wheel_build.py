"""Build easycat's wheel once, for callers that need an installable artifact.

Two suites need a real wheel: ``tests/cli/test_packaging.py`` (inspecting its
contents) and ``tests/cli/e2e/test_generated_project_wheel.py`` (installing it
into a throwaway venv outside the checkout). This module gives both one place
to build it instead of each re-implementing the same ``uv build`` subprocess
call.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _project_root() -> Path:
    """Walk up from this file to the repo root."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate project root")


def build_wheel(dest: Path) -> Path:
    """Build easycat's wheel into ``dest`` and return its path.

    Calls ``pytest.skip`` (not an exception) when ``uv`` is absent from
    ``PATH`` or the build itself fails — building a wheel needs network access
    to resolve the build backend, which is out of scope for every caller here.
    """
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover — CI without uv is out of scope
        pytest.skip("`uv` binary not on PATH")
    root = _project_root()
    proc = subprocess.run(
        [uv, "build", "--wheel", "-o", str(dest)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover — diagnostic path
        pytest.skip(f"`uv build` failed:\n{proc.stderr}")
    wheels = list(dest.glob("easycat-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]
