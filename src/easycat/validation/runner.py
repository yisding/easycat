from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from easycat.validation.report import (
    ArtifactRef,
    GitMetadata,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationRun,
    redact_text,
)

VALIDATION_SELECTORS = {
    "quick": "not integration_socket and not integration_live and not slow and not flaky",
    "socket": "integration_socket and not integration_live and not flaky",
}

PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "CARTESIA_API_KEY",
)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ValidationRunResult:
    run: ValidationRun
    run_dir: Path
    report_path: Path
    exit_code: int


CommandRunner = Callable[[list[str]], CommandResult]


def validation_exit_code_from_pytest(pytest_exit_code: int) -> int:
    return 0 if pytest_exit_code == 0 else 1


def run_validation_slice(
    slice_name: str,
    *,
    artifacts_dir: str | Path = ".easycat/validation",
    command_runner: CommandRunner | None = None,
    started_at: datetime | None = None,
) -> ValidationRunResult:
    if slice_name not in VALIDATION_SELECTORS:
        known = ", ".join(sorted(VALIDATION_SELECTORS))
        raise ValueError(f"unknown validation slice {slice_name!r}; expected one of: {known}")

    command_runner = command_runner or _run_subprocess
    started_at = started_at or datetime.now(UTC)
    artifacts_root = Path(artifacts_dir)
    run_id = _make_run_id(slice_name, started_at)
    run_dir = artifacts_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    junit_path = run_dir / "junit.xml"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    report_path = run_dir / "report.json"

    command = [
        "uv",
        "run",
        "pytest",
        "-q",
        f"--junitxml={junit_path}",
        "-m",
        VALIDATION_SELECTORS[slice_name],
    ]

    started_monotonic = time.perf_counter()
    result = command_runner(command)
    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)

    stdout_path.write_text(redact_text(result.stdout))
    stderr_path.write_text(redact_text(result.stderr))

    exit_code = validation_exit_code_from_pytest(result.exit_code)
    status = "pass" if exit_code == 0 else "fail"

    check_artifacts: dict[str, ArtifactRef] = {
        "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
        "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
    }
    if junit_path.exists():
        check_artifacts["junit"] = ArtifactRef(kind="junit", path=str(junit_path))

    artifacts: dict[str, ArtifactRef] = {
        "report": ArtifactRef(kind="validation_report", path=str(report_path)),
        **check_artifacts,
    }

    failures = []
    if exit_code != 0:
        failures.append(
            ValidationFailure(
                name=f"pytest.{slice_name}",
                message=result.stderr or result.stdout or f"pytest exited {result.exit_code}",
            )
        )

    run = ValidationRun(
        run_id=run_id,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes={"pytest": result.exit_code},
        git=_collect_git_metadata(),
        environment=_collect_environment_metadata(),
        checks=[
            ValidationCheck(
                name=f"pytest.{slice_name}",
                status=status,
                duration_s=duration_s,
                command=command,
                artifacts=check_artifacts,
            )
        ],
        failures=failures,
        artifacts=artifacts,
    )

    _write_atomic(report_path, run.to_json())
    _write_atomic(artifacts_root / "latest.json", run.to_json())
    return ValidationRunResult(
        run=run,
        run_dir=run_dir,
        report_path=report_path,
        exit_code=exit_code,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    command_runner: CommandRunner | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run EasyCat validation slices.")
    parser.add_argument("slice", choices=sorted(VALIDATION_SELECTORS))
    parser.add_argument(
        "--artifacts-dir",
        default=".easycat/validation",
        help="Directory where validation reports and logs are written.",
    )
    args = parser.parse_args(argv)

    result = run_validation_slice(
        args.slice,
        artifacts_dir=args.artifacts_dir,
        command_runner=command_runner,
    )
    print(f"{args.slice}: {result.run.status}; report: {result.report_path}")
    return result.exit_code


def _run_subprocess(command: list[str]) -> CommandResult:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _make_run_id(slice_name: str, started_at: datetime) -> str:
    timestamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return f"{timestamp}-{slice_name}-{suffix}"


def _collect_git_metadata() -> GitMetadata:
    return GitMetadata(
        sha=_git_output(["rev-parse", "--short", "HEAD"]),
        branch=_git_output(["branch", "--show-current"]),
        dirty=bool(_git_output(["status", "--porcelain"])),
    )


def _git_output(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _collect_environment_metadata() -> ValidationEnvironment:
    return ValidationEnvironment(
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        platform=platform.platform(),
        ci=bool(os.environ.get("CI")),
        env_vars={name: name in os.environ for name in PROVIDER_ENV_VARS},
    )


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(text)
    os.replace(tmp_path, path)
