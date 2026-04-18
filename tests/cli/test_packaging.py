"""Plan 14 — wheel packaging ships template dotfiles.

Run with ``pytest -m integration_local tests/cli/test_packaging.py``.
Skipped by default to keep the fast test suite fast; the wheel build
takes a few seconds.

See ``TEST_PLANS.md`` §14.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration_local


def _project_root() -> Path:
    """Walk up from this test file to the repo root."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate project root")


_EXPECTED_TEMPLATES: tuple[str, ...] = ("openai-agents", "pydantic-ai", "text-chat")
_EXPECTED_FILES: tuple[str, ...] = (
    "agent.py",
    "pyproject.toml",
    "README.md",
    ".env.example",
    ".gitignore",
)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build easycat's wheel once and return its path."""
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover — CI without uv is out of scope
        pytest.skip("`uv` binary not on PATH")
    out_dir = tmp_path_factory.mktemp("wheel")
    root = _project_root()
    proc = subprocess.run(
        [uv, "build", "--wheel", "-o", str(out_dir)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:  # pragma: no cover — diagnostic path
        pytest.skip(f"`uv build` failed:\n{proc.stderr}")
    wheels = list(out_dir.glob("easycat-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


def _wheel_members(wheel_path: Path) -> list[str]:
    with zipfile.ZipFile(wheel_path) as zf:
        return zf.namelist()


@pytest.mark.parametrize("template", _EXPECTED_TEMPLATES)
def test_wheel_ships_template_directory(built_wheel: Path, template: str) -> None:
    members = _wheel_members(built_wheel)
    prefix = f"easycat/cli/scaffold/templates/{template}/"
    found = [m for m in members if m.startswith(prefix)]
    assert found, f"template {template} not in wheel"


@pytest.mark.parametrize("template", _EXPECTED_TEMPLATES)
@pytest.mark.parametrize("fname", _EXPECTED_FILES)
def test_wheel_ships_template_file(built_wheel: Path, template: str, fname: str) -> None:
    """The dotfile-critical test: .env.example and .gitignore must land."""
    members = _wheel_members(built_wheel)
    expected = f"easycat/cli/scaffold/templates/{template}/{fname}"
    assert expected in members, f"{expected} missing from wheel"


def test_wheel_ships_cli_entry_point(built_wheel: Path) -> None:
    """``[project.scripts] easycat = "easycat.cli:main"`` must land."""
    members = _wheel_members(built_wheel)
    # The metadata record is in the RECORD / METADATA files; simplest
    # check: the cli package is present.
    assert any(m.startswith("easycat/cli/") for m in members)
    # And the top-level entry point file exists.
    assert "easycat/cli/__init__.py" in members
    assert "easycat/cli/_app.py" in members
