"""Plan 17 (DX3-3) — prove a scaffolded project from the *built wheel*.

Every other test in ``tests/cli/e2e/test_scaffold_smoke.py`` scaffolds a
project and runs its tests against **this checkout's** ``easycat`` — imported
from ``src/`` via the dev venv's ``easycat.pth``. That proves the templates
render correctly, but it never proves two things the plan's acceptance
sentences ask for: that a generated project can import and test itself with
no framework SDK anywhere on the checkout's own dependency floor (the SDK
lives only in a throwaway venv this module builds), and that it does so
*outside* the source tree, installed the way a real user installs it — from a
wheel, with no ``[tool.uv.sources]`` pin back to this repo.

**No automated lane runs this file.** It is marked ``integration_external``,
which the default ``addopts`` (``pyproject.toml``), every ``just guard-*``
recipe, and ``ci.yml`` all deselect. It exists so a maintainer can reproduce
locally, in one command, what the *actual* automated proof —
``.github/workflows/ci.yml``'s ``generated-app-smoke`` job, which runs on
every push and pull request — does in CI. Treat a green run here as a local
rehearsal, never as a substitute for that job passing on the PR.

Run with: ``uv run pytest tests/cli/e2e/test_generated_project_wheel.py -m integration_external``
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._wheel_build import build_wheel
from tests.cli.e2e.test_scaffold_smoke import (
    _assert_netguard_is_loaded,
    _netguard_env,
    _pypi_reachable,
)

# The repo-wide `timeout = 60` (pyproject.toml) covers fixture setup as well as
# the call phase, and pytest-timeout's thread method aborts the whole process
# when it fires.  The module-scoped `app_venv` fixture -- a wheel build, a
# `uv venv` and a `uv pip install` of the agent SDK -- is charged to whichever
# test runs first, and on a cold uv cache that alone exceeds 60 s.  Without
# this override the maintainer's reproduction command dies with a process
# abort instead of reporting a result.
pytestmark = [pytest.mark.integration_external, pytest.mark.timeout(900)]

# Each subprocess also carries its own budget, so a wedged child reports which
# step hung instead of taking the module's whole budget with it.
_VENV_TIMEOUT_S = 600.0
_PYTEST_TIMEOUT_S = 300.0


@pytest.fixture(scope="module", autouse=True)
def _requires_uv_and_pypi() -> None:
    """Skip the whole module rather than fail it: building/installing a wheel
    from PyPI extras needs both `uv` and network access, neither of which this
    marker's callers are expected to guarantee.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv executable not on PATH")
    if not _pypi_reachable():
        pytest.skip("PyPI unreachable — offline environment")


def _venv_bin(venv_dir: Path, name: str) -> Path:
    return venv_dir / "bin" / name


@pytest.fixture(scope="module")
def app_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway venv, outside the checkout, with the wheel + agent SDK.

    Mirrors the ``ci.yml`` ``generated-app-smoke`` job: build the wheel, then
    install ``easycat[openai-agents]`` from it (never from ``src/``) plus
    ``pytest``, into a venv this repo's own dependency floor never touches.
    """
    # strict: a broken wheel build is the exact defect this module exists to
    # catch, so it must fail here rather than skip the rehearsal silently.
    wheel = build_wheel(tmp_path_factory.mktemp("wheel"), strict=True)
    venv_dir = tmp_path_factory.mktemp("venv") / "app-venv"

    proc = subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", "3.12"],
        capture_output=True,
        text=True,
        timeout=_VENV_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"uv venv failed:\n{proc.stdout}\n{proc.stderr}"

    proc = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(_venv_bin(venv_dir, "python")),
            f"easycat[openai-agents] @ file://{wheel}",
            "pytest",
        ],
        capture_output=True,
        text=True,
        timeout=_VENV_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"uv pip install failed:\n{proc.stdout}\n{proc.stderr}"
    return venv_dir


@pytest.fixture
def scaffolded_project(app_venv: Path, tmp_path: Path) -> Path:
    """Scaffold a fresh ``openai-agents`` project outside the checkout.

    A fresh ``tmp_path`` per test, not a module-scoped one: the seeded-break
    tests mutate the generated ``agent.py``/``tools.py`` in place.
    """
    project = tmp_path / "easycat-app-smoke"
    proc = subprocess.run(
        [
            str(_venv_bin(app_venv, "easycat")),
            "init",
            str(project),
            "--template",
            "openai-agents",
            "--no-git",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        timeout=_PYTEST_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"easycat init failed:\n{proc.stdout}\n{proc.stderr}"
    return project


def test_generated_project_scaffold_has_no_local_source_pin(scaffolded_project: Path) -> None:
    """Outside the checkout, ``_editable_easycat_source`` finds nothing.

    A wheel install is never PEP 610 editable, so the generated
    ``pyproject.toml`` must ship no ``[tool.uv.sources]`` pin back to this
    repo — the "not a source-tree import" half of A2.
    """
    pyproject = (scaffolded_project / "pyproject.toml").read_text(encoding="utf-8")
    assert "tool.uv.sources" not in pyproject


def test_generated_project_imports_without_starting_anything(
    app_venv: Path, scaffolded_project: Path
) -> None:
    """A1, at runtime, from the wheel: import must never start audio or a server.

    The old module-scope ``VoiceApp(...).run("local")`` shape would have
    blocked here on a microphone; the guarded ``__main__`` entry point must
    not.
    """
    env = _netguard_env()
    # Canary the interpreter that is about to do the guarded work, not this
    # process's: loading `sitecustomize` is a per-interpreter property, so a
    # canary in the dev venv would prove nothing about the wheel venv.
    _assert_netguard_is_loaded(env, _venv_bin(app_venv, "python"))

    proc = subprocess.run(
        [str(_venv_bin(app_venv, "python")), "-c", "import agent, tools"],
        cwd=scaffolded_project,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout == ""


def test_generated_project_offline_tests_pass_from_the_wheel(
    app_venv: Path, scaffolded_project: Path
) -> None:
    """A2, plus the skipped halves of A1/A4/A6, with the SDK installed.

    Two runs, deliberately: a bare ``"skipped" not in stdout`` check on the
    whole suite (the negative half — nothing may skip once the SDK is
    present), and an exact-nodeid run for the positive proof that the
    SDK-bound wiring test actually executed. ``-rs`` prints
    ``file:line: reason`` and never a test name, so a skip reports
    "1 skipped", never "1 passed" — that is the only reliable discriminator.
    """
    env = _netguard_env(OPENAI_API_KEY="sk-ambient-not-used")
    # The wheel venv's own interpreter -- the one `<app_venv>/bin/pytest` runs
    # under -- must be the one proven to load the guard (see A2's "provider
    # traffic blocked" half); canarying `sys.executable` would not.
    _assert_netguard_is_loaded(env, _venv_bin(app_venv, "python"))

    # Console script, not `python -m pytest`: it does NOT prepend the cwd, so
    # this also re-proves the generated `[tool.pytest.ini_options]
    # pythonpath = ["."]` key (PR1, design §0.2) inside a wheel-installed venv.
    proc = subprocess.run(
        [
            str(_venv_bin(app_venv, "pytest")),
            "tests",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
        ],
        cwd=scaffolded_project,
        capture_output=True,
        text=True,
        env=env,
        timeout=_PYTEST_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "skipped" not in proc.stdout, proc.stdout

    wiring_proc = subprocess.run(
        [
            str(_venv_bin(app_venv, "pytest")),
            "tests/test_agent.py::test_agent_wires_its_instructions_and_tools",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=scaffolded_project,
        capture_output=True,
        text=True,
        env=env,
        timeout=_PYTEST_TIMEOUT_S,
        check=False,
    )
    assert wiring_proc.returncode == 0, f"{wiring_proc.stdout}\n{wiring_proc.stderr}"
    assert "1 passed" in wiring_proc.stdout


# Each row: (seed name, file to edit, exact substring to replace, its
# replacement, and the generated test name the break must fail). Every
# `before` string is asserted present first, so a stale seed fails loudly
# instead of silently passing.
_SEED_EDITS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "drop_tool",
        "agent.py",
        "tools=[function_tool(current_time)]",
        "tools=[]",
        "test_agent_wires_its_instructions_and_tools",
    ),
    (
        "rename_agent",
        "agent.py",
        "Agent(name=AGENT_NAME, instructions=INSTRUCTIONS, tools=[function_tool(current_time)])",
        ('Agent(name="Renamed", instructions=INSTRUCTIONS, tools=[function_tool(current_time)])'),
        "test_agent_wires_its_instructions_and_tools",
    ),
    (
        "break_tool_format",
        "tools.py",
        'strftime("%H:%M")',
        'strftime("%H hours")',
        "test_current_time_tool_speaks_hh_mm",
    ),
    (
        "break_app_config",
        "agent.py",
        "VoiceApp(agent=make_agent())",
        'VoiceApp(agent=make_agent(), foo="bar")',
        "test_agent_wires_its_instructions_and_tools",
    ),
)


@pytest.mark.parametrize(
    ("seed", "target_file", "before", "after", "expected_failing_test"),
    _SEED_EDITS,
    ids=[row[0] for row in _SEED_EDITS],
)
def test_generated_project_tests_fail_when_the_agent_wiring_breaks(
    app_venv: Path,
    scaffolded_project: Path,
    seed: str,
    target_file: str,
    before: str,
    after: str,
    expected_failing_test: str,
) -> None:
    """A3, from the wheel: breaking a tested app/tool behavior fails its test."""
    path = scaffolded_project / target_file
    source = path.read_text(encoding="utf-8")
    assert before in source, f"stale seed {seed!r}: {before!r} is no longer in {target_file}"
    path.write_text(source.replace(before, after), encoding="utf-8")

    proc = subprocess.run(
        [str(_venv_bin(app_venv, "pytest")), "tests", "-q", "-p", "no:cacheprovider"],
        cwd=scaffolded_project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": "sk-ambient-not-used", "PYTHONPATH": ""},
        timeout=_PYTEST_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode != 0, f"seed {seed!r} did not fail the generated tests"
    assert expected_failing_test in proc.stdout, proc.stdout
