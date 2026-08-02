"""Shared ``justfile`` parsing helpers.

The repo ``justfile`` is the single source of truth for developer task
commands, including the docs/onboarding ``guard-*`` recipes. Both the test
suite and ``scripts/regen_guard_commands.py`` parse it through this module.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_RECIPE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)(?:\s+[^:]*)?:(?P<deps>.*)$")
_VARIABLE_RE = re.compile(r"^(?:export\s+)?(?P<name>[A-Za-z0-9_-]+)\s*:=\s*(?P<value>.+)$")


def _is_recipe_line(line: str, match: re.Match[str] | None) -> bool:
    return match is not None and ":=" not in line and not line.startswith((" ", "\t", "#"))


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


def _strip_command_modifiers(command: str) -> str:
    """Remove Just's echo/error command modifiers from one logical command."""
    for prefix in ("@-", "-@", "@", "-"):
        if command.startswith(prefix):
            return command.removeprefix(prefix).lstrip()
    return command


def _continues_on_next_line(command: str) -> bool:
    """Whether ``command`` ends in an unescaped shell continuation backslash."""
    stripped = command.rstrip()
    trailing_slashes = len(stripped) - len(stripped.rstrip("\\"))
    return trailing_slashes % 2 == 1


def _without_continuation_slash(command: str) -> str:
    return command.rstrip()[:-1].rstrip()


def _fold_continued_recipe_lines(lines: Iterable[str]) -> Iterator[str]:
    """Yield logical recipe lines with shell continuations folded first."""
    continued_line: str | None = None
    for line in lines:
        if continued_line is not None:
            assert line.startswith((" ", "\t")) and line.strip(), (
                "justfile has an unterminated recipe line continuation"
            )
            physical_part = line.strip()
            continues = _continues_on_next_line(physical_part)
            if continues:
                physical_part = _without_continuation_slash(physical_part)
            continued_line = f"{continued_line} {physical_part}"
            if not continues:
                yield continued_line
                continued_line = None
            continue

        if line.startswith((" ", "\t")) and line.strip() and _continues_on_next_line(line):
            continued_line = _without_continuation_slash(line)
            continue
        yield line

    assert continued_line is None, "justfile has an unterminated recipe line continuation"


def just_recipe_commands(repo_root: Path) -> dict[str, str]:
    command_parts: dict[str, list[str]] = {}
    dependencies: dict[str, list[str]] = {}
    variables: dict[str, str] = {}
    current_recipe: str | None = None

    physical_lines = (repo_root / "justfile").read_text(encoding="utf-8").splitlines()
    for line in _fold_continued_recipe_lines(physical_lines):
        if not line.startswith((" ", "\t", "#")):
            variable = _parse_variable(line)
            if variable is not None:
                variables[variable[0]] = variable[1]
                current_recipe = None
                continue

        recipe_match = _RECIPE_RE.match(line)
        if recipe_match is not None and _is_recipe_line(line, recipe_match):
            current_recipe = recipe_match.group("name")
            command_parts[current_recipe] = []
            dependencies[current_recipe] = shlex.split(recipe_match.group("deps"))
            continue

        if current_recipe is not None and line.startswith((" ", "\t")) and line.strip():
            command = _strip_command_modifiers(line.strip())
            command_parts[current_recipe].append(_expand_variables(command, variables))

    assert command_parts, "justfile recipes were not found"

    def _resolve(recipe: str, stack: tuple[str, ...] = ()) -> str:
        if recipe in stack:
            cycle = " -> ".join((*stack, recipe))
            raise AssertionError(f"justfile recipe dependency cycle: {cycle}")
        parts = [
            *(_resolve(dep, (*stack, recipe)) for dep in dependencies[recipe]),
            *command_parts[recipe],
        ]
        return " && ".join(part for part in parts if part)

    return {recipe: _resolve(recipe) for recipe in command_parts}


def just_recipe_names(repo_root: Path) -> set[str]:
    return set(just_recipe_commands(repo_root))


def just_recipe_descriptions(repo_root: Path) -> dict[str, str]:
    """Map each recipe to the comment block written directly above it."""
    descriptions: dict[str, str] = {}
    pending: list[str] = []

    for line in (repo_root / "justfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            pending.append(line.lstrip("#").strip())
            continue
        recipe_match = _RECIPE_RE.match(line)
        if recipe_match is not None and _is_recipe_line(line, recipe_match):
            descriptions[recipe_match.group("name")] = " ".join(part for part in pending if part)
        pending = []

    return descriptions


@dataclass(frozen=True)
class GuardRecipe:
    """One docs/onboarding ``guard-*`` recipe parsed from the justfile."""

    name: str
    description: str
    command: str


def just_guard_recipes(repo_root: Path) -> tuple[GuardRecipe, ...]:
    """All ``guard-*`` recipes in justfile order with comments and raw commands."""
    commands = just_recipe_commands(repo_root)
    descriptions = just_recipe_descriptions(repo_root)
    guards = tuple(
        GuardRecipe(name=name, description=descriptions.get(name, ""), command=command)
        for name, command in commands.items()
        if name.startswith("guard-")
    )

    assert guards, "justfile guard recipes were not found"
    for guard in guards:
        assert guard.description, f"justfile recipe {guard.name} is missing a comment"
        assert guard.command, f"justfile recipe {guard.name} is missing a command"
    return guards
