"""Build easycat's distributions once, for callers that need a real artifact.

Two suites need a built distribution: ``tests/cli/test_packaging.py``
(inspecting wheel and sdist contents) and
``tests/cli/e2e/test_generated_project_wheel.py`` (installing the wheel into a
throwaway venv outside the checkout). This module gives both one place to
locate the repo root and to run ``uv build`` instead of each re-implementing
the same subprocess call.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

import pytest

_BUILD_TIMEOUT_S = 600.0

_KINDS: dict[str, tuple[str, str]] = {
    # kind -> (uv build flag, glob for the artifact it produces)
    "wheel": ("--wheel", "easycat-*.whl"),
    "sdist": ("--sdist", "easycat-*.tar.gz"),
}


def project_root() -> Path:
    """Walk up from this file to the repo root (the directory with pyproject)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate project root")


def build_dist(
    dest: Path,
    *,
    kind: Literal["wheel", "sdist"] = "wheel",
    strict: bool = False,
) -> Path:
    """Build one easycat distribution into ``dest`` and return its path.

    ``uv`` missing from ``PATH`` is always an environmental precondition, so
    that skips. A *failed build* is a real defect, but it stays a skip by
    default because the historical ``test_packaging.py`` fixtures treated a
    build that cannot reach the network for its build backend as out of
    scope. Callers whose whole purpose is to detect a broken build — the
    wheel e2e rehearsal — pass ``strict=True`` and get a failure instead.
    """
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover — CI without uv is out of scope
        pytest.skip("`uv` binary not on PATH")
    flag, pattern = _KINDS[kind]
    proc = subprocess.run(
        [uv, "build", flag, "-o", str(dest)],
        cwd=project_root(),
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        detail = f"`uv build {flag}` failed:\n{proc.stdout}\n{proc.stderr}"
        if strict:
            raise AssertionError(detail)
        pytest.skip(detail)  # pragma: no cover — diagnostic path
    built = list(dest.glob(pattern))
    assert len(built) == 1, f"expected one {kind}, got {built}"
    return built[0]


def build_wheel(dest: Path, *, strict: bool = False) -> Path:
    """Build easycat's wheel into ``dest`` and return its path."""
    return build_dist(dest, kind="wheel", strict=strict)


def build_sdist(dest: Path, *, strict: bool = False) -> Path:
    """Build easycat's sdist into ``dest`` and return its path."""
    return build_dist(dest, kind="sdist", strict=strict)
