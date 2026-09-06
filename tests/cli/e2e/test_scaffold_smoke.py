"""Plan 17 — End-to-end scaffold-and-invoke.

For each template:
* Scaffold into a tmpdir via the CLI.
* Assert the generated Python files pass ``py_compile`` AND ``ruff``.
* Assert the generated ``pyproject.toml`` *resolves* with ``uv lock``
  (the pre-launch ``[tool.uv.sources]`` block points at this checkout).
* Run the generated offline test suite (``tests/``) with pytest —
  key-free, against this repo's installed ``easycat``.

A full ``uv sync`` round-trip stays out of the guard path on purpose —
it would download numpy/onnxruntime wheels on every run.  Resolution is
enough to prove the install path works; it is skipped offline.

See ``TEST_PLANS.md`` §17.
"""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app
from easycat.cli.scaffold._schema import available_templates

pytestmark = pytest.mark.integration_local

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def cli() -> CliRunner:
    _register_commands()
    return CliRunner()


def _scaffold_project(
    cli: CliRunner,
    tmp_path: Path,
    template: str,
    *,
    easycat_source: Path | None = None,
    agent_name: str = "SmokeBot",
    agent_instructions: str = "Answer smoke-test questions.",
) -> Path:
    config = json.dumps(
        {
            "schema_version": 1,
            "template": template,
            "agent_name": agent_name,
            "agent_instructions": agent_instructions,
        }
    )
    project = tmp_path / f"demo-{template}"
    args = ["init", str(project), "--config", config, "--no-git"]
    if easycat_source is not None:
        args += ["--easycat-source", str(easycat_source)]
    result = cli.invoke(app, args)
    assert result.exit_code == 0, result.stderr
    return project


def _pypi_reachable() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


def _generated_python_files(project: Path) -> list[Path]:
    files = sorted([*project.glob("*.py"), *project.glob("tests/*.py")])
    assert files, f"{project} did not generate any Python files"
    return files


# ``uv run pytest`` executes the console script, which — unlike
# ``python -m pytest`` — does NOT prepend the cwd to ``sys.path``.  There is
# deliberately no fallback: swapping in ``[sys.executable, "-m", "pytest"]``
# would silently turn the test below into a duplicate of the module-form one.
def _console_pytest() -> Path | None:
    runner = Path(sys.executable).parent / "pytest"
    return runner if runner.is_file() else None


_NETGUARD_DIR = REPO_ROOT / "tests" / "_netguard"
_NETGUARD_MARKER = "easycat test guard: outbound network blocked"


def _netguard_env(**extra: str) -> dict[str, str]:
    """Environment whose child processes cannot open a non-loopback socket."""
    return {**os.environ, "PYTHONPATH": str(_NETGUARD_DIR), **extra}


def _assert_netguard_is_loaded(env: dict[str, str]) -> None:
    """Canary: ``sitecustomize`` is silently skipped under ``-I``/``-S``.

    Without this the offline test below passes identically when nothing is
    blocked at all, i.e. it stops testing the condition it exists for.
    """
    blocked = subprocess.run(
        [
            sys.executable,
            "-c",
            # The connect timeout matters only when the guard did NOT load:
            # this must then fail fast, not stall the whole lane on a SYN to
            # a host a network-blackholing runner never answers for.
            "import socket; socket.create_connection(('example.com', 443), timeout=2)",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert blocked.returncode != 0, "the outbound-network guard did not load"
    assert _NETGUARD_MARKER in blocked.stderr, blocked.stderr

    # Second half: the guard is selective, not a blanket socket break — the
    # same environment must still allow loopback, or pytest plugins and local
    # servers would break and the run would fail for the wrong reason.
    loopback = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket\n"
                "server = socket.socket()\n"
                "server.bind(('127.0.0.1', 0))\n"
                "server.listen(1)\n"
                "socket.create_connection(server.getsockname(), timeout=2).close()\n"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert loopback.returncode == 0, loopback.stderr


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_scaffold_python_files_compile(cli: CliRunner, tmp_path: Path, template: str) -> None:
    """Rendered Python files must compile with py_compile.

    This catches placeholder-substitution bugs that leave invalid source in
    any generated entry point or support module.
    """
    project = _scaffold_project(cli, tmp_path, template)

    for py_file in _generated_python_files(project):
        py_compile.compile(str(py_file), doraise=True)


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_scaffold_python_files_pass_ruff(cli: CliRunner, tmp_path: Path, template: str) -> None:
    project = _scaffold_project(cli, tmp_path, template)
    python_files = _generated_python_files(project)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *(str(path.relative_to(project)) for path in python_files),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"ruff check failed on scaffolded {template} Python files:\n{proc.stdout}\n{proc.stderr}"
    )


@pytest.mark.parametrize("template", sorted(available_templates()))
@pytest.mark.integration_external
def test_scaffold_dependencies_resolve_with_uv_lock(
    cli: CliRunner, tmp_path: Path, template: str
) -> None:
    """Resolution-only install smoke: ``uv lock`` must succeed.

    Pre-launch, ``easycat`` 404s on PyPI; the scaffold therefore pins
    this checkout via ``[tool.uv.sources]``.  Locking proves the
    generated project's ``uv sync`` can actually resolve — without
    dragging the full wheel download into every guard run.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv executable not on PATH")
    if not _pypi_reachable():
        pytest.skip("PyPI unreachable — offline environment")

    project = _scaffold_project(cli, tmp_path, template, easycat_source=REPO_ROOT)

    proc = subprocess.run(
        ["uv", "lock", "--no-progress"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert proc.returncode == 0, (
        f"uv lock failed for scaffolded {template}:\n{proc.stdout}\n{proc.stderr}"
    )

    lock_text = (project / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "easycat"' in lock_text
    assert str(REPO_ROOT) in lock_text


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_scaffold_offline_test_suite_passes(cli: CliRunner, tmp_path: Path, template: str) -> None:
    """``pytest`` must pass inside a freshly generated project, offline.

    Runs the scaffold's whole ``tests/`` directory with this repo's
    interpreter (which has ``easycat`` + ``pytest`` installed) so the
    smoke test needs no ``uv sync`` round-trip, no API keys, and no
    network — a scripted stand-in for the model drives EasyCat's real
    turn pipelines while the project's own tools run for real.
    """
    project = _scaffold_project(cli, tmp_path, template)
    assert (project / "tests" / "test_agent.py").is_file()
    assert (project / "AGENTS.md").is_file()

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode == 0, (
        f"pytest failed inside scaffolded {template} project:\n{proc.stdout}\n{proc.stderr}"
    )


def test_scaffold_offline_tests_run_without_cwd_on_sys_path(
    cli: CliRunner, tmp_path: Path
) -> None:
    """The documented ``uv run pytest`` must work, not just ``python -m pytest``.

    The console script is the entire point of this test: it does not prepend
    the cwd to ``sys.path``, so only the generated
    ``[tool.pytest.ini_options] pythonpath = ["."]`` makes ``import agent`` /
    ``import tools`` resolve.  Swapping the runner for ``python -m pytest``
    silently voids the test.
    """
    runner = _console_pytest()
    if runner is None:
        pytest.skip("console-script pytest not available; this test exists to exercise it")

    project = _scaffold_project(cli, tmp_path, "openai-agents")

    proc = subprocess.run(
        [str(runner), "tests", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode == 0, (
        f"console-script pytest failed in the scaffolded project:\n{proc.stdout}\n{proc.stderr}"
    )


def test_scaffold_offline_tests_pass_with_hostile_agent_text(
    cli: CliRunner, tmp_path: Path
) -> None:
    """A user's own quotes, backslashes and dollar signs are legitimate text.

    ``string.Template`` never re-scans a substituted value, so ``$USD`` renders
    fine; the generated suite must therefore check the constants against the
    *placeholders*, not against a bare ``"$"``, or the user's very first
    ``uv run pytest`` fails on their own agent description.
    """
    project = _scaffold_project(
        cli,
        tmp_path,
        "openai-agents",
        agent_name='Billing "Bot" \\ $9',
        agent_instructions="Quote prices in $USD and explain fees.",
    )
    assert "$USD" in (project / "agent.py").read_text(encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode == 0, (
        f"a dollar sign in the agent text failed the generated tests:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_scaffold_offline_tests_pass_with_ambient_credentials_and_no_network(
    cli: CliRunner, tmp_path: Path
) -> None:
    """Offline half of A2: ambient credentials present, provider traffic blocked."""
    credential = "sk-ambient-credential"
    env = _netguard_env(OPENAI_API_KEY=credential, DEEPGRAM_API_KEY="dg-ambient")
    _assert_netguard_is_loaded(env)

    project = _scaffold_project(cli, tmp_path, "openai-agents")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, (
        f"generated tests need network or a key:\n{proc.stdout}\n{proc.stderr}"
    )
    assert credential not in proc.stdout
    assert credential not in proc.stderr


def test_scaffold_offline_tests_fail_when_tool_behavior_breaks(
    cli: CliRunner, tmp_path: Path
) -> None:
    """A3: breaking a tested tool behaviour must fail the generated tests."""
    project = _scaffold_project(cli, tmp_path, "openai-agents")
    tools = project / "tools.py"
    source = tools.read_text(encoding="utf-8")
    seed = 'strftime("%H:%M")'
    assert seed in source, f"stale seed: {seed} is no longer in the generated tools.py"
    tools.write_text(source.replace(seed, 'strftime("%H hours")'), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode != 0, "a broken tool did not fail the generated tests"
    assert "test_current_time_tool_speaks_hh_mm" in proc.stdout
    assert "test_two_turns_share_one_session" in proc.stdout


def test_scaffold_app_wiring_tests_skip_cleanly_without_the_agent_sdk(
    cli: CliRunner, tmp_path: Path
) -> None:
    """Without ``agents`` installed the wiring test skips — it never errors.

    Guards the collection path: a contributor must not "fix" the skip by
    breaking the import, and the reason a user sees must stay actionable.
    """
    if importlib.util.find_spec("agents") is not None:
        pytest.skip("the agent SDK is installed; this test describes a bare environment")

    project = _scaffold_project(cli, tmp_path, "openai-agents")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # ``-rs`` prints ``file:line: reason`` and never a test name, so the
    # reason string is the only anchor available in a skip report.
    assert "1 skipped" in proc.stdout
    assert "run `uv sync` to install the agent SDK" in proc.stdout


def test_provider_scaffold_named_vad_conformance_suite_passes(
    cli: CliRunner, tmp_path: Path
) -> None:
    """The provider on-ramp must execute its named-registration example."""
    project = _scaffold_project(cli, tmp_path, "provider")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_custom_vad.py",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode == 0, (
        f"pytest failed inside scaffolded provider project:\n{proc.stdout}\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    ("template", "contract_file"),
    [
        ("provider-stt", "test_custom_stt.py"),
        ("provider-tts", "test_custom_tts.py"),
    ],
)
def test_speech_provider_scaffold_contract_suite_passes(
    cli: CliRunner,
    tmp_path: Path,
    template: str,
    contract_file: str,
) -> None:
    """Each speech-provider on-ramp executes offline after generation."""
    project = _scaffold_project(cli, tmp_path, template)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            contract_file,
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
        check=False,
    )
    assert proc.returncode == 0, (
        f"pytest failed inside scaffolded {template} project:\n{proc.stdout}\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    ("template", "contract_file"),
    [
        ("provider-stt", "test_custom_stt.py"),
        ("provider-tts", "test_custom_tts.py"),
    ],
)
def test_speech_provider_bare_pytest_excludes_live_tests_with_ambient_credentials(
    cli: CliRunner,
    tmp_path: Path,
    template: str,
    contract_file: str,
) -> None:
    """Generated pytest defaults must keep credentialed tests explicitly opt-in."""
    project = _scaffold_project(cli, tmp_path, template)
    live_sentinel = project / "test_live_selection_guard.py"
    live_sentinel.write_text(
        """import pytest


@pytest.mark.integration_live
def test_live_requests_are_never_implicit() -> None:
    raise AssertionError("bare pytest selected an integration_live test")
""",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            contract_file,
            live_sentinel.name,
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "OPENAI_API_KEY": "sk-ambient-provider-credential",
            "DEEPGRAM_API_KEY": "dg-ambient-provider-credential",
            "ELEVENLABS_API_KEY": "el-ambient-provider-credential",
        },
        check=False,
    )

    assert proc.returncode == 0, (
        f"bare pytest selected a live test in {template}:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "1 deselected" in proc.stdout
