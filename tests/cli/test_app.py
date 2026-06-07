"""Top-level CLI surface: --version, --help, journey menu.

Also guards the ``--version`` fast path in ``easycat/cli/__init__.py``
that short-circuits before importing Typer/Rich.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path

from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner

import easycat.cli._app as cli_app
from easycat.cli._app import (
    _CLI_HINTS,
    _COMMAND_TEXT,
    _DOCS_COMMAND_NOTE,
    _DOCS_LINKS,
    _JOURNEY_SECTIONS,
    _docs_entries,
    _format_docs_entry,
    _register_commands,
    app,
)
from easycat.cli.validate import validate_app
from tests._markdown import github_markdown_heading_anchors

REPO_ROOT = Path(__file__).resolve().parents[2]


def _render_rich_markup(markup: str) -> str:
    stream = StringIO()
    Console(file=stream, force_terminal=False, no_color=True, width=120).print(markup)
    return stream.getvalue()


def _registered_top_level_command_names() -> set[str]:
    command_names = {command.name for command in app.registered_commands}
    command_names.update(group.name for group in app.registered_groups)
    command_names.discard(None)
    return command_names


def _registered_top_level_command_help() -> dict[str, str]:
    help_by_name = {
        command.name: command.help
        for command in app.registered_commands
        if command.name is not None
    }
    help_by_name.update(
        {group.name: group.help for group in app.registered_groups if group.name is not None}
    )
    return help_by_name


def _journey_menu_command_names(output: str) -> set[str]:
    return set(re.findall(r"\[green\]([a-z][a-z0-9-]*)\[/\]", output))


def test_version(cli: CliRunner) -> None:
    result = cli.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "easycat" in result.stdout


def test_short_version_flag(cli: CliRunner) -> None:
    result = cli.invoke(app, ["-V"])
    assert result.exit_code == 0


def test_help_renders(cli: CliRunner) -> None:
    result = cli.invoke(app, ["--help"])
    normalized = re.sub(r"\s+", " ", result.stdout)

    assert result.exit_code == 0
    assert "EasyCat" in result.stdout
    assert "Run easycat docs for learning, maintenance, validation, and operations routes" in (
        normalized
    )
    assert "Run easycat docs --json for machine-readable docs routes" in normalized
    assert "Run easycat explain json-schema for CLI JSON" in normalized
    assert "Check API keys, optional extras, and provider reachability" in result.stdout
    assert "Run validation checks and inspect validation reports" in result.stdout
    assert "Show docs for learning, maintenance, validation, and operations" in result.stdout
    assert "Look up errors and CLI schema topics" in result.stdout
    missing = sorted(
        command_name
        for command_name in _registered_top_level_command_names()
        if command_name not in result.stdout
    )
    assert not missing, "Help output missing registered commands: " + ", ".join(missing)


def test_registered_help_uses_command_text_table() -> None:
    _register_commands()

    expected_help = {name: text.help for name, text in _COMMAND_TEXT.items()}
    assert _registered_top_level_command_help() == expected_help


def test_journey_sections_cover_command_text_table_once() -> None:
    command_names = [
        command_name
        for _, section_command_names in _JOURNEY_SECTIONS
        for command_name in section_command_names
    ]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for command_name in command_names:
        if command_name in seen:
            duplicates.add(command_name)
        seen.add(command_name)

    assert not duplicates, "Journey menu has duplicate commands: " + ", ".join(sorted(duplicates))
    assert set(command_names) == set(_COMMAND_TEXT)


def test_cli_app_docstring_tracks_journey_sections() -> None:
    docstring = cli_app.__doc__ or ""

    for section, _ in _JOURNEY_SECTIONS:
        assert section in docstring
    assert "Scaffold* and *Debug with the journal*" not in docstring


def test_peripheral_cli_plan_tracks_journey_menu() -> None:
    plan = (REPO_ROOT / "plan/peripherals/peripheral-cli.md").read_text()
    help_architecture = plan.split("## Help Architecture", 1)[1].split(
        "## Error UX Integration", 1
    )[0]

    for section, command_names in _JOURNEY_SECTIONS:
        assert section in help_architecture
        for command_name in command_names:
            assert re.search(rf"^\s+{re.escape(command_name)}\s+", help_architecture, re.M)
            assert _COMMAND_TEXT[command_name].journey in help_architecture
    for command, purpose in _CLI_HINTS:
        assert f"Run `{command}` for {purpose}" in help_architecture

    assert "Check environment and provider reachability" not in help_architecture
    assert "Look up an error code (like `cargo --explain`)" not in help_architecture
    assert "List, inspect, and export RunBundles" not in help_architecture
    assert "Replay a RunBundle against current code" not in help_architecture


def test_validation_tasks_current_state_tracks_cli_surface() -> None:
    _register_commands()
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text()
    current_state = plan.split("### V1.1 Move Validation Into CLI", 1)[1].split("Files:", 1)[0]
    validate_commands = set(get_command(validate_app).commands)

    for command_name in sorted(_registered_top_level_command_names()):
        assert f"`{command_name}`" in current_state
    for section, _ in _JOURNEY_SECTIONS:
        assert f"`{section}`" in current_state
    for command_name in sorted(validate_commands):
        assert f"`{command_name}`" in current_state

    assert "has no validation section" not in current_state


def test_peripheral_cli_plan_tracks_current_test_contract() -> None:
    plan = (REPO_ROOT / "plan/peripherals/peripheral-cli.md").read_text()
    testing = plan.split("## CLI-Specific Testing", 1)[1].split(
        "## `uvx` Zero-Install Guarantee", 1
    )[0]
    normalized = re.sub(r"\s+", " ", testing)

    assert "pytest --update-snapshots" not in testing
    assert "Golden-file tests" not in testing
    assert "`uv sync`, import the scaffolded `agent.py`" not in testing
    assert "--fail-on-regression" not in testing
    assert "Top-level help, bare journey menu, docs routes, JSON envelopes" in normalized
    assert "generated top-level Python compiles, passes ruff" in normalized
    assert "Full `uv sync` and runtime invocation stay gated" in normalized
    assert "The e2e layer currently covers scaffold smoke only" in testing
    assert "Fixture replay smoke" not in testing


def test_journey_menu(cli: CliRunner) -> None:
    """Bare ``easycat`` prints the journey menu listing implemented commands."""
    result = cli.invoke(app, [])
    normalized = re.sub(r"\s+", " ", result.stdout)
    assert result.exit_code == 0
    assert "Scaffold" in result.stdout
    assert "Debug with the journal" in result.stdout
    assert "Docs and guidance" in result.stdout
    assert "Check API keys, optional extras, and provider reachability" in result.stdout
    assert "Check API keys, extras, and provider reachability" not in result.stdout
    assert "Check environment and provider reachability" not in result.stdout
    assert "Show docs for learning, maintenance, validation, and operations" in result.stdout
    assert "Show documentation entry points" not in result.stdout
    assert "Look up errors and CLI schema topics" in result.stdout
    assert "cargo --explain" not in result.stdout
    assert "List captured debug bundles and crash dumps" in result.stdout
    assert "Summarise a debug bundle or SQLite journal" in result.stdout
    assert "Run validation checks and inspect validation reports" in result.stdout
    assert "Run validation checks and inspect reports" not in result.stdout
    assert "easycat docs" in result.stdout
    assert "easycat docs --json" in result.stdout
    assert "learning, maintenance, validation, and operations routes" in result.stdout
    assert "machine-readable docs routes, audiences, and command hints" in normalized
    assert "easycat explain json-schema" in result.stdout
    missing = sorted(
        command_name
        for command_name in _registered_top_level_command_names()
        if command_name not in result.stdout
    )
    assert not missing, "Journey menu missing registered commands: " + ", ".join(missing)
    stale = sorted(
        _journey_menu_command_names(result.stdout) - _registered_top_level_command_names()
    )
    assert not stale, "Journey menu advertises unregistered commands: " + ", ".join(stale)


def test_docs_command(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs"])
    normalized = re.sub(r"\s+", " ", result.stdout)
    assert result.exit_code == 0
    assert "EasyCat documentation" in result.stdout
    assert "For: all readers" in result.stdout
    assert "For: new users" in result.stdout
    assert "For: app builders" in result.stdout
    assert "README.md#cli" in result.stdout
    assert "base package requirements" in result.stdout
    assert "extras" in result.stdout
    assert "env requirements" in result.stdout
    assert "optional env knobs" in result.stdout
    assert "generated files" in result.stdout
    assert "copyable create/preflight/check/docs/run commands" in result.stdout
    assert "learn CLI JSON envelopes" in result.stdout
    assert "Commands:" in result.stdout
    assert "README.md#choose-your-path" in result.stdout
    assert "right first route" in result.stdout
    assert "uv run python examples/openai_agents_voice.py" in result.stdout
    assert "easycat init --list-templates" in result.stdout
    assert "easycat init --list-templates --json" in result.stdout
    assert "easycat doctor --json" in result.stdout
    assert "easycat doctor --env-file .env --json" in result.stdout
    assert "docker compose -f docker/compose.yaml up --build" in result.stdout
    assert "docker compose --env-file docker/.env -f docker/compose.yaml up --build" in (
        result.stdout
    )
    assert "easycat validate report .easycat/validation/latest.json" in result.stdout
    assert "docs/README.md" in result.stdout
    assert "docs/teaching" in result.stdout
    assert "docs/teaching/00-hello-audio" in result.stdout
    assert "examples/README.md" in result.stdout
    assert "CLAUDE.md" in result.stdout
    assert "provider registries" in result.stdout
    assert "AGENTS.md" in result.stdout
    assert "PR expectations" in result.stdout
    assert "just guard-templates" in result.stdout
    assert "docs/public-api.md" in result.stdout
    assert "CONTRIBUTING.md" in result.stdout
    assert "docs/onboarding guards" in result.stdout
    assert "just guard-docs" in result.stdout
    assert "just guard-contributing" in result.stdout
    assert "docs/deployment/docker.md" in result.stdout
    assert "docs/observability.md" in result.stdout
    assert "src/easycat/runtime/DURABILITY.md" in result.stdout
    assert "#validation-workflow" in result.stdout
    assert ".easycat/validation/latest.json" in result.stdout
    assert "plan/validation/reference.md" in result.stdout
    assert "easycat validate report .easycat/validation/latest.json --json" in result.stdout
    assert "https://github.com/yisding/easycat/blob/main/docs/README.md" in result.stdout
    assert "https://github.com/yisding/easycat/tree/main/docs/teaching" in result.stdout
    assert (
        "https://github.com/yisding/easycat/blob/main/src/easycat/runtime/DURABILITY.md"
        in result.stdout
    )
    assert "Machine-readable routes, audiences, and command hints: easycat docs --json" in (
        result.stdout
    )
    assert "Available audiences: all readers, app builders, coding agents, contributors" in (
        normalized
    )
    assert "Multi-word audiences also accept hyphens or underscores" in normalized
    assert _DOCS_COMMAND_NOTE in result.stdout
    assert "DURABILITY.\nmd" not in result.stdout


def test_docs_command_renders_every_route_entry(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs"])
    normalized_stdout = re.sub(r"\s+", " ", result.stdout)
    missing: list[str] = []

    assert result.exit_code == 0

    for entry in _docs_entries():
        for field, expected in (
            ("label", entry["label"]),
            ("path", entry["path"]),
            ("audience", f"For: {entry['audience']}"),
            ("url", entry["url"]),
        ):
            if expected not in result.stdout:
                missing.append(f"{entry['label']}: {field} {expected!r}")

        normalized_description = re.sub(r"\s+", " ", entry["description"])
        if normalized_description not in normalized_stdout:
            missing.append(f"{entry['label']}: description {entry['description']!r}")

        for command in entry.get("commands", ()):
            if command not in result.stdout:
                missing.append(f"{entry['label']}: command {command!r}")

    assert not missing, "easycat docs output missing route fields:\n" + "\n".join(missing)


def test_docs_entry_renders_bracketed_text_literally() -> None:
    entry = {
        "label": "SDK[beta]",
        "path": "docs/[beta].md",
        "audience": "Builders[dev]",
        "description": "Install optional extra easycat[openai-agents].",
        "commands": ("uv add 'easycat[openai-agents]'",),
        "url": "https://example.test/docs/[beta].md",
    }

    rendered = _render_rich_markup(_format_docs_entry(entry, label_width=len(entry["label"])))

    assert "SDK[beta]" in rendered
    assert "docs/[beta].md" in rendered
    assert "Builders[dev]" in rendered
    assert "easycat[openai-agents]" in rendered
    assert "uv add 'easycat[openai-agents]'" in rendered
    assert "https://example.test/docs/[beta].md" in rendered


def test_docs_help_names_primary_routes(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--help"])
    help_text = re.sub(r"\W+", " ", result.stdout)

    assert result.exit_code == 0
    assert "Show docs for learning, maintenance, validation, and operations" in result.stdout
    assert "--json" in result.stdout
    assert "--audience" in result.stdout
    assert "learners app builders coding agents contributors operators or maintainers" in (
        help_text
    )
    assert "Multi word labels also accept hyphens or underscores" in help_text
    assert "machine-readable docs route map" in result.stdout
    assert "audiences and command hints" in help_text
    assert "command hints" in help_text


def test_docs_command_json(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "docs"
    assert payload["status"] == "ok"
    assert payload["command_note"] == _DOCS_COMMAND_NOTE
    assert payload["audience_filter"] is None
    assert "learners" in payload["available_audiences"]
    assert "operators" in payload["available_audiences"]
    assert "maintainers" in payload["available_audiences"]
    assert [entry["label"] for entry in payload["entries"]] == [
        entry["label"] for entry in _DOCS_LINKS
    ]
    assert [entry["path"] for entry in payload["entries"]] == [
        entry["path"] for entry in _DOCS_LINKS
    ]
    paths = {entry["path"] for entry in payload["entries"]}
    descriptions = {entry["path"]: entry["description"] for entry in payload["entries"]}
    audiences = {entry["path"]: entry["audience"] for entry in payload["entries"]}
    assert "README.md#choose-your-path" in paths
    assert "README.md#cli" in paths
    assert "docs/README.md" in paths
    assert "docs/teaching/" in paths
    assert "docs/teaching/00-hello-audio/" in paths
    assert "examples/README.md" in paths
    assert "CLAUDE.md" in paths
    assert "AGENTS.md" in paths
    assert "tests/contracts/README.md" in paths
    assert "CONTRIBUTING.md" in paths
    assert "docs/deployment/docker.md" in paths
    assert "docs/observability.md" in paths
    assert "src/easycat/runtime/DURABILITY.md" in paths
    assert ".easycat/validation/latest.json" in descriptions["README.md#validation-workflow"]
    assert "plan/validation/reference.md" in paths
    assert all(entry.get("description") for entry in payload["entries"])
    assert all(entry.get("audience") for entry in payload["entries"])
    assert all(entry.get("url") for entry in payload["entries"])
    commands = {entry["path"]: entry.get("commands", []) for entry in payload["entries"]}
    assert commands["docs/README.md"] == [
        "easycat docs",
        "easycat docs --audience learners",
        "easycat docs --json",
        "easycat docs --audience maintainers --json",
    ]
    assert commands["README.md#choose-your-path"] == [
        "uv sync --extra quickstart --group dev",
        "uv run easycat doctor",
        "easycat init --list-templates",
        "uv run easycat validate quick",
        "uv run pytest tests/test_install_guidance.py",
    ]
    assert commands["README.md#cli"] == [
        "easycat init --list-templates",
        "easycat init --list-templates --json",
        "easycat init my-agent",
        "easycat doctor --json",
        "easycat doctor --env-file .env --json",
        "easycat explain json-schema",
    ]
    assert commands["README.md#validation-workflow"] == [
        "just guard-docs",
        "just guard-examples",
        "just guard-templates",
        "just guard-contributing",
        "just guard-markdown",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate contracts --json",
        "uv run easycat validate release --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ]
    assert commands["docs/public-api.md"] == [
        "uv run easycat docs",
        "uv run easycat docs --json",
        "uv run easycat explain json-schema",
        "uv run pytest tests/test_public_api.py",
        "just guard-docs",
    ]
    assert commands["tests/contracts/README.md"] == [
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "uv run pytest tests/contracts",
        "uv run pytest tests/integration/test_provider_contract_matrix.py",
    ]
    assert commands["CONTRIBUTING.md"] == [
        "just guard-docs",
        "just guard-examples",
        "just guard-templates",
        "just guard-contributing",
        "just guard-markdown",
        "uv run easycat docs --audience contributors",
        "uv run pytest",
        "uv run ruff check .",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate contracts --json",
        "uv run easycat validate release --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ]
    assert commands["src/easycat/runtime/DURABILITY.md"] == [
        "uv run pytest tests/runtime/test_sqlite_journal.py",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite --json",
        "uv run easycat inspect .easycat/crash-dumps/<session_id>.sqlite --json",
    ]
    descriptions = {entry["path"]: entry["description"] for entry in payload["entries"]}
    assert "JSON envelopes" in descriptions["README.md#cli"]
    assert "base package requirements" in descriptions["README.md#cli"]
    assert "extras" in descriptions["README.md#cli"]
    assert "env requirements" in descriptions["README.md#cli"]
    assert "optional env knobs" in descriptions["README.md#cli"]
    assert "generated files" in descriptions["README.md#cli"]
    assert audiences["README.md#choose-your-path"] == "all readers"
    assert audiences["README.md#install"] == "new users"
    assert audiences["README.md#cli"] == "app builders"
    assert audiences["CLAUDE.md"] == "maintainers"
    assert audiences["AGENTS.md"] == "coding agents"
    assert audiences["docs/observability.md"] == "operators"
    assert "copyable create/preflight/check/docs/run commands" in descriptions["README.md#cli"]
    assert "right first route" in descriptions["README.md#choose-your-path"]
    assert "maintenance" in descriptions["README.md#choose-your-path"]
    assert "docs/onboarding guards" in descriptions["README.md#validation-workflow"]
    assert "provider registries" in descriptions["CLAUDE.md"]
    assert "development commands" in descriptions["AGENTS.md"]
    assert "docs/onboarding guards" in descriptions["AGENTS.md"]
    assert "protocol" in descriptions["tests/contracts/README.md"]
    assert "docs/onboarding guards" in descriptions["CONTRIBUTING.md"]
    assert "maintained guide" in descriptions["docs/README.md"]
    assert "runnable local" in descriptions["examples/README.md"]
    assert "debugger UI" in descriptions["docs/observability.md"]
    assert "storage layout" in descriptions["src/easycat/runtime/DURABILITY.md"]
    urls = {entry["path"]: entry["url"] for entry in payload["entries"]}
    assert urls["README.md#choose-your-path"] == (
        "https://github.com/yisding/easycat/blob/main/README.md#choose-your-path"
    )
    assert urls["README.md#cli"] == "https://github.com/yisding/easycat/blob/main/README.md#cli"
    assert urls["README.md#install"] == (
        "https://github.com/yisding/easycat/blob/main/README.md#install"
    )
    assert urls["docs/teaching/"] == "https://github.com/yisding/easycat/tree/main/docs/teaching"
    assert urls["docs/teaching/00-hello-audio/"] == (
        "https://github.com/yisding/easycat/tree/main/docs/teaching/00-hello-audio"
    )
    assert payload["source_url"] == "https://github.com/yisding/easycat"


def test_docs_command_filters_human_routes_by_audience(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--audience", "operators"])
    normalized = re.sub(r"\s+", " ", result.stdout)

    assert result.exit_code == 0
    assert "Audience filter: operators" in result.stdout
    assert "Available audiences: all readers, app builders, coding agents, contributors" in (
        normalized
    )
    assert "Multi-word audiences also accept hyphens or underscores" in normalized
    assert "Deployment" in result.stdout
    assert "Observability" in result.stdout
    assert "Journal durability" in result.stdout
    assert "Teaching ladder" not in result.stdout
    assert "First lesson" not in result.stdout


def test_docs_command_accepts_hyphenated_audience_filter(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--audience", "app-builders"])

    assert result.exit_code == 0
    assert "Audience filter: app-builders" in result.stdout
    assert "CLI and scaffolds" in result.stdout
    assert "Examples" in result.stdout
    assert "Teaching ladder" not in result.stdout


def test_docs_command_filters_json_routes_by_audience(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--audience", "maintainers", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    labels = {entry["label"] for entry in payload["entries"]}
    audiences = {entry["audience"] for entry in payload["entries"]}

    assert payload["status"] == "ok"
    assert payload["audience_filter"] == "maintainers"
    assert "maintainers" in payload["available_audiences"]
    assert "Architecture" in labels
    assert "Provider contracts" in labels
    assert "Journal durability" in labels
    assert "Validation reference" in labels
    assert "Teaching ladder" not in labels
    assert all("maintainers" in audience for audience in audiences)


def test_docs_command_accepts_underscored_json_audience_filter(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--audience", "provider_maintainers", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["audience_filter"] == "provider_maintainers"
    assert [entry["label"] for entry in payload["entries"]] == ["Provider contracts"]
    assert payload["entries"][0]["audience"] == "provider maintainers"


def test_docs_command_unknown_audience_reports_available_labels(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--audience", "time-travelers", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["command"] == "docs"
    assert payload["audience_filter"] == "time-travelers"
    assert "Unknown docs audience 'time-travelers'" in payload["message"]
    assert "learners" in payload["available_audiences"]
    assert "operators" in payload["available_audiences"]


def test_docs_route_paths_resolve_to_local_sources() -> None:
    problems: list[str] = []

    for entry in _DOCS_LINKS:
        route, _, fragment = entry["path"].partition("#")
        destination = REPO_ROOT / route.rstrip("/")
        label = entry["label"]

        if not destination.exists():
            problems.append(f"{label}: missing route {entry['path']!r}")
            continue
        if destination.is_dir() and not (destination / "README.md").exists():
            problems.append(f"{label}: directory route {route!r} has no README.md")
        if fragment:
            if destination.suffix != ".md":
                problems.append(
                    f"{label}: anchor #{fragment} targets non-Markdown route {route!r}"
                )
                continue
            if fragment not in github_markdown_heading_anchors(destination):
                problems.append(f"{label}: missing anchor #{fragment} in {route!r}")

    assert not problems, "Broken docs routes:\n" + "\n".join(problems)


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
