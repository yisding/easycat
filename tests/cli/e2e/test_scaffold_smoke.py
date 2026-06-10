"""Plan 17 — End-to-end scaffold-and-invoke.

For each template:
* Scaffold into a tmpdir via the CLI.
* Assert the generated Python files pass ``py_compile`` AND ``ruff``.
* Run the generated offline test suite (``tests/test_agent.py``) with
  pytest — key-free, against this repo's installed ``easycat``.

Full ``uv sync`` round-trip is intentionally skipped (requires the
template-pinned ``easycat`` version on PyPI — see TEST_PLANS.md §17).

See ``TEST_PLANS.md`` §17.
"""

from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app
from easycat.cli.scaffold._schema import available_templates

pytestmark = pytest.mark.integration_local


@pytest.fixture
def cli() -> CliRunner:
    _register_commands()
    return CliRunner()


def _scaffold_project(cli: CliRunner, tmp_path: Path, template: str) -> Path:
    config = json.dumps(
        {
            "schema_version": 1,
            "template": template,
            "agent_name": "SmokeBot",
            "agent_instructions": "Answer smoke-test questions.",
        }
    )
    project = tmp_path / f"demo-{template}"
    result = cli.invoke(app, ["init", str(project), "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    return project


def _generated_python_files(project: Path) -> list[Path]:
    files = sorted([*project.glob("*.py"), *project.glob("tests/*.py")])
    assert files, f"{project} did not generate any Python files"
    return files


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
    )
    assert proc.returncode == 0, (
        f"ruff check failed on scaffolded {template} Python files:\n{proc.stdout}\n{proc.stderr}"
    )


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_scaffold_offline_test_suite_passes(cli: CliRunner, tmp_path: Path, template: str) -> None:
    """``pytest`` must pass inside a freshly generated project, offline.

    Runs the scaffold's ``tests/test_agent.py`` with this repo's
    interpreter (which has ``easycat`` + ``pytest`` installed) so the
    smoke test needs no ``uv sync`` round-trip, no API keys, and no
    network — the stub agent drives EasyCat's real text-turn pipeline.
    """
    project = _scaffold_project(cli, tmp_path, template)
    assert (project / "tests" / "test_agent.py").is_file()
    assert (project / "AGENTS.md").is_file()

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_agent.py", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": ""},
    )
    assert proc.returncode == 0, (
        f"pytest failed inside scaffolded {template} project:\n{proc.stdout}\n{proc.stderr}"
    )
