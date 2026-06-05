from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV_LOOP_ROW_RE = re.compile(
    r"^\| (?P<task>[^|]+) \| `just (?P<recipe>[^` ]+)(?P<args>[^`]*)?` "
    r"\| `(?P<raw>[^`]+)` \|$"
)
_VALIDATION_ROW_RE = re.compile(
    r"^\| `(?P<slice>[^`]+)` \| `(?P<command>[^`]+)` \| (?P<markers>[^|]+) \|$"
)
_RECIPE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)(?:\s+[^:]*)?:")


def _just_recipes() -> dict[str, str]:
    recipes: dict[str, str] = {}
    current_recipe: str | None = None

    for line in (REPO_ROOT / "justfile").read_text(encoding="utf-8").splitlines():
        recipe_match = _RECIPE_RE.match(line)
        if recipe_match and not line.startswith((" ", "\t", "#")):
            current_recipe = recipe_match.group("name")
            recipes[current_recipe] = ""
            continue

        if current_recipe is not None and line.startswith((" ", "\t")) and line.strip():
            recipes[current_recipe] = line.strip().removeprefix("@")
            current_recipe = None

    return recipes


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


def test_contributing_development_loop_just_recipes_stay_current() -> None:
    recipes = _just_recipes()
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


def test_contributing_development_loop_lists_validation_just_recipes() -> None:
    recipes = _just_recipes()
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


def test_validation_plan_matches_contributor_quick_command() -> None:
    rows = {row["slice"]: row["command"] for row in _validation_slice_rows()}
    tasks = (REPO_ROOT / "plan" / "validation" / "tasks.md").read_text(encoding="utf-8")

    assert rows["quick"] in tasks
