"""Shared prologue/epilogue for every validation lane module.

The lanes in :mod:`easycat.validation.runner` and
:mod:`easycat.validation._slice_runner` share a run-id/run-dir/report-path
prologue and git/env-stamp plus triple atomic-write epilogue. This module owns
just those two shared halves so a report-format change is a one-site edit:

- :func:`_start_lane_run` creates the run id, the run directory, resolves the
  report paths, and seeds the base artifacts dict.
- :func:`_finish_lane_run` stamps git/env metadata, performs the triple atomic
  report write, and assembles the :class:`ValidationRunResult`.

Lane-specific concerns stay in the lane bodies: the stdout/stderr/junit
redaction (which differs per lane) and the release strictness flags are
deliberately *not* unified here.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easycat.validation._environment import PROVIDER_ENV_VARS
from easycat.validation.latency import _is_ci
from easycat.validation.report import (
    ArtifactRef,
    GitMetadata,
    ProviderCheck,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationRun,
    ValidationSkip,
    ValidationStatus,
)


@dataclass(frozen=True)
class ValidationRunResult:
    run: ValidationRun
    run_dir: Path
    report_path: Path
    exit_code: int


@dataclass(frozen=True)
class LaneRunContext:
    """Prologue output shared by every lane: run id, dirs, paths, base artifacts."""

    run_id: str
    run_dir: Path
    run_report_path: Path
    requested_report_path: Path | None
    artifacts: dict[str, ArtifactRef] = field(default_factory=dict)


def _start_lane_run(
    label: str,
    *,
    started_at: datetime,
    artifacts_root: Path,
    report_path: str | Path | None,
) -> LaneRunContext:
    """Create the run id/dir, resolve the report paths, seed base artifacts."""
    run_id = _make_run_id(label, started_at)
    run_dir = artifacts_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    run_report_path = run_dir / "report.json"
    requested_report_path = Path(report_path) if report_path is not None else None
    artifacts: dict[str, ArtifactRef] = {
        "report": ArtifactRef(kind="validation_report", path=str(run_report_path)),
    }
    return LaneRunContext(
        run_id=run_id,
        run_dir=run_dir,
        run_report_path=run_report_path,
        requested_report_path=requested_report_path,
        artifacts=artifacts,
    )


def _finish_lane_run(
    ctx: LaneRunContext,
    *,
    artifacts_root: Path,
    command: Sequence[str],
    started_at: datetime,
    finished_at: datetime,
    duration_s: float,
    status: ValidationStatus,
    exit_code: int,
    tool_exit_codes: Mapping[str, int],
    checks: Sequence[ValidationCheck],
    failures: Sequence[ValidationFailure],
    artifacts: Mapping[str, ArtifactRef],
    skips: Sequence[ValidationSkip] = (),
    reliability: Mapping[str, Any] | None = None,
    latency: Mapping[str, Any] | None = None,
    providers: Sequence[ProviderCheck] = (),
    provider_reports: Sequence[Mapping[str, Any]] = (),
) -> ValidationRunResult:
    """Stamp git/env metadata, write the report three times, build the result."""
    run = ValidationRun(
        run_id=ctx.run_id,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes=tool_exit_codes,
        git=_collect_git_metadata(),
        environment=_collect_environment_metadata(),
        checks=checks,
        skips=list(skips),
        failures=failures,
        reliability=reliability,
        latency=latency,
        providers=list(providers),
        provider_reports=list(provider_reports),
        artifacts=artifacts,
    )

    _write_atomic(ctx.run_report_path, run.to_json())
    if ctx.requested_report_path is not None:
        # Authoritative writer of the CLI ``--report`` path; the CLI relies
        # on this and does not write the report itself.
        _write_atomic(ctx.requested_report_path, run.to_json())
    _write_atomic(artifacts_root / "latest.json", run.to_json())
    return ValidationRunResult(
        run=run,
        run_dir=ctx.run_dir,
        report_path=ctx.requested_report_path or ctx.run_report_path,
        exit_code=exit_code,
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
        ci=_is_ci(),
        env_vars={name: name in os.environ for name in PROVIDER_ENV_VARS},
    )


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(text)
    os.replace(tmp_path, path)
