from __future__ import annotations

import re
import shlex
from pathlib import Path

_RECIPE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)(?:\s+[^:]*)?:(?P<deps>.*)$")
_VARIABLE_RE = re.compile(r"^(?:export\s+)?(?P<name>[A-Za-z0-9_-]+)\s*:=\s*(?P<value>.+)$")


def _parse_variable(line: str) -> tuple[str, str] | None:
    match = _VARIABLE_RE.match(line)
    if match is None:
        return None
    value = match.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return match.group("name"), value


def _expand_variables(command: str, variables: dict[str, str]) -> str:
    def _substitute(match: re.Match[str]) -> str:
        name = match.group("name")
        return variables.get(name, match.group(0))

    return re.sub(r"\{\{\s*(?P<name>[a-z][A-Za-z0-9_-]*)\s*\}\}", _substitute, command)


def just_recipe_commands(repo_root: Path) -> dict[str, str]:
    commands: dict[str, str] = {}
    dependencies: dict[str, list[str]] = {}
    variables: dict[str, str] = {}
    current_recipe: str | None = None

    for line in (repo_root / "justfile").read_text(encoding="utf-8").splitlines():
        if not line.startswith((" ", "\t", "#")):
            variable = _parse_variable(line)
            if variable is not None:
                variables[variable[0]] = variable[1]
                current_recipe = None
                continue

        recipe_match = _RECIPE_RE.match(line)
        if recipe_match and ":=" not in line and not line.startswith((" ", "\t", "#")):
            current_recipe = recipe_match.group("name")
            commands[current_recipe] = ""
            dependencies[current_recipe] = shlex.split(recipe_match.group("deps"))
            continue

        if current_recipe is not None and line.startswith((" ", "\t")) and line.strip():
            commands[current_recipe] = _expand_variables(line.strip().removeprefix("@"), variables)
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
