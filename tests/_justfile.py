from __future__ import annotations

import re
import shlex
from pathlib import Path

_RECIPE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)(?:\s+[^:]*)?:(?P<deps>.*)$")


def just_recipe_commands(repo_root: Path) -> dict[str, str]:
    commands: dict[str, str] = {}
    dependencies: dict[str, list[str]] = {}
    current_recipe: str | None = None

    for line in (repo_root / "justfile").read_text(encoding="utf-8").splitlines():
        recipe_match = _RECIPE_RE.match(line)
        if recipe_match and ":=" not in line and not line.startswith((" ", "\t", "#")):
            current_recipe = recipe_match.group("name")
            commands[current_recipe] = ""
            dependencies[current_recipe] = shlex.split(recipe_match.group("deps"))
            continue

        if current_recipe is not None and line.startswith((" ", "\t")) and line.strip():
            commands[current_recipe] = line.strip().removeprefix("@")
            current_recipe = None

    assert commands, "justfile recipes were not found"

    def _resolve(recipe: str, stack: tuple[str, ...] = ()) -> str:
        if recipe in stack:
            cycle = " -> ".join((*stack, recipe))
            raise AssertionError(f"justfile recipe dependency cycle: {cycle}")
        if commands[recipe]:
            return commands[recipe]
        deps = dependencies[recipe]
        if deps:
            return " && ".join(_resolve(dep, (*stack, recipe)) for dep in deps)
        return ""

    return {recipe: _resolve(recipe) for recipe in commands}


def just_recipe_names(repo_root: Path) -> set[str]:
    return set(just_recipe_commands(repo_root))
