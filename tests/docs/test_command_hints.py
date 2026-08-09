from __future__ import annotations

from tests.docs._docs_index_helpers import (
    REPO_ROOT,
    _cli_docs_command_hint_problems,
    _command_hint_variants,
    _docs_entries,
    _documented_command_lines,
    _documented_commands,
    _root_path_chooser_command_spans,
    _route_target_text,
)


def test_docs_index_command_hints_are_locally_valid() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    commands = _documented_commands(
        text,
        prefixes=("easycat ", "just ", "uv run easycat ", "uv sync "),
    )
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "docs/README.md",
                "path": "docs/README.md",
                "audience": "all readers",
                "description": "Docs index command hints.",
                "commands": commands,
            }
        ]
    )

    assert commands
    assert not problems, "docs/README.md command hints are stale:\n" + "\n".join(problems)


def test_cli_docs_routes_have_useful_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    required_commands = {
        "README.md#choose-your-path": "uv run easycat validate quick",
        "README.md#install": "uv run python examples/openai_agents_voice.py",
        "docs/teaching/": "uv run python docs/teaching/00-hello-audio/main.py",
        "docs/using-easycat/": ("uv run python docs/using-easycat/00-first-voice-app/main.py"),
        "docs/teaching/PROGRESS.md": (
            "uv run python docs/teaching/offline_spine.py --run --jobs 4 --show-evidence"
        ),
        "docs/install.md": "uv sync --extra quickstart --group dev",
        "docs/cli.md": "easycat init --list-templates --json",
        "docs/README.md": "easycat docs --json",
        "examples/README.md": "uv run easycat validate quick",
        "CLAUDE.md": "uv run pytest tests/install/test_install_guidance.py",
        "AGENTS.md": "uv run easycat validate quick",
        "docs/public-api.md": "uv run pytest tests/test_public_api.py",
        "tests/contracts/README.md": "uv run easycat validate contracts",
        "docs/deployment/docker.md": "docker compose -f docker/compose.yaml up --build",
        "docs/observability.md": "easycat bundles list",
        "src/easycat/runtime/DURABILITY.md": (
            "uv run pytest tests/runtime/test_sqlite_journal.py"
        ),
        "docs/validation.md": ("uv run easycat validate report .easycat/validation/latest.json"),
    }

    missing = [
        f"{path}: {command}"
        for path, command in required_commands.items()
        if command not in entries[path].get("commands", ())
    ]

    assert not missing, "easycat docs routes missing command hints: " + ", ".join(missing)
    assert "easycat doctor --json" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --verbose" in entries["docs/cli.md"].get("commands", ())
    assert "easycat init my-agent --easycat-git URL --easycat-git-rev REV" in entries[
        "docs/cli.md"
    ].get("commands", ())
    assert (
        "uv run easycat init my-agent --easycat-git "
        "https://github.com/yisding/easycat.git --easycat-git-rev <commit-sha>"
        in entries["docs/install.md"].get("commands", ())
    )
    assert "easycat docs --verbose" in entries["docs/README.md"].get("commands", ())
    assert "easycat docs --audience learners" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience learners --json" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience app-builders" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience app-builders --json" in entries["docs/cli.md"].get(
        "commands", ()
    )
    assert "easycat docs --audience operators" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience operators --json" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience maintainers" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience maintainers --json" in entries["docs/cli.md"].get(
        "commands", ()
    )
    assert "easycat docs --audience coding-agents" in entries["docs/cli.md"].get("commands", ())
    assert "easycat docs --audience coding-agents --json" in entries["docs/cli.md"].get(
        "commands", ()
    )
    assert "uv run easycat init --list-templates" in entries["README.md#choose-your-path"].get(
        "commands", ()
    )
    assert "uv run easycat init my-agent" in entries["README.md#choose-your-path"].get(
        "commands", ()
    )
    assert "uv run easycat docs --audience maintainers" in entries[
        "README.md#choose-your-path"
    ].get("commands", ())
    assert "uv run easycat docs --audience coding-agents" in entries[
        "README.md#choose-your-path"
    ].get("commands", ())
    assert "uv run easycat doctor --env-file .env" in entries["README.md#choose-your-path"].get(
        "commands", ()
    )
    assert "uv run --env-file .env python examples/openai_agents_voice.py" in entries[
        "README.md#choose-your-path"
    ].get("commands", ())
    assert "uv run python examples/journal_demo.py" in entries["README.md#choose-your-path"].get(
        "commands", ()
    )
    assert "uv run python docs/teaching/offline_spine.py --run --jobs 4" in entries[
        "README.md#choose-your-path"
    ].get("commands", ())
    assert "uv run python docs/teaching/offline_spine.py --run --jobs 4 --json" in entries[
        "docs/teaching/"
    ].get("commands", ())


def test_root_path_chooser_command_hints_are_locally_valid() -> None:
    commands = _root_path_chooser_command_spans()
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "README.md path chooser",
                "path": "README.md#choose-your-path",
                "audience": "all readers",
                "description": "Root README first-screen command hints.",
                "commands": commands,
            }
        ]
    )

    assert commands
    assert not problems, "README.md path chooser commands are stale:\n" + "\n".join(problems)


def test_cli_docs_command_hints_are_visible_on_target_pages() -> None:
    missing: list[str] = []

    for entry in _docs_entries():
        target_text = _route_target_text(entry["path"])
        for command in entry.get("commands", ()):
            if not any(variant in target_text for variant in _command_hint_variants(command)):
                missing.append(f"{entry['label']} ({entry['path']}): {command}")

    assert not missing, "easycat docs command hints missing from target pages:\n" + "\n".join(
        missing
    )


def test_validation_workflow_command_hints_are_locally_valid() -> None:
    validation_section = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")
    commands = _documented_command_lines(
        validation_section,
        prefixes=("just ", "uv run easycat ", "uv run pytest "),
    )
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "docs/validation.md validation workflow",
                "path": "docs/validation.md",
                "audience": "contributors",
                "description": "Validation workflow doc commands.",
                "commands": commands,
            }
        ]
    )

    assert commands
    assert not problems, "docs/validation.md workflow commands are stale:\n" + "\n".join(problems)


def test_cli_docs_command_hints_are_locally_valid() -> None:
    problems = _cli_docs_command_hint_problems(_docs_entries())

    assert not problems, "easycat docs command hints are stale:\n" + "\n".join(problems)
