from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from tests._justfile import just_recipe_commands

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV_LOOP_ROW_RE = re.compile(
    r"^\| (?P<task>[^|]+) \| `just (?P<recipe>[^` ]+)(?P<args>[^`]*)?` "
    r"\| `(?P<raw>[^`]+)` \|$"
)
_VALIDATION_ROW_RE = re.compile(
    r"^\| `(?P<slice>[^`]+)` \| `(?P<command>[^`]+)` \| (?P<markers>[^|]+) \|$"
)
_VALIDATION_CHOOSER_ROW_RE = re.compile(
    r"^\| (?P<touches>[^|]+) \| `(?P<command>[^`]+)` \| (?P<why>[^|]+) \|$"
)


def _pytest_marker_names() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declarations = pyproject["tool"]["pytest"]["ini_options"]["markers"]

    return {declaration.split(":", 1)[0].split("(", 1)[0].strip() for declaration in declarations}


def _contributing_marker_taxonomy() -> str:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    return contributing.split("## Marker taxonomy", 1)[1].split("## Flaky-quarantine", 1)[0]


def _contributing_runbundle_section() -> str:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    return contributing.split("## RunBundle golden tests", 1)[1].split(
        "## Adding an STT or TTS provider",
        1,
    )[0]


def _contributing_docs_onboarding_section() -> str:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    return contributing.split("## Maintaining docs and onboarding maps", 1)[1].split(
        "## Parallel runs and xdist safety",
        1,
    )[0]


def _marker_name_is_documented(section: str, marker: str) -> bool:
    return re.search(rf"`{re.escape(marker)}(?:\([^`]*\))?`", section) is not None


def _development_loop_rows() -> list[dict[str, str]]:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []

    for line in contributing.splitlines():
        match = _DEV_LOOP_ROW_RE.match(line)
        if match is not None:
            rows.append(match.groupdict())

    assert rows, "CONTRIBUTING.md development-loop table was not found"
    return rows


def _validation_slice_rows() -> list[dict[str, str]]:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = contributing.split("## Validation slices", 1)[1].split("## ", 1)[0]
    rows: list[dict[str, str]] = []

    for line in section.splitlines():
        match = _VALIDATION_ROW_RE.match(line)
        if match is not None:
            rows.append(match.groupdict())

    assert rows, "CONTRIBUTING.md validation-slices table was not found"
    return rows


def _validation_chooser_rows() -> list[dict[str, str]]:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = contributing.split("## Validation slices", 1)[1].split("## ", 1)[0]
    rows: list[dict[str, str]] = []

    for line in section.splitlines():
        match = _VALIDATION_CHOOSER_ROW_RE.match(line)
        if (
            match is not None
            and match.group("touches") != "If your change touches"
            and not match.group("touches").strip().startswith("`")
        ):
            rows.append(match.groupdict())

    assert rows, "CONTRIBUTING.md validation chooser table was not found"
    return rows


def _render_recipe_command(command: str, args_text: str | None) -> str:
    args = shlex.split(args_text or "")
    rendered = command

    if "{{ prepend('--extra ', EXTRAS) }}" in rendered:
        extras = " ".join(f"--extra {extra}" for extra in args)
        return rendered.replace("{{ prepend('--extra ', EXTRAS) }}", extras)

    if args:
        rendered = re.sub(r'"?\{\{\s*[A-Z_]+\s*\}\}"?', args[0], rendered)

    return rendered


def _registered_easycat_commands() -> set[str]:
    from easycat.cli import _app

    _app._register_commands()
    commands = {command.name for command in _app.app.registered_commands if command.name}
    commands.update(group.name for group in _app.app.registered_groups if group.name)
    return commands


def test_contributing_quick_start_points_to_docs_command() -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    quick_start = contributing.split("## Quick start", 1)[1].split(
        "## The development loop",
        1,
    )[0]
    normalized = re.sub(r"\s+", " ", quick_start)

    assert "uv run easycat docs" in quick_start
    assert "uv run easycat docs --json" in quick_start
    assert "uv run easycat explain json-schema" in quick_start
    assert "maintained reader-facing map" in quick_start
    assert "script or coding agent needs the same route map with command hints" in normalized
    assert "replace uppercase placeholders such as `PATH` before running those hints" in (
        normalized
    )
    assert "standard `--json` envelope" in normalized
    assert "command-specific fields" in normalized
    assert "CLI and scaffold commands" in quick_start
    assert "uv run easycat doctor" in quick_start
    assert "uv run easycat doctor --json" in quick_start
    assert "uv run easycat doctor --env-file .env --json" in quick_start
    assert "script or coding agent needs parseable environment/check rows" in normalized
    assert "before debugging tests or examples" in quick_start

    registered_commands = _registered_easycat_commands()
    command_mentions = re.findall(r"\buv run easycat\s+(?P<command>[A-Za-z0-9_-]+)\b", quick_start)
    stale = sorted({command for command in command_mentions if command not in registered_commands})
    assert not stale, (
        "CONTRIBUTING.md quick-start references stale easycat commands: " + ", ".join(stale)
    )


def test_contributing_validation_report_points_to_latest_artifact() -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", contributing)

    assert "uv run easycat validate report .easycat/validation/latest.json" in contributing
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in contributing
    assert "renders the latest saved report" in normalized
    assert "script or coding agent needs the saved report inside the standard CLI envelope" in (
        normalized
    )
    assert ".easycat/validation/runs/<run_id>/report.json" in contributing
    assert "uv run easycat validate report <path>" not in contributing


def test_contributing_docs_onboarding_map_lists_resolving_guard_targets() -> None:
    section = _contributing_docs_onboarding_section()
    normalized = re.sub(r"\s+", " ", section)
    commands = (
        "uv run pytest "
        "tests/test_quickstart_e2e.py::"
        "test_readme_choose_your_path_routes_primary_onboarding_surfaces "
        "tests/test_docs_index.py "
        "tests/cli/test_app.py::test_docs_command "
        "tests/cli/test_app.py::test_docs_command_json",
        "uv run pytest "
        "tests/test_examples.py::test_examples_readme_choose_example_table_tracks_matrix "
        "tests/test_docs_index.py::test_examples_docs_route_matches_examples_fast_path",
        "uv run pytest "
        "tests/cli/test_templates.py "
        "tests/cli/test_init.py::test_list_templates "
        "tests/cli/test_init.py::test_list_templates_json",
        "uv run pytest "
        "tests/test_contributing.py "
        "tests/test_docs_index.py::"
        "test_contributing_docs_route_matches_validation_report_commands "
        "tests/test_validation_plan.py",
        "uv run pytest tests/test_markdown_links.py",
    )

    assert "narrow guard that owns that surface" in normalized
    assert "Then run `uv run easycat validate quick` before a PR" in normalized
    for phrase in (
        "Root README chooser or docs route map",
        "Examples chooser or command matrix",
        "Scaffold templates or template catalog",
        "Contributor and validation guidance",
        "Markdown links in maintained docs",
        "Root onboarding links, `easycat docs`, and JSON route entries",
        "Generated README sections, line budgets, catalog text, and catalog JSON",
        "`justfile` parity, validation lanes, docs-route hints, and plan current-state evidence",
    ):
        assert phrase in section

    for command in commands:
        assert command in section
        parts = shlex.split(command)
        assert parts[:3] == ["uv", "run", "pytest"]
        for target in parts[3:]:
            file_target, _, test_name = target.partition("::")
            path = REPO_ROOT / file_target
            assert path.exists(), f"CONTRIBUTING.md docs guard references missing {file_target}"
            if test_name:
                text = path.read_text(encoding="utf-8")
                assert f"def {test_name}" in text, (
                    f"CONTRIBUTING.md docs guard references missing {target}"
                )


def test_contributing_development_loop_just_recipes_stay_current() -> None:
    recipes = just_recipe_commands(REPO_ROOT)
    missing: list[str] = []
    stale_raw_commands: list[str] = []

    for row in _development_loop_rows():
        recipe = row["recipe"]
        raw_command = row["raw"]
        if recipe not in recipes:
            missing.append(f"{row['task'].strip()}: just {recipe}")
            continue
        if "..." in raw_command:
            stale_raw_commands.append(f"{recipe}: raw command contains placeholder")
            continue
        expected_command = _render_recipe_command(recipes[recipe], row.get("args"))
        if raw_command != expected_command:
            stale_raw_commands.append(
                f"{recipe}: CONTRIBUTING has {raw_command!r}, justfile has {expected_command!r}"
            )

    assert not missing, "CONTRIBUTING.md references missing just recipes: " + ", ".join(missing)
    assert not stale_raw_commands, "CONTRIBUTING.md stale raw commands: " + "; ".join(
        stale_raw_commands
    )


def test_contributing_development_loop_lists_public_just_recipes() -> None:
    recipes = set(just_recipe_commands(REPO_ROOT)) - {"default"}
    documented_recipes = {row["recipe"] for row in _development_loop_rows()}
    missing = sorted(recipes - documented_recipes)

    assert not missing, "CONTRIBUTING.md missing just recipes: " + ", ".join(missing)


def test_contributing_development_loop_lists_validation_just_recipes() -> None:
    recipes = just_recipe_commands(REPO_ROOT)
    documented_recipes = {row["recipe"] for row in _development_loop_rows()}
    validation_recipes = sorted(name for name in recipes if name.startswith("validate-"))
    missing = [recipe for recipe in validation_recipes if recipe not in documented_recipes]

    assert not missing, "CONTRIBUTING.md missing validation recipes: " + ", ".join(missing)


def test_contributing_validation_slices_track_public_validate_lanes() -> None:
    from typer.main import get_command

    from easycat.cli.validate import validate_app

    documented_slices = {row["slice"] for row in _validation_slice_rows()}
    public_lanes = set(get_command(validate_app).commands) - {"report"}
    missing = sorted(public_lanes - documented_slices)
    stale = sorted(documented_slices - public_lanes)

    assert not missing, "CONTRIBUTING.md missing validation lanes: " + ", ".join(missing)
    assert not stale, "CONTRIBUTING.md advertises stale validation lanes: " + ", ".join(stale)


def test_contributing_validation_slice_commands_use_repo_local_uv_run() -> None:
    stale = [
        f"{row['slice']}: {row['command']}"
        for row in _validation_slice_rows()
        if not row["command"].startswith("uv run easycat validate ")
    ]

    assert not stale, "CONTRIBUTING.md validation commands should use uv run: " + "; ".join(stale)


def test_contributing_validation_chooser_tracks_slice_commands() -> None:
    slice_commands = {row["slice"]: row["command"] for row in _validation_slice_rows()}
    chooser_rows = _validation_chooser_rows()
    chooser_commands = {row["command"] for row in chooser_rows}

    for slice_name in ("quick", "socket", "stress", "contracts", "latency", "live", "release"):
        assert slice_commands[slice_name] in chooser_commands
    assert "uv run easycat validate report .easycat/validation/latest.json" in chooser_commands
    for row in chooser_rows:
        assert row["command"].startswith("uv run easycat validate ")
        assert row["touches"].strip()
        assert row["why"].strip()
    normalized_rows = " ".join(" ".join(row.values()) for row in chooser_rows)
    for phrase in (
        "Most code, docs, CLI help, unit behavior",
        "WebSocket, WebRTC, transports",
        "Provider protocols, cassettes, contract matrix, or agent bridges",
        "Queues, load, reliability sampling, or saturation behavior",
        "Live latency budgets or end-to-end timing",
        "Live provider adapters, credentials, or provider/surface canaries",
        "Packaging, release workflows, or installed-wheel behavior",
        "A saved validation artifact",
    ):
        assert phrase in normalized_rows


def test_contributing_marker_taxonomy_lists_pytest_markers() -> None:
    section = _contributing_marker_taxonomy()
    missing = sorted(
        marker
        for marker in _pytest_marker_names()
        if not _marker_name_is_documented(section, marker)
    )

    assert not missing, "CONTRIBUTING.md marker taxonomy missing pytest markers: " + ", ".join(
        missing
    )


def test_contributing_runbundle_helpers_track_public_testing_exports() -> None:
    from easycat.debug import testing

    section = _contributing_runbundle_section()
    missing = sorted(name for name in testing.__all__ if f"`{name}`" not in section)

    assert not missing, "CONTRIBUTING.md RunBundle section missing helpers: " + ", ".join(missing)


def test_validation_plan_matches_contributor_quick_command() -> None:
    rows = {row["slice"]: row["command"] for row in _validation_slice_rows()}
    tasks = (REPO_ROOT / "plan" / "validation" / "tasks.md").read_text(encoding="utf-8")

    assert rows["quick"] in tasks


def test_validation_tasks_v05_current_state_tracks_contributor_workflow() -> None:
    from typer.main import get_command

    from easycat.cli.validate import validate_app

    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V0.5 Document Contributor Workflow", 1)[1].split(
        "## V1: First-Class CLI And CI Artifacts",
        1,
    )[0]
    normalized_section = re.sub(r"\s+", " ", section)
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    docs_index_tests = (REPO_ROOT / "tests/test_docs_index.py").read_text(encoding="utf-8")
    test_source = (REPO_ROOT / "tests/test_contributing.py").read_text(encoding="utf-8")
    recipes = set(just_recipe_commands(REPO_ROOT)) - {"default"}
    documented_recipes = {row["recipe"] for row in _development_loop_rows()}
    documented_slices = {row["slice"] for row in _validation_slice_rows()}
    public_lanes = set(get_command(validate_app).commands) - {"report"}

    assert "Current verified state:" in section
    assert recipes <= documented_recipes
    assert public_lanes == documented_slices
    for command in (
        "uv sync --group dev",
        "just",
        "just check",
        "uv run easycat docs",
        "uv run easycat docs --json",
        "uv run easycat explain json-schema",
        "uv run easycat doctor",
        "uv run easycat doctor --json",
        "uv run easycat doctor --env-file .env --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in contributing
        assert f"`{command}`" in section
    for token in (
        "CONTRIBUTING.md",
        "justfile",
        "easycat validate",
        "uv run easycat validate",
        "pyproject.toml",
        "flaky",
        "easycat.debug.testing",
        "tests/test_contributing.py",
        "tests/test_docs_index.py",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "development-loop table",
        "validation chooser table",
        "docs/onboarding maintenance map",
        "validation-slices table",
        "narrowest useful validation lane",
        "root README chooser",
        "docs route map",
        "examples matrix",
        "scaffold templates",
        "maintained Markdown links",
        "strict pytest markers",
        "provider/surface pairing",
        "flaky quarantine metadata",
        "validation slices deselect `flaky`",
        "RunBundle golden-test section",
    ):
        assert phrase in normalized_section
    for test_name in (
        "test_contributing_quick_start_points_to_docs_command",
        "test_contributing_validation_report_points_to_latest_artifact",
        "test_contributing_development_loop_just_recipes_stay_current",
        "test_contributing_development_loop_lists_public_just_recipes",
        "test_contributing_validation_slices_track_public_validate_lanes",
        "test_contributing_validation_slice_commands_use_repo_local_uv_run",
        "test_contributing_validation_chooser_tracks_slice_commands",
        "test_contributing_docs_onboarding_map_lists_resolving_guard_targets",
        "test_contributing_marker_taxonomy_lists_pytest_markers",
        "test_contributing_runbundle_helpers_track_public_testing_exports",
        "test_validation_plan_matches_contributor_quick_command",
    ):
        assert test_name in test_source
    assert "test_contributing_docs_route_matches_validation_report_commands" in docs_index_tests
