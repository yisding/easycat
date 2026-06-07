"""Guards for the top-level documentation map."""

import re
import shlex
from pathlib import Path
from urllib.parse import unquote

from typer.main import get_command

from easycat.cli._app import (
    _DOCS_COMMAND_NOTE,
    _DOCS_LINKS,
    _available_docs_audience_filters,
    _docs_entries,
    _register_commands,
    app,
)
from tests._justfile import just_recipe_commands
from tests._markdown import github_markdown_heading_anchors

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)\n]+)\)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
ANGLE_PLACEHOLDER_RE = re.compile(r"<[^>\s]+>")
ONBOARDING_GUARD_COMMANDS = (
    "just guard-docs",
    "just guard-examples",
    "just guard-templates",
    "just guard-contributing",
    "just guard-markdown",
)
DOCS_MAP_COMMANDS = ("uv run easycat docs", "uv run easycat docs --json")
AGENT_GUIDE_MACHINE_COMMANDS = (
    "uv run easycat doctor --json",
    "uv run easycat doctor --env-file .env --json",
    "uv run easycat explain json-schema",
    "uv run easycat bundles show PATH --json",
    "uv run easycat bundles export PATH --output DIR --json",
    "uv run easycat replay PATH --json",
    "uv run easycat validate quick",
    "uv run easycat validate quick --json",
    "uv run easycat validate contracts --json",
    "uv run easycat validate release --json",
    "uv run easycat validate report .easycat/validation/latest.json",
    "uv run easycat validate report .easycat/validation/latest.json --json",
)


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


def _root_path_chooser_command_spans() -> tuple[str, ...]:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Choose Your Path", 1)[1].split(
        "## Learn the pipeline from scratch", 1
    )[0]
    return tuple(
        match.group(1)
        for match in CODE_SPAN_RE.finditer(section)
        if match.group(1).startswith(("uv ", "easycat ", "just ", "docker "))
    )


def _strip_shell_comment(command: str) -> str:
    return re.sub(r"\s+#.*$", "", command).strip()


def _documented_commands(section: str, *, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    commands: list[str] = []
    seen: set[str] = set()

    def add(raw_command: str) -> None:
        command = _strip_shell_comment(raw_command)
        if command.startswith(prefixes) and command not in seen:
            seen.add(command)
            commands.append(command)

    for line in section.splitlines():
        add(line.strip())
    for match in CODE_SPAN_RE.finditer(section):
        add(match.group(1).strip())

    return tuple(commands)


def _easycat_command_tree() -> dict[str, set[str] | None]:
    _register_commands()
    root_command = get_command(app)
    return {
        name: set(nested_commands) if nested_commands is not None else None
        for name, command in root_command.commands.items()
        for nested_commands in (getattr(command, "commands", None),)
    }


def _docs_audience_hint_values() -> set[str]:
    filters = set(_available_docs_audience_filters())
    return (
        filters
        | {value.replace("-", "_") for value in filters}
        | {entry["audience"] for entry in _DOCS_LINKS}
    )


def _validate_docs_command_hint(*, label: str, args: list[str], problems: list[str]) -> None:
    valid_audiences = _docs_audience_hint_values()

    for index, arg in enumerate(args):
        if arg == "--audience":
            if index + 1 >= len(args):
                problems.append(f"{label}: docs audience hint missing value")
                return
            value = args[index + 1]
        elif arg.startswith("--audience="):
            value = arg.split("=", 1)[1]
        else:
            continue

        if value not in valid_audiences:
            problems.append(f"{label}: unknown docs audience hint {value}")


def _validate_easycat_command_hint(
    *,
    label: str,
    command_tree: dict[str, set[str] | None],
    subcommand: str,
    args: list[str],
    problems: list[str],
) -> None:
    if subcommand not in command_tree:
        problems.append(f"{label}: unknown easycat command {subcommand}")
        return

    if subcommand == "docs":
        _validate_docs_command_hint(label=label, args=args, problems=problems)

    nested_commands = command_tree[subcommand]
    if nested_commands is None:
        return

    if not args or args[0].startswith("-"):
        problems.append(f"{label}: missing easycat {subcommand} command")
        return

    nested_command = args[0]
    if nested_command not in nested_commands:
        problems.append(f"{label}: unknown easycat {subcommand} command {nested_command}")


def _cli_docs_command_hint_problems(entries: list[dict[str, object]]) -> list[str]:
    command_tree = _easycat_command_tree()
    just_recipes = just_recipe_commands(REPO_ROOT)
    problems: list[str] = []

    for entry in entries:
        label = str(entry["label"])
        for command in entry.get("commands", ()):
            tokens = shlex.split(command)
            if not tokens:
                problems.append(f"{label}: empty command hint")
                continue
            match tokens:
                case ["easycat", subcommand, *args]:
                    _validate_easycat_command_hint(
                        label=label,
                        command_tree=command_tree,
                        subcommand=subcommand,
                        args=args,
                        problems=problems,
                    )
                case ["uv", "run", "easycat", subcommand, *args]:
                    _validate_easycat_command_hint(
                        label=label,
                        command_tree=command_tree,
                        subcommand=subcommand,
                        args=args,
                        problems=problems,
                    )
                case ["uv", "run", "python", script, *_]:
                    if not (REPO_ROOT / script).exists():
                        problems.append(f"{label}: missing python script {script}")
                case ["uv", "run", "--env-file", _env_file, "python", script, *_]:
                    if not (REPO_ROOT / script).exists():
                        problems.append(f"{label}: missing python script {script}")
                case ["uv", "run", "pytest", *paths]:
                    for path in paths:
                        if not path.startswith("-") and not (REPO_ROOT / path).exists():
                            problems.append(f"{label}: missing pytest target {path}")
                case ["uv", "run", "ruff", *_] | ["uv", "sync", *_]:
                    continue
                case ["python", "-m", "http.server", *_args]:
                    if "--directory" in tokens:
                        directory_index = tokens.index("--directory") + 1
                        if directory_index >= len(tokens):
                            problems.append(f"{label}: http.server hint missing directory")
                            continue
                        directory = tokens[directory_index]
                        if not (REPO_ROOT / directory).is_dir():
                            problems.append(f"{label}: missing http.server directory {directory}")
                case ["just", recipe, *_]:
                    if recipe not in just_recipes:
                        problems.append(f"{label}: unknown just recipe {recipe}")
                case ["docker", "compose", *args]:
                    if "-f" in args:
                        compose_file = args[args.index("-f") + 1]
                        if not (REPO_ROOT / compose_file).exists():
                            problems.append(f"{label}: missing compose file {compose_file}")
                    else:
                        problems.append(f"{label}: docker compose hint missing -f")
                case _:
                    problems.append(f"{label}: unsupported command hint {command!r}")

    return problems


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
        "../tests/contracts/README.md",
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
    assert "uv run easycat docs --audience learners" in text
    assert "uv run easycat docs --json" in text
    assert "uv run easycat docs --audience maintainers --json" in text
    assert "repository path chooser" in normalized
    assert "installed app environment" in text
    assert "prints the same map" in text
    assert "docs route map" in normalized
    assert "route map with command hints and audience labels" in normalized
    assert (
        "Replace uppercase or angle-bracket placeholders in command hints, such as `PATH` "
        "or `<session_id>`"
    ) in normalized
    assert "The human docs menu also prints the available audience labels" in normalized
    assert "uv run easycat docs --audience app-builders" in text
    assert 'uv run easycat docs --audience "app builders"' in text
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
    assert "copyable create/preflight/check/docs/run commands" in normalized
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
    assert "top-level `available_audience_filters`" in text
    assert "top-level `audience_alias_note`" in text
    assert "`app-builders` and `coding-agents`" in text
    assert "top-level `command_note`" in text
    assert "installed CLI hints from repo-local `uv run` hints" in normalized
    assert "uv run easycat validate quick" in text
    assert "uv run easycat validate quick --json" in text
    assert "uv run easycat validate contracts --json" in text
    assert "uv run easycat validate release --json" in text
    assert "uv run easycat validate report .easycat/validation/latest.json" in text
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in text
    assert "automation needs validation run/report payloads" in normalized
    assert "script or coding agent needs validation output inside the standard CLI envelope" in (
        normalized
    )
    assert "journal CLI commands, the debugger UI, metrics, and traces" in normalized
    assert "Start with `easycat bundles list`" in normalized
    assert "uv sync --extra debugger --group dev" in text


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
        "tests/contracts/README.md": "uv run easycat validate contracts",
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
    assert "uv run easycat init --list-templates" in entries["README.md#choose-your-path"].get(
        "commands", ()
    )
    assert "uv run easycat init my-agent" in entries["README.md#choose-your-path"].get(
        "commands", ()
    )
    assert "uv run pytest tests/test_install_guidance.py" in entries[
        "README.md#choose-your-path"
    ].get("commands", ())


def test_start_here_docs_route_tracks_root_path_chooser_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    route_commands = set(entries["README.md#choose-your-path"].get("commands", ()))
    missing = [
        command for command in _root_path_chooser_command_spans() if command not in route_commands
    ]

    assert not missing, (
        "Start here docs route missing root path chooser command hints: " + ", ".join(missing)
    )


def test_quickstart_docs_route_matches_install_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    route_commands = set(entries["README.md#install"].get("commands", ()))
    install_section = (
        _route_target_text("README.md#install").split("## Install", 1)[1].split("## CLI", 1)[0]
    )
    install_commands = [
        match.group(1)
        for match in CODE_SPAN_RE.finditer(install_section)
        if match.group(1).startswith(("uv sync ", "uv run "))
    ]
    missing = [command for command in install_commands if command not in route_commands]

    assert not missing, "Quickstart docs route missing install command hints: " + ", ".join(
        missing
    )


def test_coding_agents_docs_route_matches_guide_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    command_section = agents.split("## Build, Test, and Development Commands", 1)[1].split(
        "## Coding Style",
        1,
    )[0]
    route_commands = entries["AGENTS.md"].get("commands", ())

    for command in (
        DOCS_MAP_COMMANDS
        + ("uv run easycat docs --audience coding-agents",)
        + AGENT_GUIDE_MACHINE_COMMANDS
        + ONBOARDING_GUARD_COMMANDS
    ):
        assert command in command_section
        assert command in route_commands


def test_architecture_docs_route_matches_guide_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    command_section = guide.split("## Commands", 1)[1].split("## Architecture", 1)[0]
    route_commands = entries["CLAUDE.md"].get("commands", ())

    for command in (
        DOCS_MAP_COMMANDS
        + ("uv run easycat docs --audience maintainers",)
        + AGENT_GUIDE_MACHINE_COMMANDS
        + ("uv run pytest tests/test_install_guidance.py",)
        + ONBOARDING_GUARD_COMMANDS
    ):
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
    first_lesson = (REPO_ROOT / "docs" / "teaching" / "00-hello-audio" / "README.md").read_text(
        encoding="utf-8"
    )
    first_lesson_commands = entries["docs/teaching/00-hello-audio/"].get("commands", ())

    for command in (
        "uv sync --extra quickstart --group dev",
        "uv run easycat doctor",
        "uv run easycat docs --audience learners",
        "uv run python docs/teaching/00-hello-audio/main.py",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in teaching_readme
        assert command in route_commands

    for command in (
        "uv sync --extra quickstart --group dev",
        "uv run python docs/teaching/00-hello-audio/main.py",
    ):
        assert command in first_lesson
        assert command in first_lesson_commands

    assert "uv run pytest tests/teaching/test_ladder_index.py" not in route_commands
    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "uv run easycat validate quick" not in first_lesson_commands
    assert "easycat validate quick" not in first_lesson_commands


def test_examples_docs_route_matches_examples_fast_path() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    examples_readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    intro = examples_readme.split("For the fastest local mic/speaker path:", 1)[0]
    fast_path = examples_readme.split("For the fastest local mic/speaker path:", 1)[1]
    route_commands = entries["examples/README.md"].get("commands", ())

    for command in (
        "uv run easycat init --list-templates",
        "uv run easycat init my-agent",
        "uv run easycat init --list-templates --json",
    ):
        assert command in intro
        assert command in route_commands

    for command in (
        "uv run easycat doctor",
        "uv run python examples/openai_agents_voice.py",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in fast_path
        assert command in route_commands

    assert "easycat doctor" not in route_commands
    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands


def test_public_api_docs_route_matches_contract_guard_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contract = (REPO_ROOT / "docs" / "public-api.md").read_text(encoding="utf-8")
    route_commands = entries["docs/public-api.md"].get("commands", ())

    for command in (
        "uv run easycat docs",
        "uv run easycat docs --json",
        "uv run easycat explain json-schema",
        "uv run pytest tests/test_public_api.py",
        "just guard-docs",
    ):
        assert command in contract
        assert command in route_commands

    assert "easycat docs --json" not in route_commands


def test_provider_contract_docs_route_matches_contract_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contract_readme = (REPO_ROOT / "tests" / "contracts" / "README.md").read_text(encoding="utf-8")
    route_commands = entries["tests/contracts/README.md"].get("commands", ())

    for command in (
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "uv run pytest tests/contracts",
        "uv run pytest tests/integration/test_provider_contract_matrix.py",
    ):
        assert command in contract_readme
        assert command in route_commands

    assert "easycat validate contracts" not in route_commands


def test_deployment_docs_route_matches_docker_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    deployment = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    route_commands = entries["docs/deployment/docker.md"].get("commands", ())

    for command in (
        "uv run easycat docs --audience operators",
        "docker compose -f docker/compose.yaml up --build",
        "python -m http.server 8080 --directory examples",
        "docker compose --env-file docker/.env -f docker/compose.yaml up --build",
        "docker compose -f docker/compose.yaml down",
    ):
        assert command in deployment
        assert command in route_commands

    assert "docker compose up --build" not in route_commands


def test_observability_docs_route_matches_journal_cli_entry_points() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    observability = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    cli_section = observability.split("- CLI entry points:", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]
    route_commands = entries["docs/observability.md"].get("commands", ())

    assert "uv run easycat docs --audience operators" in observability
    assert "uv run easycat docs --audience operators" in route_commands
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
        "uv sync --extra debugger --group dev",
    ):
        documented_command = command.replace("PATH", "<path>")
        assert f"`{documented_command}`" in cli_section
        assert command in route_commands

    assert "easycat bundles show <path>" not in route_commands
    assert "easycat bundles export <path>" not in route_commands
    assert "serve_bundle" in cli_section
    assert "serve_session" in cli_section
    assert "allow_remote=True" in cli_section


def test_journal_durability_docs_route_matches_inspection_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    durability = (REPO_ROOT / "src" / "easycat" / "runtime" / "DURABILITY.md").read_text(
        encoding="utf-8"
    )
    route_commands = entries["src/easycat/runtime/DURABILITY.md"].get("commands", ())

    for command in (
        "uv run pytest tests/runtime/test_sqlite_journal.py",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite --json",
        "uv run easycat inspect .easycat/crash-dumps/<session_id>.sqlite --json",
    ):
        assert command in durability
        assert command in route_commands

    assert "uv run easycat inspect PATH" not in route_commands


def test_validation_docs_route_matches_validation_workflow_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    validation_section = readme.split("## Validation Workflow", 1)[1].split("## ", 1)[0]
    route_commands = entries["README.md#validation-workflow"].get("commands", ())
    guard_commands = _documented_commands(validation_section, prefixes=("just guard-",))
    validation_commands = _documented_commands(
        validation_section,
        prefixes=("uv run easycat validate ",),
    )

    assert guard_commands
    assert validation_commands
    for command in guard_commands:
        assert command in validation_section
        assert command in route_commands

    for command in validation_commands:
        assert command in validation_section
        assert command in route_commands

    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json --json" not in route_commands


def test_contributing_docs_route_matches_validation_lane_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    quick_start = contributing.split("## Quick start", 1)[1].split(
        "## The development loop",
        1,
    )[0]
    maintenance_section = contributing.split("## Maintaining docs and onboarding maps", 1)[
        1
    ].split("## Parallel runs and xdist safety", 1)[0]
    validation_section = contributing.split(
        "## Validation slices and the `easycat validate` CLI",
        1,
    )[1].split("## ", 1)[0]
    route_commands = entries["CONTRIBUTING.md"].get("commands", ())
    guard_commands = _documented_commands(maintenance_section, prefixes=("just guard-",))
    validation_commands = _documented_commands(
        validation_section,
        prefixes=("uv run easycat validate ",),
    )

    assert "uv run easycat docs --audience contributors" in quick_start
    assert "uv run easycat docs --audience contributors" in route_commands

    assert guard_commands
    assert validation_commands
    for command in guard_commands:
        assert command in maintenance_section
        assert command in route_commands

    for command in validation_commands:
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
        "easycat validate contracts --json",
        "easycat validate release --json",
        "easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in reference
        assert command in route_commands

    assert "uv run easycat validate quick --json" not in route_commands


def test_cli_docs_command_hints_are_locally_valid() -> None:
    problems = _cli_docs_command_hint_problems(_docs_entries())

    assert not problems, "easycat docs command hints are stale:\n" + "\n".join(problems)


def test_cli_docs_command_hint_validator_checks_nested_easycat_commands() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken nested hints",
                "path": "README.md#cli",
                "audience": "contributors",
                "description": "Regression fixture for nested command validation.",
                "commands": (
                    "uv run easycat validate not-a-lane",
                    "easycat bundles not-a-bundle-command",
                ),
            }
        ]
    )

    assert "Broken nested hints: unknown easycat validate command not-a-lane" in problems
    assert "Broken nested hints: unknown easycat bundles command not-a-bundle-command" in problems


def test_cli_docs_command_hint_validator_checks_docs_audience_filters() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken docs audience hints",
                "path": "docs/README.md",
                "audience": "all readers",
                "description": "Regression fixture for docs audience validation.",
                "commands": (
                    "uv run easycat docs --audience time-travelers",
                    "easycat docs --audience",
                ),
            }
        ]
    )

    assert "Broken docs audience hints: unknown docs audience hint time-travelers" in problems
    assert "Broken docs audience hints: docs audience hint missing value" in problems


def test_cli_docs_command_placeholders_are_explained() -> None:
    placeholders: set[str] = set()
    for entry in _docs_entries():
        for command in entry.get("commands", ()):
            for token in shlex.split(command):
                if token.isupper():
                    placeholders.add(token)
                placeholders.update(ANGLE_PLACEHOLDER_RE.findall(token))

    missing = [
        placeholder for placeholder in placeholders if placeholder not in _DOCS_COMMAND_NOTE
    ]

    assert not missing, "command_note missing placeholders: " + ", ".join(missing)
    assert "placeholder" in _DOCS_COMMAND_NOTE.lower()
    assert "uppercase or angle-bracket placeholders" in _DOCS_COMMAND_NOTE
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
