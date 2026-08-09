"""Deterministic pytest-backed validation slice orchestration."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from easycat.validation._environment import runtime_secret_values
from easycat.validation._lane_harness import (
    LaneRunContext,
    ValidationRunResult,
    _finish_lane_run,
    _start_lane_run,
)
from easycat.validation._latency_artifacts import build_reliability_artifact
from easycat.validation._latency_models import ReliabilitySample
from easycat.validation._reliability_policy import (
    load_reliability,
    reliability_budget_failure,
)
from easycat.validation._runner_support import (
    CommandResult,
    CommandRunner,
    pytest_command_prefix,
    redact_validation_artifacts,
    run_subprocess,
    run_timed_command,
    validation_exit_code_from_pytest,
    validation_test_paths,
)
from easycat.validation.redaction import TextArtifactFormat
from easycat.validation.report import (
    ArtifactRef,
    ValidationCheck,
    ValidationFailure,
    ValidationStatus,
    redact_runtime_secrets,
)


@dataclass(frozen=True, slots=True)
class _SliceSpec:
    """Declarative command and artifact policy for one deterministic lane."""

    name: str
    selector: str
    pytest_args: tuple[str, ...] = ()
    captures_webrtc_stats: bool = False


# The quick slice excludes socket and timing-sensitive tests, so xdist can
# schedule individual tests instead of turning very large modules into a
# single-worker tail. Socket, stress, contract, and guard lanes stay serial.
_SLICE_SPECS = {
    "quick": _SliceSpec(
        name="quick",
        selector=(
            "not integration_socket and not integration_live and not integration_external "
            "and not contract and not latency and not slow and not stress and not serial "
            "and not flaky and not guard"
        ),
        pytest_args=("-n", "auto", "--dist", "load"),
    ),
    "guard": _SliceSpec(
        name="guard",
        selector="guard and not integration_live and not integration_external and not flaky",
    ),
    "socket": _SliceSpec(
        name="socket",
        selector="integration_socket and not integration_live and not flaky",
        captures_webrtc_stats=True,
    ),
    "stress": _SliceSpec(
        name="stress",
        selector="stress and not integration_live and not flaky",
    ),
    "contracts": _SliceSpec(
        name="contracts",
        selector="contract and not integration_live and not flaky",
    ),
}

# Maintained compatibility surface for docs, Justfile parity tests, and callers
# that need to display the marker expression without constructing a run.
VALIDATION_SELECTORS = {name: spec.selector for name, spec in _SLICE_SPECS.items()}


@dataclass(frozen=True, slots=True)
class _SlicePaths:
    """Resolved artifact paths for one isolated slice run."""

    junit: Path
    stdout: Path
    stderr: Path
    reliability_samples: Path
    webrtc_stats: Path

    @classmethod
    def create(cls, run_dir: Path, junit_path: str | Path | None) -> _SlicePaths:
        """Resolve every artifact path for one isolated slice run."""
        return cls(
            junit=Path(junit_path) if junit_path is not None else run_dir / "junit.xml",
            stdout=run_dir / "stdout.log",
            stderr=run_dir / "stderr.log",
            reliability_samples=run_dir / "reliability" / "samples.json",
            webrtc_stats=run_dir / "webrtc" / "stats.jsonl",
        )

    def prepare(self, spec: _SliceSpec) -> None:
        """Create parent directories required before invoking pytest."""
        self.junit.parent.mkdir(parents=True, exist_ok=True)
        self.reliability_samples.parent.mkdir(parents=True, exist_ok=True)
        if spec.captures_webrtc_stats:
            self.webrtc_stats.parent.mkdir(parents=True, exist_ok=True)

    def write_redacted(
        self,
        result: CommandResult,
        secrets: Sequence[str],
    ) -> dict[str, ValidationFailure]:
        """Persist streams and scrub every validation-owned text artifact."""
        self.stdout.write_text(redact_runtime_secrets(result.stdout, secrets))
        self.stderr.write_text(redact_runtime_secrets(result.stderr, secrets))
        artifact_specs: tuple[tuple[str, Path, TextArtifactFormat], ...] = (
            ("junit", self.junit, "text"),
            ("reliability", self.reliability_samples, "json"),
            ("webrtc_stats", self.webrtc_stats, "jsonl"),
        )
        return redact_validation_artifacts(artifact_specs, secrets)

    def check_artifacts(
        self,
        spec: _SliceSpec,
        *,
        excluded: frozenset[str] = frozenset(),
    ) -> dict[str, ArtifactRef]:
        """Project artifacts that were actually produced by the slice."""
        artifacts = {
            "stdout": ArtifactRef(kind="stdout", path=str(self.stdout)),
            "stderr": ArtifactRef(kind="stderr", path=str(self.stderr)),
        }
        if "junit" not in excluded and self.junit.exists():
            artifacts["junit"] = ArtifactRef(kind="junit", path=str(self.junit))
        if "reliability" not in excluded and self.reliability_samples.exists():
            artifacts["reliability"] = ArtifactRef(
                kind="reliability",
                path=str(self.reliability_samples),
            )
        if (
            "webrtc_stats" not in excluded
            and spec.captures_webrtc_stats
            and self.webrtc_stats.exists()
        ):
            artifacts["webrtc_stats"] = ArtifactRef(
                kind="webrtc_stats",
                path=str(self.webrtc_stats),
            )
        return artifacts


@dataclass(frozen=True, slots=True)
class _SliceOutcome:
    """Evaluated pytest and reliability result passed to the lane epilogue."""

    exit_code: int
    status: ValidationStatus
    failures: tuple[ValidationFailure, ...]
    reliability: Mapping[str, object] | None
    reliability_failure: ValidationFailure | None
    reliability_budget_failure: ValidationFailure | None
    artifact_redaction_failures: tuple[ValidationFailure, ...]


def run_validation_slice(
    slice_name: str,
    *,
    artifacts_dir: str | Path = ".easycat/validation",
    report_path: str | Path | None = None,
    junit_path: str | Path | None = None,
    junit_prefix: str | None = None,
    command_runner: CommandRunner | None = None,
    started_at: datetime | None = None,
) -> ValidationRunResult:
    """Run one deterministic validation selector and persist its report."""
    spec = _slice_spec(slice_name)
    started_at = started_at or datetime.now(UTC)
    artifacts_root = Path(artifacts_dir)
    ctx = _start_lane_run(
        spec.name,
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    paths = _SlicePaths.create(ctx.run_dir, junit_path)
    paths.prepare(spec)
    command = _slice_command(spec, paths.junit, junit_prefix)
    command_env = _slice_environment(spec, paths)
    secrets = runtime_secret_values()
    result, duration_s, finished_at = run_timed_command(
        command_runner or run_subprocess,
        command,
        env=command_env,
    )
    artifact_redaction_failures = paths.write_redacted(result, secrets)
    outcome = _evaluate_result(
        spec.name,
        result,
        (None if "reliability" in artifact_redaction_failures else paths.reliability_samples),
        finished_at,
        secrets,
        tuple(artifact_redaction_failures.values()),
    )
    check_artifacts = paths.check_artifacts(
        spec,
        excluded=frozenset(artifact_redaction_failures),
    )
    artifacts = ctx.artifacts_with(check_artifacts)
    return _finish_slice(
        slice_name=spec.name,
        ctx=ctx,
        artifacts_root=artifacts_root,
        command=command,
        result=result,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        outcome=outcome,
        check_artifacts=check_artifacts,
        artifacts=artifacts,
    )


def _slice_spec(slice_name: str) -> _SliceSpec:
    try:
        return _SLICE_SPECS[slice_name]
    except KeyError:
        known = ", ".join(sorted(VALIDATION_SELECTORS))
        raise ValueError(
            f"unknown validation slice {slice_name!r}; expected one of: {known}"
        ) from None


def _slice_command(
    spec: _SliceSpec,
    junit_path: Path,
    junit_prefix: str | None,
) -> list[str]:
    command = [
        *pytest_command_prefix(test_override_mode="paths"),
        "-q",
        *spec.pytest_args,
        *validation_test_paths(),
        f"--junitxml={junit_path}",
        "-m",
        spec.selector,
    ]
    if junit_prefix:
        command.append(f"--junit-prefix={junit_prefix}")
    return command


def _slice_environment(spec: _SliceSpec, paths: _SlicePaths) -> dict[str, str]:
    env = {
        **os.environ,
        "EASYCAT_RELIABILITY_SAMPLES_PATH": str(paths.reliability_samples),
    }
    if spec.captures_webrtc_stats:
        env["EASYCAT_WEBRTC_STATS_PATH"] = str(paths.webrtc_stats)
    return env


def _evaluate_result(
    slice_name: str,
    result: CommandResult,
    reliability_path: Path | None,
    finished_at: datetime,
    secrets: Sequence[str],
    artifact_redaction_failures: tuple[ValidationFailure, ...],
) -> _SliceOutcome:
    if reliability_path is None:
        samples, reliability_failure = None, None
    else:
        samples, reliability_failure = load_reliability(reliability_path)
    reliability, budget_failure = _load_reliability(
        samples,
        finished_at,
    )
    exit_code = validation_exit_code_from_pytest(result.exit_code)
    if (
        reliability_failure is not None
        or budget_failure is not None
        or artifact_redaction_failures
    ):
        exit_code = 1
    return _SliceOutcome(
        exit_code=exit_code,
        status="pass" if exit_code == 0 else "fail",
        failures=tuple(
            _slice_failures(
                slice_name,
                result,
                reliability_failure,
                budget_failure,
                secrets,
                artifact_redaction_failures,
            )
        ),
        reliability=reliability,
        reliability_failure=reliability_failure,
        reliability_budget_failure=budget_failure,
        artifact_redaction_failures=artifact_redaction_failures,
    )


def _load_reliability(
    samples: list[ReliabilitySample] | None,
    finished_at: datetime,
) -> tuple[dict[str, object] | None, ValidationFailure | None]:
    if samples is None:
        return None, None
    return (
        build_reliability_artifact(samples=samples, generated_at=finished_at),
        reliability_budget_failure(samples),
    )


def _slice_failures(
    slice_name: str,
    result: CommandResult,
    reliability_failure: ValidationFailure | None,
    budget_failure: ValidationFailure | None,
    secrets: Sequence[str],
    artifact_redaction_failures: Sequence[ValidationFailure],
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    if result.exit_code != 0:
        failures.append(
            ValidationFailure(
                name=f"pytest.{slice_name}",
                message=redact_runtime_secrets(
                    result.stderr or result.stdout or f"pytest exited {result.exit_code}",
                    secrets,
                ),
            )
        )
    if reliability_failure is not None:
        failures.append(reliability_failure)
    if budget_failure is not None:
        failures.append(budget_failure)
    failures.extend(artifact_redaction_failures)
    return failures


def _finish_slice(
    *,
    slice_name: str,
    ctx: LaneRunContext,
    artifacts_root: Path,
    command: list[str],
    result: CommandResult,
    started_at: datetime,
    finished_at: datetime,
    duration_s: float,
    outcome: _SliceOutcome,
    check_artifacts: dict[str, ArtifactRef],
    artifacts: dict[str, ArtifactRef],
) -> ValidationRunResult:
    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=outcome.status,
        exit_code=outcome.exit_code,
        tool_exit_codes={
            "pytest": result.exit_code,
            **({"reliability_samples": 1} if outcome.reliability_failure is not None else {}),
            **(
                {"reliability_budget": 1} if outcome.reliability_budget_failure is not None else {}
            ),
            **({"artifact_redaction": 1} if outcome.artifact_redaction_failures else {}),
        },
        checks=[
            ValidationCheck(
                name=f"pytest.{slice_name}",
                status=outcome.status,
                duration_s=duration_s,
                command=command,
                artifacts=check_artifacts,
            ),
            *(
                [
                    ValidationCheck(
                        name="validation.artifact_redaction",
                        status="fail",
                        duration_s=0.0,
                        details={
                            "failures": [
                                {
                                    "name": failure.name,
                                    "message": failure.message,
                                    "path": failure.details.get("path", ""),
                                }
                                for failure in outcome.artifact_redaction_failures
                            ]
                        },
                    )
                ]
                if outcome.artifact_redaction_failures
                else []
            ),
        ],
        failures=outcome.failures,
        reliability=outcome.reliability,
        artifacts=artifacts,
    )
