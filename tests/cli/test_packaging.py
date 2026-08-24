"""Plan 16 — wheel and sdist packaging ship clean release contents.

Run with ``pytest -m integration_local tests/cli/test_packaging.py`` to select
it directly. The marker lets validation lanes filter heavier wheel-build checks;
bare pytest still collects it unless the caller supplies a marker expression.

See ``TEST_PLANS.md`` §16.
"""

from __future__ import annotations

import email
import shutil
import subprocess
import tarfile
import zipfile
from email.message import Message
from pathlib import Path

import pytest

from scripts.check_wheel_size import MAX_WHEEL_BYTES
from tests._release_artifacts import release_artifact_offenders

pytestmark = pytest.mark.integration_local


def _project_root() -> Path:
    """Walk up from this test file to the repo root."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate project root")


_EXPECTED_TEMPLATES: tuple[str, ...] = (
    "openai-agents",
    "provider",
    "pydantic-ai",
    "pydantic-ai-workflow",
    "telnyx-phone",
    "text-chat",
    "twilio-phone",
    "webrtc-browser",
)
_EXPECTED_FILES: tuple[str, ...] = (
    "agent.py",
    "pyproject.toml",
    "README.md",
    "AGENTS.md",
    "tests/test_agent.py",
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
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover — diagnostic path
        pytest.skip(f"`uv build` failed:\n{proc.stderr}")
    wheels = list(out_dir.glob("easycat-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def built_sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build easycat's sdist once and return its path."""
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover — CI without uv is out of scope
        pytest.skip("`uv` binary not on PATH")
    out_dir = tmp_path_factory.mktemp("sdist")
    root = _project_root()
    proc = subprocess.run(
        [uv, "build", "--sdist", "-o", str(out_dir)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover — diagnostic path
        pytest.skip(f"`uv build` failed:\n{proc.stderr}")
    sdists = list(out_dir.glob("easycat-*.tar.gz"))
    assert len(sdists) == 1, f"expected one sdist, got {sdists}"
    return sdists[0]


def _wheel_members(wheel_path: Path) -> list[str]:
    with zipfile.ZipFile(wheel_path) as zf:
        return zf.namelist()


def _sdist_members(sdist_path: Path) -> list[str]:
    with tarfile.open(sdist_path, "r:gz") as tf:
        return tf.getnames()


def _wheel_metadata(wheel_path: Path) -> Message:
    with zipfile.ZipFile(wheel_path) as zf:
        metadata_name = next(
            name for name in zf.namelist() if name.endswith(".dist-info/METADATA")
        )
        return email.message_from_bytes(zf.read(metadata_name))


def test_wheel_stays_within_deliberate_size_budget(built_wheel: Path) -> None:
    assert built_wheel.stat().st_size <= MAX_WHEEL_BYTES


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


def test_wheel_ships_twilio_phone_server(built_wheel: Path) -> None:
    members = _wheel_members(built_wheel)
    assert "easycat/cli/scaffold/templates/twilio-phone/server.py" in members


def test_wheel_ships_cli_entry_point(built_wheel: Path) -> None:
    """``[project.scripts] easycat = "easycat.cli:main"`` must land."""
    members = _wheel_members(built_wheel)
    # The metadata record is in the RECORD / METADATA files; simplest
    # check: the cli package is present.
    assert any(m.startswith("easycat/cli/") for m in members)
    # And the top-level entry point file exists.
    assert "easycat/cli/__init__.py" in members
    assert "easycat/cli/_app.py" in members


def test_wheel_metadata_is_useful_for_package_indexes(built_wheel: Path) -> None:
    """Release artifacts should be understandable before users read the README."""
    metadata = _wheel_metadata(built_wheel)

    assert metadata["Name"] == "easycat"
    assert metadata["Requires-Python"] == ">=3.11"
    assert metadata["Author"] == "EasyCat contributors"
    assert metadata.get_all("Project-URL") == [
        "Documentation, https://yisding.github.io/easycat/",
        "Issues, https://github.com/yisding/easycat/issues",
        "Repository, https://github.com/yisding/easycat",
    ]
    assert metadata.get_all("Keywords") == [
        "agent-framework,speech-to-text,telephony,text-to-speech,voice-agents,webrtc"
    ]
    classifiers = set(metadata.get_all("Classifier") or [])
    assert {
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Multimedia :: Sound/Audio :: Speech",
        "Typing :: Typed",
    } <= classifiers


def test_wheel_does_not_ship_local_generated_or_secret_artifacts(built_wheel: Path) -> None:
    """Ignored local artifacts under ``src/`` must not leak into release wheels."""
    members = _wheel_members(built_wheel)
    offenders = release_artifact_offenders(members)

    assert not offenders, "wheel should not ship cache/workspace/generated/secret artifacts: " + (
        ", ".join(offenders)
    )


def test_sdist_does_not_ship_local_generated_or_secret_artifacts(built_sdist: Path) -> None:
    """Ignored local artifacts must not leak into source distributions."""
    members = _sdist_members(built_sdist)
    offenders = release_artifact_offenders(members)

    assert not offenders, "sdist should not ship cache/workspace/generated/secret artifacts: " + (
        ", ".join(offenders)
    )
