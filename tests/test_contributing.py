from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV_LOOP_ROW_RE = re.compile(
    r"^\| (?P<task>[^|]+) \| `just (?P<recipe>[^` ]+)(?: [^`]*)?` "
    r"\| `(?P<raw>[^`]+)` \|$"
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


def test_contributing_development_loop_just_recipes_stay_current() -> None:
    recipes = _just_recipes()
    missing: list[str] = []
    stale_raw_commands: list[str] = []
    parameterized = {"sync-extra", "test-one"}

    for row in _development_loop_rows():
        recipe = row["recipe"]
        raw_command = row["raw"]
        if recipe not in recipes:
            missing.append(f"{row['task'].strip()}: just {recipe}")
            continue
        if "..." in raw_command:
            stale_raw_commands.append(f"{recipe}: raw command contains placeholder")
            continue
        if recipe not in parameterized and raw_command != recipes[recipe]:
            stale_raw_commands.append(
                f"{recipe}: CONTRIBUTING has {raw_command!r}, justfile has {recipes[recipe]!r}"
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
