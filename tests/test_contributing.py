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


def _render_recipe_command(command: str, args_text: str | None) -> str:
    args = shlex.split(args_text or "")
    rendered = command

    if "{{ prepend('--extra ', EXTRAS) }}" in rendered:
        extras = " ".join(f"--extra {extra}" for extra in args)
        return rendered.replace("{{ prepend('--extra ', EXTRAS) }}", extras)

    if args:
        rendered = re.sub(r'"?\{\{\s*[A-Z_]+\s*\}\}"?', args[0], rendered)

    return rendered


def test_contributing_quick_start_points_to_docs_command() -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    quick_start = contributing.split("## Quick start", 1)[1].split(
        "## The development loop",
        1,
    )[0]

    assert "uv run easycat docs" in quick_start
    assert "maintained reader-facing map" in quick_start
    assert "uv run easycat doctor" in quick_start
    assert "before debugging tests or examples" in quick_start


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


def test_contributing_validation_slice_commands_use_repo_local_uv_run() -> None:
    stale = [
        f"{row['slice']}: {row['command']}"
        for row in _validation_slice_rows()
        if not row["command"].startswith("uv run easycat validate ")
    ]

    assert not stale, "CONTRIBUTING.md validation commands should use uv run: " + "; ".join(stale)


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
