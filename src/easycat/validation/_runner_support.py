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
from typing import Literal, Protocol

_SOURCE_CHECKOUT_ERROR = (
    "validation lanes require the EasyCat source checkout; run from the EasyCat "
    "repository root, or set EASYCAT_VALIDATION_PYTEST_COMMAND together with "
    "{test_override_hint}"
)
_TestOverrideMode = Literal["paths", "root", "both"]


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


class ValidationSourceCheckoutError(RuntimeError):
    """Raised when a repository validation lane has no repository tests."""


def _is_source_checkout_root(path: Path) -> bool:
    return (
        (path / "pyproject.toml").is_file()
        and (path / "src" / "easycat").is_dir()
        and (path / "tests").is_dir()
    )


def ensure_validation_source_checkout(*, test_override_mode: _TestOverrideMode = "both") -> None:
    """Require repository tests or explicit installed-wheel test overrides."""
    if _is_source_checkout_root(Path.cwd()):
        return

    pytest_override = os.environ.get("EASYCAT_VALIDATION_PYTEST_COMMAND")
    required_test_overrides = {
        "paths": ("EASYCAT_VALIDATION_TEST_PATHS",),
        "root": ("EASYCAT_VALIDATION_TEST_ROOT",),
        "both": (
            "EASYCAT_VALIDATION_TEST_PATHS",
            "EASYCAT_VALIDATION_TEST_ROOT",
        ),
    }[test_override_mode]
    if pytest_override and all(os.environ.get(name) for name in required_test_overrides):
        return

    override_hint = " and ".join(required_test_overrides)
    raise ValidationSourceCheckoutError(
        _SOURCE_CHECKOUT_ERROR.format(test_override_hint=override_hint)
    )


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


def pytest_command_prefix(*, test_override_mode: _TestOverrideMode = "both") -> list[str]:
    """Resolve the pytest executable used by validation lanes."""
    ensure_validation_source_checkout(test_override_mode=test_override_mode)
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
