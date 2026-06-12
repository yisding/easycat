"""Guards for the top-level documentation map."""

import re
import shlex
from pathlib import Path
from urllib.parse import unquote

from easycat.cli._app import (
    _DOCS_COMMAND_NOTE,
    _DOCS_LINKS,
    _DOCS_ONBOARDING_GUARD_COMMANDS,
    _DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
    _docs_entries,
)
from tests._command_hints import (
    command_hint_problems as _shared_command_hint_problems,
)
from tests._command_hints import (
    command_hint_variants as _shared_command_hint_variants,
)
from tests._command_hints import (
    documented_command_lines as _documented_command_lines,
)
from tests._command_hints import (
    documented_commands as _documented_commands,
)
from tests._markdown import github_markdown_heading_anchors

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)\n]+)\)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
ANGLE_PLACEHOLDER_RE = re.compile(r"<[^>\s]+>")
EXAMPLE_README_ROW_RE = re.compile(
    r"^\| \[(?P<name>[^\]]+\.py)\]\((?P<link>[^)]+\.py)\) "
    r"\| (?P<use_when>[^|]+) "
    r"\| `(?P<run>[^`]+)` "
    r"\| (?P<install>[^|]+) "
    r"\| (?P<env>[^|]+) \|$"
)
ONBOARDING_GUARD_COMMANDS = _DOCS_ONBOARDING_GUARD_COMMANDS
RAW_ONBOARDING_GUARD_COMMANDS = _DOCS_ONBOARDING_RAW_GUARD_COMMANDS
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


def _cli_docs_command_hint_problems(entries: list[dict[str, object]]) -> list[str]:
    return _shared_command_hint_problems(entries, repo_root=REPO_ROOT)


def _command_hint_variants(command: str) -> set[str]:
    return _shared_command_hint_variants(command)


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
        "validation.md",
        "../plan/validation/reference.md",
    ]

    missing = [link for link in required_links if link not in text]

    assert not missing, "docs/README.md missing route links: " + ", ".join(missing)


def test_docs_index_points_to_docs_command() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", text)
    required_commands = (
        "uv run easycat docs",
        "uv run easycat docs --json",
        "uv run easycat docs --audience learners",
        "uv run easycat docs --audience app-builders",
        "uv run easycat docs --audience operators",
        "uv run easycat docs --audience maintainers",
        "uv run easycat explain json-schema",
        "uv run easycat init --list-templates",
        "uv run easycat init --list-templates --json",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate contracts --json",
        "uv run easycat validate release --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    )

    for command in required_commands:
        assert command in text
    assert (
        "Coding agent? Use the root [AGENTS.md](../AGENTS.md) for repository coding rules"
    ) in normalized
    assert "[llms.txt](../llms.txt) for machine-readable docs route discovery" in normalized
    assert "when a script or coding agent" not in normalized
    assert "If `just` is not installed" in text
    for recipe in ONBOARDING_GUARD_COMMANDS:
        assert recipe in text
    guard_recipe_text = text.split("docs/onboarding guard recipes (", 1)[1].split(
        "), marker taxonomy",
        1,
    )[0]
    assert tuple(CODE_SPAN_RE.findall(guard_recipe_text)) == ONBOARDING_GUARD_COMMANDS


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
        "Maintainer guide",
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
    assert audiences["docs/validation.md"] == "contributors"


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
        "docs/validation.md": ("uv run easycat validate report .easycat/validation/latest.json"),
    }

    missing = [
        f"{path}: {command}"
        for path, command in required_commands.items()
        if command not in entries[path].get("commands", ())
    ]

    assert not missing, "easycat docs routes missing command hints: " + ", ".join(missing)
    assert "easycat doctor --json" in entries["README.md#cli"].get("commands", ())
    assert "easycat docs --audience learners" in entries["README.md#cli"].get("commands", ())
    assert "easycat docs --audience learners --json" in entries["README.md#cli"].get(
        "commands", ()
    )
    assert "easycat docs --audience app-builders" in entries["README.md#cli"].get("commands", ())
    assert "easycat docs --audience app-builders --json" in entries["README.md#cli"].get(
        "commands", ()
    )
    assert "easycat docs --audience operators" in entries["README.md#cli"].get("commands", ())
    assert "easycat docs --audience operators --json" in entries["README.md#cli"].get(
        "commands", ()
    )
    assert "easycat docs --audience maintainers" in entries["README.md#cli"].get("commands", ())
    assert "easycat docs --audience maintainers --json" in entries["README.md#cli"].get(
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


def test_cli_docs_env_file_doctor_hints_include_json_variant() -> None:
    missing: list[str] = []

    for entry in _docs_entries():
        commands = set(entry.get("commands", ()))
        for command in commands:
            if "easycat doctor --env-file .env" not in command or command.endswith(" --json"):
                continue
            json_command = f"{command} --json"
            if json_command not in commands:
                missing.append(f"{entry['label']} ({entry['path']}): {json_command}")

    assert not missing, (
        "Docs routes with `.env` doctor hints should expose parseable variants:\n"
        + "\n".join(missing)
    )


def test_cli_docs_plain_doctor_hints_include_json_variant() -> None:
    missing: list[str] = []

    for entry in _docs_entries():
        commands = set(entry.get("commands", ()))
        for command in commands:
            if not command.endswith("easycat doctor"):
                continue
            json_command = f"{command} --json"
            if json_command not in commands:
                missing.append(f"{entry['label']} ({entry['path']}): {json_command}")

    assert not missing, (
        "Docs routes with doctor hints should expose parseable variants:\n" + "\n".join(missing)
    )


def test_start_here_docs_route_tracks_root_path_chooser_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    route_commands = set(entries["README.md#choose-your-path"].get("commands", ()))
    missing = [
        command for command in _root_path_chooser_command_spans() if command not in route_commands
    ]

    assert not missing, (
        "Start here docs route missing root path chooser command hints: " + ", ".join(missing)
    )


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


def test_quickstart_docs_route_matches_install_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    route_commands = set(entries["README.md#install"].get("commands", ()))
    install_section = (
        _route_target_text("README.md#install")
        .split("## Install", 1)[1]
        .split("## Choose Your Path", 1)[0]
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
        + (
            "uv run easycat docs --audience coding-agents",
            "uv run easycat docs --audience coding-agents --json",
        )
        + AGENT_GUIDE_MACHINE_COMMANDS
        + ONBOARDING_GUARD_COMMANDS
        + RAW_ONBOARDING_GUARD_COMMANDS
    ):
        assert command in command_section
        assert command in route_commands


def test_maintainer_guide_docs_route_matches_guide_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    command_section = guide.split("## Commands", 1)[1].split("## Architecture", 1)[0]
    route_commands = entries["CLAUDE.md"].get("commands", ())

    for command in (
        DOCS_MAP_COMMANDS
        + (
            "uv run easycat docs --audience maintainers",
            "uv run easycat docs --audience maintainers --json",
        )
        + AGENT_GUIDE_MACHINE_COMMANDS
        + ("uv run pytest tests/test_install_guidance.py",)
        + ONBOARDING_GUARD_COMMANDS
        + RAW_ONBOARDING_GUARD_COMMANDS
    ):
        assert command in command_section
        assert command in route_commands


def test_architecture_explanation_carries_claude_guide_prose() -> None:
    """docs/architecture.md owns the architecture explanation; CLAUDE.md links to it."""
    page = re.sub(
        r"\s+", " ", (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    )
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    architecture_section = guide.split("## Architecture", 1)[1].split("## Key Patterns", 1)[0]

    assert "[docs/architecture.md](docs/architecture.md)" in architecture_section
    assert "ExternalAgentBridge" in page
    assert "SessionWiringContext" not in guide


def test_cli_docs_routes_declare_diataxis_categories() -> None:
    allowed = {"tutorial", "how-to", "reference", "explanation"}
    invalid = [
        f"{entry['label']} ({entry.get('diataxis')!r})"
        for entry in _DOCS_LINKS
        if entry.get("diataxis") not in allowed
    ]

    assert not invalid, "easycat docs routes missing valid diataxis labels: " + ", ".join(invalid)

    diataxis = {entry["path"]: entry["diataxis"] for entry in _DOCS_LINKS}
    assert diataxis["README.md#install"] == "tutorial"
    assert diataxis["docs/teaching/"] == "tutorial"
    assert diataxis["docs/architecture.md"] == "explanation"
    assert diataxis["docs/reference/events.md"] == "reference"
    assert diataxis["docs/reference/easyconfig.md"] == "reference"
    assert diataxis["docs/reference/session-lifecycle.md"] == "reference"
    assert diataxis["docs/public-api.md"] == "reference"

    docs_index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "`diataxis`" in docs_index


def test_events_reference_tracks_public_event_types() -> None:
    """docs/reference/events.md must list exactly the exported concrete events."""
    import easycat
    from easycat.events import Event

    text = (REPO_ROOT / "docs" / "reference" / "events.md").read_text(encoding="utf-8")
    catalog = text.split("## Event Catalog", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^- `([A-Za-z]+)`", catalog, flags=re.MULTILINE))
    exported = {
        name
        for name in easycat.__all__
        if isinstance(getattr(easycat, name), type)
        and issubclass(getattr(easycat, name), Event)
        and getattr(easycat, name) is not Event
    }

    missing = sorted(exported - documented)
    extra = sorted(documented - exported)
    assert not missing, "docs/reference/events.md missing events: " + ", ".join(missing)
    assert not extra, "docs/reference/events.md lists non-exported events: " + ", ".join(extra)

    # The page must teach the provider-scoped vs EasyCat-level distinction.
    assert "provider-scoped" in text.lower()
    assert "`STTEvent`" in text
    assert "`TTSEvent`" in text
    assert "easycat.events" in text


def _reference_section_field_names(text: str, heading: str) -> set[str]:
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^- `([A-Za-z_][A-Za-z0-9_]*)`", section, flags=re.MULTILINE))


def test_easyconfig_reference_tracks_config_fields() -> None:
    """The handwritten EasyConfig reference must match the live dataclasses."""
    import dataclasses

    from easycat import (
        AudioProcessingConfig,
        EasyConfig,
        ObservabilityConfig,
        SessionPolicyConfig,
    )

    text = (REPO_ROOT / "docs" / "reference" / "easyconfig.md").read_text(encoding="utf-8")
    expected = {
        "## Construction Fields": {f.name for f in dataclasses.fields(EasyConfig)},
        "## Audio Processing Fields": {f.name for f in dataclasses.fields(AudioProcessingConfig)},
        "## Observability Fields": {f.name for f in dataclasses.fields(ObservabilityConfig)},
        "## Session Policy Fields": {f.name for f in dataclasses.fields(SessionPolicyConfig)},
    }

    problems: list[str] = []
    for heading, names in expected.items():
        documented = _reference_section_field_names(text, heading)
        for name in sorted(names - documented):
            problems.append(f"{heading}: missing `{name}`")
        for name in sorted(documented - names):
            problems.append(f"{heading}: documents unknown `{name}`")

    assert not problems, "docs/reference/easyconfig.md is out of sync:\n" + "\n".join(problems)

    # The grouped fields double as top-level InitVar aliases; the page must say so.
    from easycat.config.easy import (
        _AUDIO_PROCESSING_ALIAS_FIELDS,
        _OBSERVABILITY_ALIAS_FIELDS,
        _SESSION_POLICY_ALIAS_FIELDS,
    )

    alias_names = (
        _AUDIO_PROCESSING_ALIAS_FIELDS | _OBSERVABILITY_ALIAS_FIELDS | _SESSION_POLICY_ALIAS_FIELDS
    )
    grouped_documented = (
        _reference_section_field_names(text, "## Audio Processing Fields")
        | _reference_section_field_names(text, "## Observability Fields")
        | _reference_section_field_names(text, "## Session Policy Fields")
    )
    assert alias_names == grouped_documented
    assert "## Top-Level Aliases" in text
    assert "test_easyconfig_reference_tracks_config_fields" in text


def test_session_lifecycle_reference_matches_lifecycle_contract() -> None:
    text = (REPO_ROOT / "docs" / "reference" / "session-lifecycle.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    lifecycle_section = guide.split("## Session Lifecycle", 1)[1].split("## Style", 1)[0]

    assert "docs/reference/session-lifecycle.md" in lifecycle_section
    for marker in (
        "`stop(force=True)`",
        "`session.shutdown()`",
        "`session.journal.read()`",
        "`session.export_debug_bundle(path)`",
        "`async with session:`",
        "record_to",
    ):
        assert marker in text, f"docs/reference/session-lifecycle.md missing {marker!r}"


def test_explain_concept_topics_print_docs_routes() -> None:
    """`easycat explain events|turn-taking|journal` must point at live docs routes."""
    from easycat.cli.diagnose._codes import META_ENTRIES

    route_paths = {entry["path"] for entry in _DOCS_LINKS}
    expected_routes = {
        "events": "docs/reference/events.md",
        "turn-taking": "docs/architecture.md",
        "journal": "docs/reference/session-lifecycle.md",
    }

    for slug, route in expected_routes.items():
        assert slug in META_ENTRIES, f"easycat explain is missing the {slug!r} concept topic"
        assert route in META_ENTRIES[slug].body
        assert route in route_paths


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
        "uv sync --extra local --group dev",
        "uv sync --extra quickstart --group dev",
        "uv run easycat doctor",
        "uv run easycat doctor --env-file .env",
        "uv run easycat docs --audience learners",
        "uv run easycat docs --audience learners --json",
        "uv run python docs/teaching/00-hello-audio/main.py",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in teaching_readme
        assert command in route_commands

    for command in (
        "uv sync --extra local --group dev",
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
    chooser = examples_readme.split("## Choose An Example", 1)[1].split("## Core Voice Loops", 1)[
        0
    ]
    fast_path = examples_readme.split("For the fastest local mic/speaker path:", 1)[1]
    route_commands = entries["examples/README.md"].get("commands", ())
    example_rows = {
        match.group("link"): match.group("run")
        for line in examples_readme.splitlines()
        if (match := EXAMPLE_README_ROW_RE.match(line)) is not None
    }

    for command in (
        "uv run easycat init --list-templates",
        "uv run easycat init my-agent",
        "uv run easycat init --list-templates --json",
    ):
        assert command in intro
        assert command in route_commands

    no_key_row = next(line for line in chooser.splitlines() if line.startswith("| No API keys |"))
    no_key_commands = [
        example_rows[link] for _, link in re.findall(r"\[([^]]+\.py)\]\(([^)]+\.py)\)", no_key_row)
    ]
    assert no_key_commands
    for command in no_key_commands:
        assert command in route_commands

    for command in (
        "uv run easycat doctor",
        "uv run easycat doctor --env-file .env",
        "uv run python examples/openai_agents_voice.py",
        "uv run --env-file .env python examples/openai_agents_voice.py",
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
        "uv run easycat docs --audience maintainers",
        "uv run easycat docs --json",
        "uv run easycat docs --audience maintainers --json",
        "uv run easycat explain json-schema",
        "uv run pytest tests/test_public_api.py",
        "just guard-docs",
        RAW_ONBOARDING_GUARD_COMMANDS[0],
    ):
        assert command in contract
        assert command in route_commands

    assert "If `just` is not installed" in contract
    assert "[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop)" in contract
    assert "easycat docs --json" not in route_commands


def test_provider_contract_docs_route_matches_contract_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contract_readme = (REPO_ROOT / "tests" / "contracts" / "README.md").read_text(encoding="utf-8")
    route_commands = entries["tests/contracts/README.md"].get("commands", ())

    for command in (
        "uv run easycat docs --audience provider-maintainers",
        "uv run easycat docs --audience provider-maintainers --json",
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "uv run pytest tests/contracts",
        "uv run pytest tests/contracts/test_provider_session_matrix.py",
    ):
        assert command in contract_readme
        assert command in route_commands

    assert "easycat validate contracts" not in route_commands


def test_extending_docs_route_matches_provider_author_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    extending_readme = (REPO_ROOT / "docs" / "extending" / "README.md").read_text(encoding="utf-8")
    route = entries["docs/extending/"]
    route_commands = route.get("commands", ())

    assert route["audience"] == "provider maintainers"
    for command in (
        "uv run easycat docs --audience provider-maintainers",
        "uv run easycat docs --audience provider-maintainers --json",
        "uv run easycat init my-provider --template provider",
        "uv run python examples/custom_transport.py",
        "uv run pytest tests/test_public_api.py",
        "uv run pytest tests/contracts",
    ):
        assert command in extending_readme
        assert command in route_commands

    for page in ("stt.md", "tts.md", "vad.md", "transport.md", "agent-bridge.md"):
        assert (REPO_ROOT / "docs" / "extending" / page).is_file()
        assert f"({page})" in extending_readme


def test_deployment_docs_route_matches_docker_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    deployment = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    route_commands = entries["docs/deployment/docker.md"].get("commands", ())

    for command in (
        "uv run easycat docs --audience operators",
        "uv run easycat docs --audience operators --json",
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
    assert "uv run easycat docs --audience operators --json" in observability
    assert "uv run easycat docs --audience operators --json" in route_commands
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
        "uv run easycat docs --audience operators-and-maintainers",
        "uv run easycat docs --audience operators-and-maintainers --json",
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
    validation_section = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")
    route_commands = entries["docs/validation.md"].get("commands", ())
    guard_commands = _documented_commands(validation_section, prefixes=("just guard-",))
    raw_guard_commands = _documented_command_lines(
        validation_section,
        prefixes=("uv run pytest ",),
    )
    validation_commands = _documented_commands(
        validation_section,
        prefixes=("uv run easycat validate ",),
    )

    assert guard_commands
    assert raw_guard_commands
    assert validation_commands
    assert "If `just` is not installed" in validation_section
    assert "[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop)" in validation_section
    assert "`uv run pytest ...` command behind each guard" in validation_section
    for command in guard_commands:
        assert command in validation_section
        assert command in route_commands

    for command in RAW_ONBOARDING_GUARD_COMMANDS:
        assert command in validation_section
        assert command in route_commands

    for command in validation_commands:
        assert command in validation_section
        assert command in route_commands

    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json --json" not in route_commands


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
    assert "uv run easycat docs --audience contributors --json" in quick_start
    assert "uv run easycat docs --audience contributors --json" in route_commands

    assert guard_commands
    assert validation_commands
    assert "If `just` is not installed" in maintenance_section
    assert "[the development loop](#the-development-loop)" in maintenance_section
    assert "`uv run pytest ...` command behind each guard" in maintenance_section
    for command in guard_commands:
        assert command in maintenance_section
        assert command in route_commands

    for command in RAW_ONBOARDING_GUARD_COMMANDS:
        assert command in contributing
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
        "easycat docs --audience release-maintainers --json",
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


def test_cli_docs_command_hint_validator_checks_pytest_node_ids() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken pytest hint",
                "path": "docs/validation.md",
                "audience": "contributors",
                "description": "Regression fixture for pytest node validation.",
                "commands": (
                    "uv run pytest tests/test_docs_index.py::missing_test "
                    "tests/test_docs_index_missing.py",
                ),
            }
        ]
    )

    assert "Broken pytest hint: missing pytest node tests/test_docs_index.py::missing_test" in (
        problems
    )
    assert "Broken pytest hint: missing pytest target tests/test_docs_index_missing.py" in (
        problems
    )


def test_cli_docs_command_hint_validator_checks_tool_path_targets() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken tool path hints",
                "path": "CONTRIBUTING.md#the-development-loop",
                "audience": "contributors",
                "description": "Regression fixture for maintenance command target validation.",
                "commands": (
                    "uv run ruff check missing.py",
                    "uv run mypy missing/package",
                    "uvx ty check missing/package",
                ),
            }
        ]
    )

    assert "Broken tool path hints: missing ruff check target missing.py" in problems
    assert "Broken tool path hints: missing mypy target missing/package" in problems
    assert "Broken tool path hints: missing ty check target missing/package" in problems


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
                    "easycat docs --audience --json",
                ),
            }
        ]
    )

    assert "Broken docs audience hints: unknown docs audience hint time-travelers" in problems
    assert problems.count("Broken docs audience hints: docs audience hint missing value") == 2


def test_cli_docs_command_hint_validator_checks_doctor_env_file_values() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken doctor env-file hints",
                "path": "README.md#cli",
                "audience": "app builders",
                "description": "Regression fixture for doctor env-file validation.",
                "commands": (
                    "easycat doctor --env-file --json",
                    "uv run easycat doctor --env-file --json",
                    "easycat doctor --env-file=missing-dir/.env",
                ),
            }
        ]
    )

    assert (
        problems.count("Broken doctor env-file hints: easycat doctor env-file hint missing value")
        == 2
    )
    assert (
        "Broken doctor env-file hints: missing easycat doctor env-file directory missing-dir/.env"
    ) in problems


def test_cli_docs_command_hint_validator_checks_uv_run_env_file_values() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uv run env-file hints",
                "path": "README.md#install",
                "audience": "new users",
                "description": "Regression fixture for uv run env-file validation.",
                "commands": (
                    "uv run --env-file .env python missing_script.py",
                    "uv run --env-file --isolated python examples/openai_agents_voice.py",
                    "uv run --env-file=missing-dir/.env python examples/openai_agents_voice.py",
                    "uv run --env-file .env",
                ),
            }
        ]
    )

    assert "Broken uv run env-file hints: missing python script missing_script.py" in problems
    assert "Broken uv run env-file hints: uv run env-file hint missing value" in problems
    assert "Broken uv run env-file hints: unsupported uv run option --isolated" in problems
    assert (
        "Broken uv run env-file hints: missing uv run env-file directory missing-dir/.env"
        in problems
    )
    assert "Broken uv run env-file hints: missing uv run command" in problems


def test_cli_docs_command_hint_validator_checks_uvicorn_targets() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uvicorn hints",
                "path": "examples/README.md",
                "audience": "app builders",
                "description": "Regression fixture for ASGI example command validation.",
                "commands": (
                    "uv run uvicorn examples.missing:create_app --factory",
                    "uv run uvicorn :create_app --factory",
                ),
            }
        ]
    )

    assert (
        "Broken uvicorn hints: missing uvicorn module target examples.missing:create_app"
        in problems
    )
    assert "Broken uvicorn hints: missing uvicorn module target" in problems


def test_cli_docs_command_hint_validator_checks_uv_sync_extras() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uv sync hints",
                "path": "README.md#install",
                "audience": "new users",
                "description": "Regression fixture for optional dependency validation.",
                "commands": (
                    "uv sync --extra not-a-real-extra --group dev",
                    "uv sync --extra=another-fake-extra --group dev",
                    "uv sync --extra",
                    "uv sync --extra --group dev",
                ),
            }
        ]
    )

    assert "Broken uv sync hints: unknown uv sync extra not-a-real-extra" in problems
    assert "Broken uv sync hints: unknown uv sync extra another-fake-extra" in problems
    assert problems.count("Broken uv sync hints: uv sync extra hint missing value") == 2


def test_cli_docs_command_hint_validator_checks_uv_sync_groups() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uv sync groups",
                "path": "README.md#install",
                "audience": "new users",
                "description": "Regression fixture for dependency group validation.",
                "commands": (
                    "uv sync --extra quickstart --group not-a-real-group",
                    "uv sync --extra quickstart --group=another-fake-group",
                    "uv sync --group",
                    "uv sync --group --extra quickstart",
                ),
            }
        ]
    )

    assert "Broken uv sync groups: unknown uv sync group not-a-real-group" in problems
    assert "Broken uv sync groups: unknown uv sync group another-fake-group" in problems
    assert problems.count("Broken uv sync groups: uv sync group hint missing value") == 2


def test_cli_docs_command_hint_validator_accepts_guide_placeholders() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Agent guide placeholders",
                "path": "AGENTS.md",
                "audience": "coding agents",
                "description": "Regression fixture for generic guide commands.",
                "commands": (
                    "just",
                    "uv sync --extra <name> --group dev",
                ),
            }
        ]
    )

    assert not problems


def test_cli_docs_command_hint_validator_checks_http_server_directories() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken http.server hints",
                "path": "docs/deployment/docker.md",
                "audience": "operators",
                "description": "Regression fixture for static file server hints.",
                "commands": (
                    "python -m http.server 8080 --directory missing-examples",
                    "python -m http.server 8080 --directory",
                    "python -m http.server 8080 --directory --bind localhost",
                ),
            }
        ]
    )

    assert "Broken http.server hints: missing http.server directory missing-examples" in problems
    assert problems.count("Broken http.server hints: http.server hint missing directory") == 2


def test_cli_docs_command_hint_validator_checks_docker_compose_files() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken docker compose hints",
                "path": "docs/deployment/docker.md",
                "audience": "operators",
                "description": "Regression fixture for compose file hints.",
                "commands": (
                    "docker compose up --build",
                    "docker compose -f docker/missing.yaml up --build",
                    "docker compose -f",
                    "docker compose -f --env-file docker/.env up --build",
                    "docker compose --env-file -f docker/compose.yaml up --build",
                    "docker compose --env-file missing-dir/.env -f docker/compose.yaml up",
                ),
            }
        ]
    )

    assert "Broken docker compose hints: docker compose hint missing -f" in problems
    assert "Broken docker compose hints: missing compose file docker/missing.yaml" in problems
    assert (
        problems.count("Broken docker compose hints: docker compose hint missing compose file")
        == 2
    )
    assert "Broken docker compose hints: docker compose env-file hint missing value" in problems
    assert (
        "Broken docker compose hints: missing docker compose env-file directory missing-dir/.env"
        in problems
    )


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
