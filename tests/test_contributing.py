from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from easycat.cli._app import (
    _DOCS_ONBOARDING_GUARD_COMMANDS,
    _DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
)
from tests._command_hints import command_hint_problems
from tests._justfile import just_recipe_commands
from tests._pytest_targets import pytest_target_problems

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


def _contributing_provider_section() -> str:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    return contributing.split("## Adding an STT or TTS provider", 1)[1].split(
        "## What's expected on a PR",
        1,
    )[0]


def _contributing_pr_expectations_section() -> str:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    return contributing.split("## What's expected on a PR", 1)[1]


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


def _pytest_target_problems(command: str, *, label: str) -> list[str]:
    return pytest_target_problems(command, repo_root=REPO_ROOT, label=label)


def test_contributing_quick_start_points_to_docs_command() -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    quick_start = contributing.split("## Quick start", 1)[1].split(
        "## The development loop",
        1,
    )[0]
    normalized = re.sub(r"\s+", " ", quick_start)

    assert "uv run easycat docs" in quick_start
    assert "uv run easycat docs --audience contributors" in quick_start
    assert "uv run easycat docs --audience contributors --json" in quick_start
    assert "uv run easycat docs --json" in quick_start
    assert "uv run easycat explain json-schema" in quick_start
    assert "maintained reader-facing map" in quick_start
    assert "narrow the map to contributor-facing routes" in normalized
    assert "automation needs that smaller route map" in normalized
    assert "Coding agent? Start at [llms.txt](llms.txt)" in normalized
    assert "when a script or coding agent" not in normalized
    assert "scripts/regen_llms_txt.py" in quick_start
    assert "CLI and scaffold commands" in quick_start
    assert "uv run easycat doctor" in quick_start
    assert "uv run easycat doctor --env-file .env" in quick_start
    assert "uv run easycat doctor --json" in quick_start
    assert "uv run easycat doctor --env-file .env --json" in quick_start
    assert "when those keys live in a project `.env`" in normalized
    assert "for parseable environment/check rows" in normalized
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
    assert "uv run easycat validate quick --json" in contributing
    assert "uv run easycat validate contracts --json" in contributing
    assert "uv run easycat validate release --json" in contributing
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in contributing
    assert "renders the latest saved report" in normalized
    assert "for the current validation run inside the standard CLI envelope" in normalized
    assert "re-emit a saved report inside that envelope" in normalized
    assert "when a script or coding agent" not in normalized
    assert ".easycat/validation/runs/<run_id>/report.json" in contributing
    assert "uv run easycat validate report <path>" not in contributing


def test_contributing_docs_onboarding_map_lists_resolving_guard_targets() -> None:
    section = _contributing_docs_onboarding_section()
    normalized = re.sub(r"\s+", " ", section)
    recipes = just_recipe_commands(REPO_ROOT)
    guard_recipes = (
        "guard-docs",
        "guard-teaching",
        "guard-examples",
        "guard-templates",
        "guard-contributing",
        "guard-validation",
        "guard-contracts",
        "guard-ops",
        "guard-markdown",
    )

    assert "narrow guard that owns that surface" in normalized
    assert "Then run `uv run easycat validate quick` before a PR" in normalized
    assert "If `just` is not installed" in section
    assert "[the development loop](#the-development-loop)" in section
    assert "`uv run pytest ...` command behind each guard" in section
    for phrase in (
        "Root README chooser, docs route map, public API docs, or CLI JSON envelopes",
        "Teaching ladder chapters or generated blocks",
        "Examples chooser or command matrix",
        "Scaffold templates or template catalog",
        "Contributor and validation guidance",
        "Validation workflow, validation reference, or validate CLI behavior",
        "Provider protocols, cassettes, contract matrix, or bridge event grammar",
        "Operator deployment, observability, or journal durability docs",
        "Markdown links in maintained docs",
        (
            "Root onboarding links, README e2e coverage, install guidance, "
            "command-hint extraction, `easycat docs`, public API import-surface docs, "
            "JSON route entries, and shared CLI `--json` envelope contracts"
        ),
        "Chapter prerequisites, generated auto blocks, diagram alignment, and learner route hints",
        (
            "Example README matrix, support files, setup/install/env guidance, "
            "script smoke checks, and docs-route hints"
        ),
        (
            "Generated README sections, line budgets, init happy paths, overwrite safety, "
            "schema rejection paths, catalog text, catalog JSON, next-step commands, "
            "generated project smoke, and generated project secret/artifact hygiene"
        ),
        (
            "`justfile` parity, agent guide command, source-layout, and architecture hints, "
            "validation lanes, docs-route hints, and plan current-state evidence"
        ),
        (
            "The `docs/validation.md` workflow, validation reference route hints, "
            "validation plan current state, validate CLI reports, JSON envelopes, "
            "latency options, and error handling"
        ),
        (
            "Provider contract docs-route hints, contributor provider guidance, "
            "offline contract suite, cassette redaction/replay, schema fingerprints, "
            "bridge contracts, and provider wiring matrix"
        ),
        (
            "Docker deployment guide, operator docs-route hints, journal CLI entry points, "
            "debugger UI docs, OpenTelemetry facade docs, debug bundle CLI behavior, "
            "and SQLite journal durability"
        ),
        "Local links, anchors, and docs-route Markdown targets",
    ):
        assert phrase in section

    for recipe in guard_recipes:
        assert f"`just {recipe}`" in section
        command = recipes[recipe]
        assert command.startswith("uv run pytest ")
        problems = _pytest_target_problems(
            command,
            label=f"CONTRIBUTING.md docs guard `just {recipe}`",
        )
        assert not problems, "\n".join(problems)


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


def test_docs_route_guard_commands_match_contributing_table() -> None:
    rows = {row["recipe"]: row["raw"] for row in _development_loop_rows()}
    guard_recipes = (
        "guard-docs",
        "guard-teaching",
        "guard-examples",
        "guard-templates",
        "guard-contributing",
        "guard-validation",
        "guard-contracts",
        "guard-ops",
        "guard-markdown",
    )
    expected_guard_commands = tuple(f"just {recipe}" for recipe in guard_recipes)
    expected_raw_commands = tuple(rows[recipe] for recipe in guard_recipes)

    assert _DOCS_ONBOARDING_GUARD_COMMANDS == expected_guard_commands
    assert _DOCS_ONBOARDING_RAW_GUARD_COMMANDS == expected_raw_commands


def test_contributing_development_loop_pytest_targets_resolve() -> None:
    problems: list[str] = []

    for row in _development_loop_rows():
        raw_command = row["raw"]
        if "uv run pytest" not in raw_command:
            continue
        problems.extend(
            _pytest_target_problems(
                raw_command,
                label=f"CONTRIBUTING.md development loop {row['task'].strip()}",
            )
        )

    assert not problems, "CONTRIBUTING.md pytest targets are stale:\n" + "\n".join(problems)


def test_contributing_development_loop_command_hints_are_locally_valid() -> None:
    commands = tuple(row["raw"] for row in _development_loop_rows())
    problems = command_hint_problems(
        [
            {
                "label": "CONTRIBUTING.md development loop",
                "path": "CONTRIBUTING.md#the-development-loop",
                "audience": "contributors",
                "description": "Copyable raw development-loop commands.",
                "commands": commands,
            }
        ],
        repo_root=REPO_ROOT,
    )

    assert commands
    assert not problems, "CONTRIBUTING.md development-loop commands are stale:\n" + "\n".join(
        problems
    )


def test_contributing_pytest_target_validator_checks_node_ids() -> None:
    problems = _pytest_target_problems(
        (
            "uv run pytest tests/test_install_guidance.py -k 'agent_guide or claude_' && "
            "uv run pytest tests/test_contributing.py::missing_test "
            "tests/test_contributing_missing.py"
        ),
        label="Broken pytest command",
    )

    assert not any("agent_guide or claude_" in problem for problem in problems)
    assert (
        "Broken pytest command: missing pytest node tests/test_contributing.py::missing_test"
        in problems
    )
    assert "Broken pytest command: missing pytest target tests/test_contributing_missing.py" in (
        problems
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


def test_contributing_provider_section_points_to_contract_map() -> None:
    section = _contributing_provider_section()
    normalized = re.sub(r"\s+", " ", section)

    for phrase in (
        "EasyCat uses **registries**, not inheritance",
        "ProviderSurfaceContract",
        "[provider contract map](tests/contracts/README.md)",
        "protocol cassettes",
        "schema fingerprints",
        "bridge event grammar",
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "standard CLI envelope",
        "uv run pytest tests/contracts",
        "uv run pytest tests/integration/test_provider_contract_matrix.py",
    ):
        assert phrase in normalized


def test_contributing_pr_expectations_point_to_raw_command_fallbacks() -> None:
    section = _contributing_pr_expectations_section()
    rows = {row["recipe"]: row["raw"] for row in _development_loop_rows()}

    assert "If `just` is not installed" in section
    assert "[the development loop](#the-development-loop)" in section
    assert "matching raw command" in section
    for recipe in ("check", "typecheck", "typecheck-all", "typecheck-fast", "cov"):
        assert f"`just {recipe}`" in section
        assert rows[recipe], f"CONTRIBUTING.md missing raw command for just {recipe}"


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
        "uv run easycat doctor --env-file .env",
        "uv run easycat doctor --json",
        "uv run easycat doctor --env-file .env --json",
        "uv run easycat validate quick --json",
        "uv run easycat validate contracts --json",
        "uv run easycat validate release --json",
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
    for recipe in (
        "guard-docs",
        "guard-teaching",
        "guard-examples",
        "guard-templates",
        "guard-contributing",
        "guard-validation",
        "guard-contracts",
        "guard-ops",
        "guard-markdown",
    ):
        assert f"`{recipe}`" in section
    for phrase in (
        "development-loop table",
        "validation chooser table",
        "docs/onboarding maintenance map",
        "validation-slices table",
        "narrowest useful validation lane",
        "root README chooser",
        "docs route map",
        "public API import-surface docs",
        "shared CLI `--json` envelope contracts",
        "teaching ladder",
        "examples matrix",
        "scaffold templates",
        "validation reference",
        "provider contract docs",
        "operator docs",
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
    assert "test_contributing_docs_route_matches_validation_lane_commands" in docs_index_tests
