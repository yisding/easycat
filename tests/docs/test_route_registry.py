from __future__ import annotations

from tests.docs._docs_index_helpers import (
    _DOCS_LINKS,
    CODE_SPAN_RE,
    ONBOARDING_GUARD_COMMANDS,
    REPO_ROOT,
    _docs_entries,
    _root_path_chooser_command_spans,
    _root_relative_doc_links,
    github_markdown_heading_anchors,
    re,
    unquote,
)


def test_docs_index_routes_primary_reader_paths() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    required_links = [
        "../README.md#choose-your-path",
        "../README.md#install",
        "install.md",
        "teaching/",
        "teaching/PROGRESS.md",
        "teaching/00-hello-audio/",
        "using-easycat/",
        "using-easycat/00-first-voice-app/",
        "using-easycat/01-runtime-modes/",
        "using-easycat/02-providers-and-voices/",
        "using-easycat/03-conversation-controls/",
        "using-easycat/04-tools-actions/",
        "using-easycat/05-agent-bridges/",
        "using-easycat/06-session-control/",
        "using-easycat/07-observability/",
        "using-easycat/08-testing-evals/",
        "using-easycat/09-multi-caller/",
        "using-easycat/10-telephony/",
        "using-easycat/11-production-ops/",
        "../README.md#cli",
        "cli.md",
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
        "reference/validation-vocabulary.md",
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
        "Installation and extras",
        "CLI and scaffolds",
        "Docs map",
        "Teaching ladder",
        "Progress worksheet",
        "First lesson",
        "EasyCat feature ladder",
        "Feature first lesson",
        "Feature runtime modes",
        "Feature providers and voices",
        "Feature conversation controls",
        "Feature tools and actions",
        "Feature agent bridges",
        "Feature session control",
        "Feature observability",
        "Feature testing and evals",
        "Feature multi-caller servers",
        "Feature telephony",
        "Telnyx Call Control setup",
        "Feature production operations",
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
    assert audiences["docs/install.md"] == "app builders"
    assert audiences["docs/cli.md"] == "app builders"
    assert audiences["AGENTS.md"] == "coding agents"
    assert audiences["docs/observability.md"] == "operators"
    assert audiences["docs/validation.md"] == "contributors"


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


def test_cli_docs_json_audience_hints_include_human_variant() -> None:
    missing: list[str] = []

    for entry in _docs_entries():
        commands = set(entry.get("commands", ()))
        for command in commands:
            if "easycat docs --audience " not in command or not command.endswith(" --json"):
                continue
            human_command = command.removesuffix(" --json")
            if human_command not in commands:
                missing.append(f"{entry['label']} ({entry['path']}): {human_command}")

    assert not missing, (
        "Docs routes with JSON audience hints should expose human variants:\n" + "\n".join(missing)
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
    assert diataxis["docs/install.md"] == "reference"
    assert diataxis["docs/cli.md"] == "how-to"
    assert diataxis["docs/teaching/"] == "tutorial"
    assert diataxis["docs/using-easycat/"] == "tutorial"
    assert diataxis["docs/architecture.md"] == "explanation"
    assert diataxis["docs/reference/events.md"] == "reference"
    assert diataxis["docs/reference/journal-records.md"] == "reference"
    assert diataxis["docs/reference/easyconfig.md"] == "reference"
    assert diataxis["docs/reference/session-lifecycle.md"] == "reference"
    assert diataxis["docs/public-api.md"] == "reference"

    docs_index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "`diataxis`" in docs_index


def test_cli_docs_routes_have_online_urls() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}

    assert entries["README.md#install"]["url"].endswith("/blob/main/README.md#install")
    assert entries["docs/install.md"]["url"].endswith("/blob/main/docs/install.md")
    assert entries["docs/cli.md"]["url"].endswith("/blob/main/docs/cli.md")
    assert entries["docs/README.md"]["url"].endswith("/blob/main/docs/README.md")
    assert entries["docs/teaching/"]["url"].endswith("/tree/main/docs/teaching")
    assert entries["docs/teaching/PROGRESS.md"]["url"].endswith(
        "/blob/main/docs/teaching/PROGRESS.md"
    )
    assert entries["docs/teaching/00-hello-audio/"]["url"].endswith(
        "/tree/main/docs/teaching/00-hello-audio"
    )
    assert entries["docs/using-easycat/"]["url"].endswith("/tree/main/docs/using-easycat")
    assert entries["docs/using-easycat/00-first-voice-app/"]["url"].endswith(
        "/tree/main/docs/using-easycat/00-first-voice-app"
    )
    assert entries["docs/using-easycat/01-runtime-modes/"]["url"].endswith(
        "/tree/main/docs/using-easycat/01-runtime-modes"
    )
    assert entries["docs/using-easycat/02-providers-and-voices/"]["url"].endswith(
        "/tree/main/docs/using-easycat/02-providers-and-voices"
    )
    assert entries["docs/using-easycat/03-conversation-controls/"]["url"].endswith(
        "/tree/main/docs/using-easycat/03-conversation-controls"
    )
    assert entries["docs/using-easycat/04-tools-actions/"]["url"].endswith(
        "/tree/main/docs/using-easycat/04-tools-actions"
    )
    assert entries["docs/using-easycat/05-agent-bridges/"]["url"].endswith(
        "/tree/main/docs/using-easycat/05-agent-bridges"
    )
    assert entries["docs/using-easycat/06-session-control/"]["url"].endswith(
        "/tree/main/docs/using-easycat/06-session-control"
    )
    assert entries["docs/using-easycat/07-observability/"]["url"].endswith(
        "/tree/main/docs/using-easycat/07-observability"
    )
    assert entries["docs/using-easycat/08-testing-evals/"]["url"].endswith(
        "/tree/main/docs/using-easycat/08-testing-evals"
    )
    assert entries["docs/using-easycat/09-multi-caller/"]["url"].endswith(
        "/tree/main/docs/using-easycat/09-multi-caller"
    )
    assert entries["docs/using-easycat/10-telephony/"]["url"].endswith(
        "/tree/main/docs/using-easycat/10-telephony"
    )
    assert entries["docs/using-easycat/11-production-ops/"]["url"].endswith(
        "/tree/main/docs/using-easycat/11-production-ops"
    )
    for route, entry in entries.items():
        route_path = route.split("#", 1)[0]
        expected_kind = "/tree/main/" if route_path.endswith("/") else "/blob/main/"
        assert expected_kind in entry["url"], route
    assert all(
        entry["url"].startswith("https://github.com/yisding/easycat/")
        for entry in entries.values()
    )
