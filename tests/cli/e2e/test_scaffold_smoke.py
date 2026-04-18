"""Plan 15 — End-to-end scaffold-and-invoke.

For each template:
* Scaffold into a tmpdir via the CLI.
* Assert the generated ``agent.py`` passes ``py_compile`` AND ``ruff``.

Full ``uv sync`` round-trip is intentionally skipped (requires the
template-pinned ``easycat`` version on PyPI — see TEST_PLANS.md §15).

See ``TEST_PLANS.md`` §15.
"""

from __future__ import annotations

import json
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


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_scaffold_agent_py_compiles(cli: CliRunner, tmp_path: Path, template: str) -> None:
    """The rendered agent.py must compile with py_compile (catches
    placeholder-substitution bugs that leave ``$AGENT_NAME`` literals
    inside the source).
    """
    config = json.dumps(
        {
            "schema_version": 1,
            "template": template,
            "agent_name": "SmokeBot",
            "agent_instructions": "Answer smoke-test questions.",
        }
    )
    project = tmp_path / f"demo-{template}"
    result = cli.invoke(
        app,
        ["init", str(project), "--config", config, "--no-git"],
    )
    assert result.exit_code == 0, result.stderr
    agent_py = project / "agent.py"
    py_compile.compile(str(agent_py), doraise=True)


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_scaffold_agent_py_passes_ruff(cli: CliRunner, tmp_path: Path, template: str) -> None:
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
    agent_py = project / "agent.py"
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(agent_py)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"ruff check failed on scaffolded {template}/agent.py:\n{proc.stdout}\n{proc.stderr}"
    )
