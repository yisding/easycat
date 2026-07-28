"""Shared subprocess and runtime-environment support for validation lanes."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SOURCE_CHECKOUT_ERROR = (
    "validation lanes require the EasyCat source checkout; run from the EasyCat "
    "repository root, or set EASYCAT_VALIDATION_PYTEST_COMMAND together with "
    "EASYCAT_VALIDATION_TEST_PATHS or EASYCAT_VALIDATION_TEST_ROOT"
)


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


def ensure_validation_source_checkout() -> None:
    """Require repository tests or explicit installed-wheel test overrides."""
    if _is_source_checkout_root(Path.cwd()):
        return

    pytest_override = os.environ.get("EASYCAT_VALIDATION_PYTEST_COMMAND")
    test_override = os.environ.get("EASYCAT_VALIDATION_TEST_PATHS") or os.environ.get(
        "EASYCAT_VALIDATION_TEST_ROOT"
    )
    if pytest_override and test_override:
        return

    raise ValidationSourceCheckoutError(_SOURCE_CHECKOUT_ERROR)


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


def pytest_command_prefix() -> list[str]:
    """Resolve the pytest executable used by validation lanes."""
    ensure_validation_source_checkout()
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
