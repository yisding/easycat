from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from typer.main import get_command

from easycat.cli._app import (
    _DOCS_LINKS,
    _available_docs_audience_filters,
    _register_commands,
    app,
)
from scripts._justfile import just_recipe_commands
from tests._pytest_targets import pytest_target_problems

INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def strip_shell_comment(command: str) -> str:
    return re.sub(r"\s+#.*$", "", command).strip()


def documented_command_lines(section: str, *, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    commands: list[str] = []
    seen: set[str] = set()

    def add(raw_command: str) -> None:
        command = strip_shell_comment(raw_command)
        if command.startswith(prefixes) and command not in seen:
            seen.add(command)
            commands.append(command)

    for line in section.splitlines():
        add(line.strip())

    return tuple(commands)


def documented_commands(section: str, *, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    commands = list(documented_command_lines(section, prefixes=prefixes))
    seen = set(commands)

    def add(raw_command: str) -> None:
        command = strip_shell_comment(raw_command)
        if command.startswith(prefixes) and command not in seen:
            seen.add(command)
            commands.append(command)

    for command in INLINE_CODE_RE.findall(section):
        add(command.strip())

    return tuple(commands)


def command_hint_variants(command: str) -> set[str]:
    variants = {command, command.replace("PATH", "<path>")}

    if command.startswith("easycat "):
        variants.update(f"uv run {variant}" for variant in tuple(variants))
    if command.startswith("uv run easycat "):
        variants.update(variant.removeprefix("uv run ") for variant in tuple(variants))

    return variants


def command_hint_problems(entries: list[dict[str, object]], *, repo_root: Path) -> list[str]:
    command_tree = _easycat_command_tree()
    just_recipes = just_recipe_commands(repo_root)
    problems: list[str] = []

    for entry in entries:
        label = str(entry["label"])
        for command in entry.get("commands", ()):
            for tokens in _command_hint_token_parts(str(command), label=label, problems=problems):
                if not tokens:
                    problems.append(f"{label}: empty command hint")
                    continue
                command_part = shlex.join(tokens)
                _validate_command_hint_tokens(
                    label=label,
                    command=command_part,
                    repo_root=repo_root,
                    command_tree=command_tree,
                    just_recipes=just_recipes,
                    tokens=tokens,
                    problems=problems,
                )

    return problems


def _command_hint_token_parts(
    command: str,
    *,
    label: str,
    problems: list[str],
) -> list[list[str]]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        problems.append(f"{label}: invalid command hint {command!r}: {exc}")
        return []

    if "&&" not in tokens:
        return [tokens]

    parts: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "&&":
            if not current:
                problems.append(f"{label}: empty command before && in {command!r}")
            else:
                parts.append(current)
            current = []
            continue
        current.append(token)

    if current:
        parts.append(current)
    else:
        problems.append(f"{label}: empty command after && in {command!r}")

    return parts


def _validate_command_hint_tokens(
    *,
    label: str,
    command: str,
    repo_root: Path,
    command_tree: dict[str, set[str] | None],
    just_recipes: dict[str, str],
    tokens: list[str],
    problems: list[str],
) -> None:
    match tokens:
        case ["easycat", subcommand, *args]:
            _validate_easycat_command_hint(
                label=label,
                repo_root=repo_root,
                command_tree=command_tree,
                subcommand=subcommand,
                args=args,
                problems=problems,
            )
        case ["uvx", "ty", "check", *args]:
            _validate_ty_check_hint(label=label, repo_root=repo_root, args=args, problems=problems)
        case ["uv", "run", *args]:
            _validate_uv_run_hint(
                label=label,
                command=command,
                repo_root=repo_root,
                command_tree=command_tree,
                args=args,
                problems=problems,
            )
        case ["uv", "sync", *args]:
            _validate_uv_sync_hint(
                label=label,
                repo_root=repo_root,
                args=args,
                problems=problems,
            )
        case ["python", "-m", "http.server", *args]:
            _validate_http_server_hint(
                label=label,
                repo_root=repo_root,
                args=args,
                problems=problems,
            )
        case ["just", *args]:
            _validate_just_hint(
                label=label,
                just_recipes=just_recipes,
                args=args,
                problems=problems,
            )
        case ["docker", "compose", *args]:
            _validate_docker_compose_hint(
                label=label,
                repo_root=repo_root,
                args=args,
                problems=problems,
            )
        case _:
            problems.append(f"{label}: unsupported command hint {command!r}")


def _easycat_command_tree() -> dict[str, set[str] | None]:
    _register_commands()
    root_command = get_command(app)
    return {
        name: set(nested_commands) if nested_commands is not None else None
        for name, command in root_command.commands.items()
        for nested_commands in (getattr(command, "commands", None),)
    }


def _docs_audience_hint_values() -> set[str]:
    filters = set(_available_docs_audience_filters())
    return (
        filters
        | {value.replace("-", "_") for value in filters}
        | {entry["audience"] for entry in _DOCS_LINKS}
    )


def _declared_optional_dependency_extras(repo_root: Path) -> set[str]:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["project"]["optional-dependencies"])


def _declared_dependency_groups(repo_root: Path) -> set[str]:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["dependency-groups"])


def _is_placeholder_value(value: str) -> bool:
    return value in {"PATH", "DIR", "FILE", "NAME"} or (
        value.startswith("<") and value.endswith(">")
    )


def _option_values(
    args: list[str],
    option: str,
    *,
    label: str,
    missing_message: str,
    problems: list[str],
) -> list[str]:
    values: list[str] = []
    prefix = f"{option}="

    for index, arg in enumerate(args):
        if arg == option:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                problems.append(f"{label}: {missing_message}")
                continue
            values.append(args[index + 1])
        elif arg.startswith(prefix):
            value = arg.split("=", 1)[1]
            if not value:
                problems.append(f"{label}: {missing_message}")
                continue
            values.append(value)

    return values


def _validate_docs_command_hint(*, label: str, args: list[str], problems: list[str]) -> None:
    valid_audiences = _docs_audience_hint_values()

    for value in _option_values(
        args,
        "--audience",
        label=label,
        missing_message="docs audience hint missing value",
        problems=problems,
    ):
        if value not in valid_audiences:
            problems.append(f"{label}: unknown docs audience hint {value}")


def _validate_env_file_values(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    context: str,
    problems: list[str],
) -> None:
    for env_file in _option_values(
        args,
        "--env-file",
        label=label,
        missing_message=f"{context} env-file hint missing value",
        problems=problems,
    ):
        if not (repo_root / env_file).parent.is_dir():
            problems.append(f"{label}: missing {context} env-file directory {env_file}")


def _validate_uv_sync_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    declared_extras = _declared_optional_dependency_extras(repo_root)
    declared_groups = _declared_dependency_groups(repo_root)

    for extra in _option_values(
        args,
        "--extra",
        label=label,
        missing_message="uv sync extra hint missing value",
        problems=problems,
    ):
        if extra not in declared_extras and not _is_placeholder_value(extra):
            problems.append(f"{label}: unknown uv sync extra {extra}")

    for group in _option_values(
        args,
        "--group",
        label=label,
        missing_message="uv sync group hint missing value",
        problems=problems,
    ):
        if group not in declared_groups and not _is_placeholder_value(group):
            problems.append(f"{label}: unknown uv sync group {group}")


def _validate_just_hint(
    *,
    label: str,
    just_recipes: dict[str, str],
    args: list[str],
    problems: list[str],
) -> None:
    if not args:
        return

    recipe = args[0]
    if recipe not in just_recipes:
        problems.append(f"{label}: unknown just recipe {recipe}")


def _validate_http_server_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    for directory in _option_values(
        args,
        "--directory",
        label=label,
        missing_message="http.server hint missing directory",
        problems=problems,
    ):
        if not (repo_root / directory).is_dir():
            problems.append(f"{label}: missing http.server directory {directory}")


def _validate_docker_compose_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    has_compose_file_flag = any(arg == "-f" or arg.startswith("-f=") for arg in args)
    compose_files = _option_values(
        args,
        "-f",
        label=label,
        missing_message="docker compose hint missing compose file",
        problems=problems,
    )

    if not has_compose_file_flag:
        problems.append(f"{label}: docker compose hint missing -f")

    for compose_file in compose_files:
        if not (repo_root / compose_file).exists():
            problems.append(f"{label}: missing compose file {compose_file}")

    for env_file in _option_values(
        args,
        "--env-file",
        label=label,
        missing_message="docker compose env-file hint missing value",
        problems=problems,
    ):
        if not (repo_root / env_file).parent.is_dir():
            problems.append(f"{label}: missing docker compose env-file directory {env_file}")


def _path_targets(args: list[str], *, options_with_values: set[str]) -> list[str]:
    targets: list[str] = []
    skip_next = False

    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            targets.extend(args[index + 1 :])
            break
        if arg in options_with_values:
            skip_next = True
            continue
        if any(arg.startswith(f"{option}=") for option in options_with_values):
            continue
        if arg.startswith("-"):
            continue
        targets.append(arg)

    return targets


def _validate_repo_path_targets(
    *,
    label: str,
    repo_root: Path,
    context: str,
    targets: list[str],
    problems: list[str],
) -> None:
    for target in targets:
        if _is_placeholder_value(target):
            continue
        if not (repo_root / target).exists():
            problems.append(f"{label}: missing {context} target {target}")


def _validate_ruff_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    if not args:
        problems.append(f"{label}: missing ruff command")
        return

    command, *command_args = args
    if command not in {"check", "format"}:
        problems.append(f"{label}: unsupported ruff command {command}")
        return

    targets = _path_targets(
        command_args,
        options_with_values={
            "--config",
            "--exclude",
            "--extend-exclude",
            "--ignore",
            "--line-length",
            "--output-format",
            "--select",
            "--stdin-filename",
            "--target-version",
        },
    )
    _validate_repo_path_targets(
        label=label,
        repo_root=repo_root,
        context=f"ruff {command}",
        targets=targets,
        problems=problems,
    )


def _validate_mypy_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    targets = _path_targets(
        args,
        options_with_values={
            "--cache-dir",
            "--command",
            "--config-file",
            "--follow-imports",
            "--module",
            "--package",
            "--python-version",
            "-c",
            "-m",
            "-p",
        },
    )
    _validate_repo_path_targets(
        label=label,
        repo_root=repo_root,
        context="mypy",
        targets=targets,
        problems=problems,
    )


def _validate_ty_check_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    targets = _path_targets(
        args,
        options_with_values={"--config", "--python", "--python-platform", "--python-version"},
    )
    _validate_repo_path_targets(
        label=label,
        repo_root=repo_root,
        context="ty check",
        targets=targets,
        problems=problems,
    )


def _validate_pre_commit_hint(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> None:
    if not args or args[0] != "run":
        command = shlex.join(["pre-commit", *args])
        problems.append(f"{label}: unsupported pre-commit command {command!r}")
        return
    if not (repo_root / ".pre-commit-config.yaml").exists():
        problems.append(f"{label}: missing pre-commit config .pre-commit-config.yaml")


def _uv_run_command_tokens(
    *,
    label: str,
    repo_root: Path,
    args: list[str],
    problems: list[str],
) -> list[str]:
    index = 0

    while index < len(args):
        arg = args[index]
        if arg == "--env-file":
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                problems.append(f"{label}: uv run env-file hint missing value")
                index += 1
                continue
            env_file = args[index + 1]
            if not (repo_root / env_file).parent.is_dir():
                problems.append(f"{label}: missing uv run env-file directory {env_file}")
            index += 2
            continue
        if arg.startswith("--env-file="):
            env_file = arg.split("=", 1)[1]
            if not env_file:
                problems.append(f"{label}: uv run env-file hint missing value")
                index += 1
                continue
            if not (repo_root / env_file).parent.is_dir():
                problems.append(f"{label}: missing uv run env-file directory {env_file}")
            index += 1
            continue
        if arg.startswith("-"):
            problems.append(f"{label}: unsupported uv run option {arg}")
            return []
        return args[index:]

    problems.append(f"{label}: missing uv run command")
    return []


def _validate_uv_run_hint(
    *,
    label: str,
    command: str,
    repo_root: Path,
    command_tree: dict[str, set[str] | None],
    args: list[str],
    problems: list[str],
) -> None:
    run_tokens = _uv_run_command_tokens(
        label=label,
        repo_root=repo_root,
        args=args,
        problems=problems,
    )
    if not run_tokens:
        return

    match run_tokens:
        case ["easycat", subcommand, *sub_args]:
            _validate_easycat_command_hint(
                label=label,
                repo_root=repo_root,
                command_tree=command_tree,
                subcommand=subcommand,
                args=sub_args,
                problems=problems,
            )
        case ["python", script, *_]:
            if not (repo_root / script).exists():
                problems.append(f"{label}: missing python script {script}")
        case ["uvicorn", target, *_]:
            module_name = target.partition(":")[0]
            if not module_name:
                problems.append(f"{label}: missing uvicorn module target")
                return
            module_path = repo_root / Path(*module_name.split(".")).with_suffix(".py")
            if not module_path.exists():
                problems.append(f"{label}: missing uvicorn module target {target}")
        case ["pytest", *_]:
            problems.extend(pytest_target_problems(command, repo_root=repo_root, label=label))
        case ["ruff", *_]:
            _validate_ruff_hint(
                label=label, repo_root=repo_root, args=run_tokens[1:], problems=problems
            )
        case ["mypy", *args]:
            _validate_mypy_hint(label=label, repo_root=repo_root, args=args, problems=problems)
        case ["pre-commit", *args]:
            _validate_pre_commit_hint(
                label=label,
                repo_root=repo_root,
                args=args,
                problems=problems,
            )
        case _:
            problems.append(f"{label}: unsupported command hint {command!r}")


def _validate_easycat_command_hint(
    *,
    label: str,
    repo_root: Path,
    command_tree: dict[str, set[str] | None],
    subcommand: str,
    args: list[str],
    problems: list[str],
) -> None:
    if subcommand not in command_tree:
        problems.append(f"{label}: unknown easycat command {subcommand}")
        return

    if subcommand == "docs":
        _validate_docs_command_hint(label=label, args=args, problems=problems)
    if subcommand == "doctor":
        _validate_env_file_values(
            label=label,
            repo_root=repo_root,
            args=args,
            context="easycat doctor",
            problems=problems,
        )

    nested_commands = command_tree[subcommand]
    if nested_commands is None:
        return

    if not args or args[0].startswith("-"):
        problems.append(f"{label}: missing easycat {subcommand} command")
        return

    nested_command = args[0]
    if nested_command not in nested_commands:
        problems.append(f"{label}: unknown easycat {subcommand} command {nested_command}")
