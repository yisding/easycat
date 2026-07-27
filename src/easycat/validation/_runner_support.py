"""Shared subprocess and runtime-environment support for validation lanes."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result of one validation subprocess."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Callable boundary for injected validation subprocess runners."""

    def __call__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> CommandResult: ...


def run_subprocess(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> CommandResult:
    """Run a validation command and capture its text streams without raising."""
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd is not None else None,
    )
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_timed_command(
    command_runner: CommandRunner,
    command: list[str],
    *,
    env: Mapping[str, str],
) -> tuple[CommandResult, float, datetime]:
    """Run one injected command and capture monotonic duration plus finish time."""
    started_monotonic = time.perf_counter()
    result = command_runner(command, env=env)
    return result, time.perf_counter() - started_monotonic, datetime.now(UTC)


def pytest_command_prefix() -> list[str]:
    """Resolve the pytest executable used by validation lanes."""
    raw = os.environ.get("EASYCAT_VALIDATION_PYTEST_COMMAND")
    return shlex.split(raw) if raw else ["uv", "run", "pytest"]


def validation_test_paths() -> list[str]:
    """Resolve optional test roots supplied by an installed-wheel release run."""
    raw = os.environ.get("EASYCAT_VALIDATION_TEST_PATHS")
    return [path for path in raw.split(os.pathsep) if path] if raw else []


def resolve_validation_test_arg(arg: str) -> str:
    """Resolve a repository-relative test target against an override root."""
    test_root = os.environ.get("EASYCAT_VALIDATION_TEST_ROOT")
    if not test_root or arg.startswith("/") or not arg.startswith("tests/"):
        return arg
    return str(Path(test_root) / arg.removeprefix("tests/"))


def validation_exit_code_from_pytest(pytest_exit_code: int) -> int:
    """Normalize every nonzero pytest outcome into a failed validation run."""
    return 0 if pytest_exit_code == 0 else 1
