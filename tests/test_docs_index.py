"""Guards for the top-level documentation map."""

import re
import shlex
from pathlib import Path
from urllib.parse import unquote

from easycat.cli._app import (
    _DOCS_COMMAND_NOTE,
    _DOCS_LINKS,
    _docs_entries,
    _register_commands,
    app,
)
from tests._justfile import just_recipe_commands
from tests._markdown import github_markdown_heading_anchors

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)\n]+)\)")
ONBOARDING_GUARD_COMMANDS = (
    "just guard-docs",
    "just guard-examples",
    "just guard-templates",
    "just guard-contributing",
    "just guard-markdown",
)
DOCS_MAP_COMMANDS = ("uv run easycat docs", "uv run easycat docs --json")


def _root_relative_doc_links() -> set[str]:
    path = REPO_ROOT / "docs" / "README.md"
    links = {"docs/README.md"}
    for match in LINK_RE.finditer(path.read_text(encoding="utf-8")):
        raw_target = match.group("target")
        target_path, sep, fragment = raw_target.partition("#")
        if target_path.startswith(("http://", "https://")):
            continue
        resolved = (path.parent / target_path).resolve()
        rel = resolved.relative_to(REPO_ROOT).as_posix()
        if raw_target.endswith("/") and not rel.endswith("/"):
            rel += "/"
        if sep:
            rel = f"{rel}#{fragment}"
        links.add(rel)
    return links


def _route_target_text(route: str) -> str:
    path = REPO_ROOT / route.split("#", 1)[0].rstrip("/")
    if path.is_dir():
        path = path / "README.md"
    return path.read_text(encoding="utf-8")


def _command_hint_variants(command: str) -> set[str]:
    variants = {command, command.replace("PATH", "<path>")}

    if command.startswith("easycat "):
        variants.update(f"uv run {variant}" for variant in tuple(variants))
    if command.startswith("uv run easycat "):
        variants.update(variant.removeprefix("uv run ") for variant in tuple(variants))

    return variants


def test_docs_heading_anchors_match_github_duplicate_suffixes(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Root\n## Route\n## Route\n## Route!\n", encoding="utf-8")

    assert github_markdown_heading_anchors(page) == {"root", "route", "route-1", "route-2"}


def test_docs_index_routes_primary_reader_paths() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    required_links = [
        "../README.md#choose-your-path",
        "../README.md#install",
        "teaching/",
        "teaching/00-hello-audio/",
        "../README.md#cli",
        "../examples/README.md",
        "../CLAUDE.md",
        "../AGENTS.md",
        "public-api.md",
        "../CONTRIBUTING.md",
        "deployment/docker.md",
        "observability.md",
        "../src/easycat/runtime/DURABILITY.md",
        "../README.md#validation-workflow",
        "../plan/validation/reference.md",
    ]

    missing = [link for link in required_links if link not in text]

    assert not missing, "docs/README.md missing route links: " + ", ".join(missing)


def test_docs_index_points_to_docs_command() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", text)

    assert "uv run easycat docs" in text
    assert "uv run easycat docs --json" in text
    assert "repository path chooser" in normalized
    assert "installed app environment" in text
    assert "prints the same map" in text
    assert "docs route map" in normalized
    assert "route map with command hints and audience labels" in normalized
    assert "Replace uppercase placeholders in command hints, such as `PATH`" in normalized
    assert "uv run easycat doctor --env-file .env" in text
    assert "easycat doctor --json" in text
    assert "uv run easycat doctor --env-file .env --json" in text
    assert "environment/check rows without Rich formatting" in normalized
    assert "uv run easycat init --list-templates" in text
    assert "uv run easycat init --list-templates --json" in text
    assert "base `easycat[...]` package requirements and extras" in normalized
    assert "required environment variables" in normalized
    assert "optional environment knobs" in normalized
    assert "generated files" in normalized
    assert "copyable create/check/run commands" in normalized
    assert "architecture map" in normalized
    assert "provider registries" in normalized
    assert "repository agent guide" in normalized
    assert "development commands, docs/onboarding guard recipes" in normalized
    assert "validation commands, and PR expectations" in normalized
    assert "docs/onboarding guard recipes" in normalized
    for recipe in ONBOARDING_GUARD_COMMANDS:
        assert recipe in text
    assert "uv run easycat explain json-schema" in text
    assert "command-specific success and error fields" in normalized
    assert "standard `--json` envelope" in text
    assert "`audience`" in text
    assert "top-level `command_note`" in text
    assert "installed CLI hints from repo-local `uv run` hints" in normalized
    assert "uv run easycat validate quick" in text
    assert "uv run easycat validate report .easycat/validation/latest.json" in text
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in text
    assert "script or coding agent needs the saved report inside the standard CLI envelope" in (
        normalized
    )


def test_cli_docs_routes_are_represented_in_docs_index() -> None:
    docs_links = _root_relative_doc_links()
    missing = [
        entry["path"]
        for entry in _DOCS_LINKS
        if isinstance(entry.get("path"), str) and entry["path"] not in docs_links
    ]

    assert not missing, "easycat docs routes missing from docs/README.md: " + ", ".join(missing)


def test_cli_docs_routes_are_unique() -> None:
    labels = [entry["label"] for entry in _DOCS_LINKS]
    paths = [entry["path"] for entry in _DOCS_LINKS]

    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    duplicate_paths = sorted({path for path in paths if paths.count(path) > 1})

    assert not duplicate_labels, "easycat docs route labels are duplicated: " + ", ".join(
        duplicate_labels
    )
    assert not duplicate_paths, "easycat docs route paths are duplicated: " + ", ".join(
        duplicate_paths
    )


def test_cli_docs_routes_keep_primary_reader_order() -> None:
    """Keep the first screen of ``easycat docs`` useful for primary readers."""
    labels = [entry["label"] for entry in _DOCS_LINKS]

    expected_prefix = [
        "Start here",
        "Quickstart",
        "CLI and scaffolds",
        "Docs map",
        "Teaching ladder",
        "First lesson",
        "Examples",
        "Architecture",
        "Coding agents",
    ]
    expected_suffix = ["Validation", "Validation reference"]

    assert labels[: len(expected_prefix)] == expected_prefix
    assert labels[-len(expected_suffix) :] == expected_suffix


def test_cli_docs_routes_resolve_locally() -> None:
    broken: list[str] = []

    for entry in _DOCS_LINKS:
        route, _, fragment = entry["path"].partition("#")
        destination = REPO_ROOT / route.rstrip("/")
        if not destination.exists():
            broken.append(f"{entry['label']}: missing {entry['path']}")
            continue
        if fragment and destination.suffix == ".md":
            anchors = github_markdown_heading_anchors(destination)
            if unquote(fragment) not in anchors:
                broken.append(f"{entry['label']}: missing #{fragment} in {route}")

    assert not broken, "easycat docs routes are stale:\n" + "\n".join(broken)


def test_cli_docs_routes_have_descriptions() -> None:
    missing = [
        f"{entry['label']} ({entry['path']})"
        for entry in _DOCS_LINKS
        if len(entry.get("description", "").split()) < 4
    ]

    assert not missing, "easycat docs routes missing useful descriptions: " + ", ".join(missing)


def test_cli_docs_routes_have_audience_labels() -> None:
    missing = [
        f"{entry['label']} ({entry['path']})"
        for entry in _DOCS_LINKS
        if len(entry.get("audience", "").split()) < 1
    ]
    audiences = {entry["path"]: entry["audience"] for entry in _DOCS_LINKS}

    assert not missing, "easycat docs routes missing audience labels: " + ", ".join(missing)
    assert audiences["README.md#choose-your-path"] == "all readers"
    assert audiences["README.md#install"] == "new users"
    assert audiences["README.md#cli"] == "app builders"
    assert audiences["AGENTS.md"] == "coding agents"
    assert audiences["docs/observability.md"] == "operators"
    assert audiences["README.md#validation-workflow"] == "contributors"


def test_cli_docs_routes_have_useful_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    required_commands = {
        "README.md#choose-your-path": "uv run easycat validate quick",
        "README.md#install": "uv run python examples/openai_agents_voice.py",
        "docs/teaching/": "uv run python docs/teaching/00-hello-audio/main.py",
        "README.md#cli": "easycat init --list-templates --json",
        "docs/README.md": "easycat docs --json",
        "examples/README.md": "uv run easycat validate quick",
        "CLAUDE.md": "uv run pytest tests/test_install_guidance.py",
        "AGENTS.md": "uv run easycat validate quick",
        "docs/public-api.md": "uv run pytest tests/test_public_api.py",
        "docs/deployment/docker.md": "docker compose -f docker/compose.yaml up --build",
        "docs/observability.md": "easycat bundles list",
        "src/easycat/runtime/DURABILITY.md": (
            "uv run pytest tests/runtime/test_sqlite_journal.py"
        ),
        "README.md#validation-workflow": (
            "uv run easycat validate report .easycat/validation/latest.json"
        ),
    }

    missing = [
        f"{path}: {command}"
        for path, command in required_commands.items()
        if command not in entries[path].get("commands", ())
    ]

    assert not missing, "easycat docs routes missing command hints: " + ", ".join(missing)
    assert "easycat doctor --json" in entries["README.md#cli"].get("commands", ())
    assert "uv run pytest tests/test_install_guidance.py" in entries[
        "README.md#choose-your-path"
    ].get("commands", ())


def test_coding_agents_docs_route_matches_docs_map_and_guard_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    command_section = agents.split("## Build, Test, and Development Commands", 1)[1].split(
        "## Coding Style",
        1,
    )[0]
    route_commands = entries["AGENTS.md"].get("commands", ())

    for command in DOCS_MAP_COMMANDS + ONBOARDING_GUARD_COMMANDS:
        assert command in command_section
        assert command in route_commands


def test_architecture_docs_route_matches_docs_map_and_guard_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    command_section = guide.split("## Commands", 1)[1].split("## Architecture", 1)[0]
    route_commands = entries["CLAUDE.md"].get("commands", ())

    for command in DOCS_MAP_COMMANDS + ONBOARDING_GUARD_COMMANDS:
        assert command in command_section
        assert command in route_commands


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


def test_teaching_ladder_docs_route_matches_learner_start_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    teaching_readme = (REPO_ROOT / "docs" / "teaching" / "README.md").read_text(encoding="utf-8")
    route_commands = entries["docs/teaching/"].get("commands", ())

    for command in (
        "uv sync --extra quickstart --group dev",
        "uv run easycat doctor",
        "uv run python docs/teaching/00-hello-audio/main.py",
    ):
        assert command in teaching_readme
        assert command in route_commands

    assert "uv run pytest tests/teaching/test_ladder_index.py" not in route_commands


def test_examples_docs_route_matches_examples_fast_path() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    examples_readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    fast_path = examples_readme.split("For the fastest local mic/speaker path:", 1)[1]
    route_commands = entries["examples/README.md"].get("commands", ())

    for command in (
        "uv run easycat doctor",
        "uv run python examples/openai_agents_voice.py",
        "uv run easycat validate quick",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in fast_path
        assert command in route_commands

    assert "easycat doctor" not in route_commands
    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands


def test_observability_docs_route_matches_journal_cli_entry_points() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    observability = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    cli_section = observability.split("- CLI entry points:", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]
    route_commands = entries["docs/observability.md"].get("commands", ())

    for command in (
        "easycat bundles list",
        "easycat bundles list --json",
        "easycat bundles show PATH",
        "easycat bundles show PATH --json",
        "easycat inspect PATH",
        "easycat inspect PATH --json",
        "easycat replay PATH",
        "easycat replay PATH --json",
        "easycat bundles export PATH",
        "easycat bundles export PATH --output DIR --json",
    ):
        documented_command = command.replace("PATH", "<path>")
        assert f"`{documented_command}`" in cli_section
        assert command in route_commands

    assert "easycat bundles show <path>" not in route_commands
    assert "easycat bundles export <path>" not in route_commands


def test_validation_docs_route_matches_validation_workflow_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    validation_section = readme.split("## Validation Workflow", 1)[1].split("## ", 1)[0]
    route_commands = entries["README.md#validation-workflow"].get("commands", ())

    for command in (
        "just guard-docs",
        "just guard-examples",
        "just guard-templates",
        "just guard-contributing",
        "just guard-markdown",
    ):
        assert command in validation_section
        assert command in route_commands

    for command in (
        "uv run easycat validate quick",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in validation_section
        assert command in route_commands

    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json --json" not in route_commands


def test_contributing_docs_route_matches_validation_report_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    maintenance_section = contributing.split("## Maintaining docs and onboarding maps", 1)[
        1
    ].split("## Parallel runs and xdist safety", 1)[0]
    validation_section = contributing.split(
        "## Validation slices and the `easycat validate` CLI",
        1,
    )[1].split("## ", 1)[0]
    route_commands = entries["CONTRIBUTING.md"].get("commands", ())

    for command in (
        "just guard-docs",
        "just guard-examples",
        "just guard-templates",
        "just guard-contributing",
        "just guard-markdown",
    ):
        assert command in maintenance_section
        assert command in route_commands

    for command in (
        "uv run easycat validate quick",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in validation_section
        assert command in route_commands

    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json --json" not in route_commands


def test_validation_reference_docs_route_matches_json_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    reference = (REPO_ROOT / "plan" / "validation" / "reference.md").read_text(encoding="utf-8")
    route_commands = entries["plan/validation/reference.md"].get("commands", ())

    for command in (
        "easycat validate quick --json",
        "easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in reference
        assert command in route_commands

    assert "uv run easycat validate quick --json" not in route_commands


def test_cli_docs_command_hints_are_locally_valid() -> None:
    _register_commands()
    registered_commands = {
        command.name for command in app.registered_commands if command.name is not None
    }
    registered_commands.update(
        group.name for group in app.registered_groups if group.name is not None
    )
    just_recipes = just_recipe_commands(REPO_ROOT)
    problems: list[str] = []

    for entry in _docs_entries():
        for command in entry.get("commands", ()):
            tokens = shlex.split(command)
            if not tokens:
                problems.append(f"{entry['label']}: empty command hint")
                continue
            match tokens:
                case ["easycat", subcommand, *_]:
                    if subcommand not in registered_commands:
                        problems.append(f"{entry['label']}: unknown easycat command {subcommand}")
                case ["uv", "run", "easycat", subcommand, *_]:
                    if subcommand not in registered_commands:
                        problems.append(f"{entry['label']}: unknown easycat command {subcommand}")
                case ["uv", "run", "python", script, *_]:
                    if not (REPO_ROOT / script).exists():
                        problems.append(f"{entry['label']}: missing python script {script}")
                case ["uv", "run", "pytest", *paths]:
                    for path in paths:
                        if not path.startswith("-") and not (REPO_ROOT / path).exists():
                            problems.append(f"{entry['label']}: missing pytest target {path}")
                case ["uv", "run", "ruff", *_] | ["uv", "sync", *_]:
                    continue
                case ["just", recipe, *_]:
                    if recipe not in just_recipes:
                        problems.append(f"{entry['label']}: unknown just recipe {recipe}")
                case ["docker", "compose", *args]:
                    if "-f" in args:
                        compose_file = args[args.index("-f") + 1]
                        if not (REPO_ROOT / compose_file).exists():
                            problems.append(
                                f"{entry['label']}: missing compose file {compose_file}"
                            )
                    else:
                        problems.append(f"{entry['label']}: docker compose hint missing -f")
                case _:
                    problems.append(f"{entry['label']}: unsupported command hint {command!r}")

    assert not problems, "easycat docs command hints are stale:\n" + "\n".join(problems)


def test_cli_docs_command_placeholders_are_explained() -> None:
    placeholders = sorted(
        {
            token
            for entry in _docs_entries()
            for command in entry.get("commands", ())
            for token in shlex.split(command)
            if token.isupper()
        }
    )

    missing = [
        placeholder for placeholder in placeholders if placeholder not in _DOCS_COMMAND_NOTE
    ]

    assert not missing, "command_note missing placeholders: " + ", ".join(missing)
    assert "placeholder" in _DOCS_COMMAND_NOTE.lower()
    assert "Bare easycat commands use installed CLI form" in _DOCS_COMMAND_NOTE
    assert "prefix them with uv run" in _DOCS_COMMAND_NOTE
    assert "Commands already starting with uv run are repo-local" in _DOCS_COMMAND_NOTE
    assert "just commands are repo-local shortcuts" in _DOCS_COMMAND_NOTE
    assert "raw command table" in _DOCS_COMMAND_NOTE
    assert "repository root" in _DOCS_COMMAND_NOTE


def test_cli_docs_routes_have_online_urls() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}

    assert entries["README.md#install"]["url"].endswith("/blob/main/README.md#install")
    assert entries["docs/README.md"]["url"].endswith("/blob/main/docs/README.md")
    assert entries["docs/teaching/"]["url"].endswith("/tree/main/docs/teaching")
    assert entries["docs/teaching/00-hello-audio/"]["url"].endswith(
        "/tree/main/docs/teaching/00-hello-audio"
    )
    for route, entry in entries.items():
        route_path = route.split("#", 1)[0]
        expected_kind = "/tree/main/" if route_path.endswith("/") else "/blob/main/"
        assert expected_kind in entry["url"], route
    assert all(
        entry["url"].startswith("https://github.com/yisding/easycat/")
        for entry in entries.values()
    )
