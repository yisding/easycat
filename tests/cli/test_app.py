"""Top-level CLI surface: --version, --help, journey menu.

Also guards the ``--version`` fast path in ``easycat/cli/__init__.py``
that short-circuits before importing Typer/Rich.
"""

from __future__ import annotations

import json
import subprocess
import sys

from typer.testing import CliRunner

from easycat.cli._app import app


def test_version(cli: CliRunner) -> None:
    result = cli.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "easycat" in result.stdout


def test_short_version_flag(cli: CliRunner) -> None:
    result = cli.invoke(app, ["-V"])
    assert result.exit_code == 0


def test_help_renders(cli: CliRunner) -> None:
    result = cli.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "EasyCat" in result.stdout
    assert "init" in result.stdout
    assert "doctor" in result.stdout
    assert "docs" in result.stdout
    assert "explain" in result.stdout
    assert "inspect" in result.stdout


def test_journey_menu(cli: CliRunner) -> None:
    """Bare ``easycat`` prints the journey menu listing implemented commands."""
    result = cli.invoke(app, [])
    assert result.exit_code == 0
    assert "Scaffold" in result.stdout
    assert "Debug with the journal" in result.stdout
    assert "Learn" in result.stdout
    assert "Show documentation entry points" in result.stdout
    assert "List captured debug bundles and crash dumps" in result.stdout
    assert "Summarise a debug bundle or SQLite journal" in result.stdout
    for cmd in ("init", "doctor", "docs", "explain", "bundles", "inspect", "replay"):
        assert cmd in result.stdout
    # Don't advertise unshipped commands until they're implemented.
    assert "demo" not in result.stdout


def test_docs_command(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs"])
    assert result.exit_code == 0
    assert "EasyCat documentation" in result.stdout
    assert "README.md#cli" in result.stdout
    assert "docs/README.md" in result.stdout
    assert "docs/teaching" in result.stdout
    assert "examples/README.md" in result.stdout
    assert "docs/public-api.md" in result.stdout
    assert "CONTRIBUTING.md" in result.stdout
    assert "docs/deployment/docker.md" in result.stdout
    assert "docs/observability.md" in result.stdout
    assert "#validation-workflow" in result.stdout
    assert "plan/validation/reference.md" in result.stdout
    assert "https://github.com/yisding/easycat/blob/main/docs/README.md" in result.stdout
    assert "https://github.com/yisding/easycat/tree/main/docs/teaching" in result.stdout


def test_docs_command_json(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "docs"
    assert payload["status"] == "ok"
    paths = {entry["path"] for entry in payload["entries"]}
    assert "README.md#cli" in paths
    assert "docs/README.md" in paths
    assert "docs/teaching/" in paths
    assert "examples/README.md" in paths
    assert "CONTRIBUTING.md" in paths
    assert "docs/deployment/docker.md" in paths
    assert "docs/observability.md" in paths
    assert "plan/validation/reference.md" in paths
    assert all(entry.get("description") for entry in payload["entries"])
    assert all(entry.get("url") for entry in payload["entries"])
    descriptions = {entry["path"]: entry["description"] for entry in payload["entries"]}
    assert "maintained guide" in descriptions["docs/README.md"]
    assert "runnable local" in descriptions["examples/README.md"]
    urls = {entry["path"]: entry["url"] for entry in payload["entries"]}
    assert urls["README.md#cli"] == "https://github.com/yisding/easycat/blob/main/README.md#cli"
    assert urls["README.md#install"] == (
        "https://github.com/yisding/easycat/blob/main/README.md#install"
    )
    assert urls["docs/teaching/"] == "https://github.com/yisding/easycat/tree/main/docs/teaching"
    assert payload["source_url"] == "https://github.com/yisding/easycat"


# ── Fast-path guard ──────────────────────────────────────────────


def test_python_m_easycat_delegates_to_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "easycat", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("easycat ")


def test_version_fast_path_skips_typer_and_rich() -> None:
    """The ``easycat --version`` fast path must not import Typer or Rich.

    This test runs the CLI in a subprocess (so module caches are cold)
    and asserts that after the entry point completes, ``typer`` and
    ``rich`` were never imported.  A regression here means the ~300ms
    Typer/Rich import cost crept back into the critical path.
    """
    script = (
        "import sys\n"
        "sys.argv = ['easycat', '--version']\n"
        "from easycat.cli import main\n"
        "main()\n"
        "print('typer:', 'typer' in sys.modules)\n"
        "print('rich:', 'rich' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "easycat" in proc.stdout
    assert "typer: False" in proc.stdout, (
        "`easycat --version` is importing Typer — the fast path regressed.\n"
        f"stdout:\n{proc.stdout}"
    )
    assert "rich: False" in proc.stdout, (
        f"`easycat --version` is importing Rich — the fast path regressed.\nstdout:\n{proc.stdout}"
    )


def test_version_fast_path_matches_typer_path() -> None:
    """Fast-path output must exactly match the Typer-path output.

    If someone changes the Typer ``--version`` callback without
    updating the fast path (or vice versa), users see inconsistent
    output depending on whether they pass ``--version`` alone or as
    part of a larger invocation.
    """
    fast = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['easycat', '--version']; "
            "from easycat.cli import main; main()",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    typer_path = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['easycat', '--version']; "
            "from easycat.cli._app import main; main()",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert fast.stdout.strip() == typer_path.stdout.strip()
