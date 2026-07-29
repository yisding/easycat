"""Latency validation lane orchestration and artifact projection."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easycat.validation._environment import runtime_secret_values
from easycat.validation._failure_classification import classify_latency_failure
from easycat.validation._lane_harness import (
    LaneRunContext,
    ValidationRunResult,
    _finish_lane_run,
    _start_lane_run,
    _write_atomic,
)
from easycat.validation._latency_artifacts import (
    build_latency_artifact,
    load_latency_samples,
)
from easycat.validation._latency_baseline import compare_latency_baseline
from easycat.validation._latency_models import (
    LatencyComparisonThresholds,
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
)
from easycat.validation._latency_selectors import latency_pytest_args
from easycat.validation._reliability_policy import (
    load_reliability,
    reliability_budget_failure,
)
from easycat.validation._runner_support import (
    CommandResult,
    CommandRunner,
    pytest_command_prefix,
    resolve_validation_test_arg,
    run_subprocess,
    run_timed_command,
    validation_exit_code_from_pytest,
)
from easycat.validation.redaction import (
    ArtifactRedactionError,
    TextArtifactFormat,
    redact_runtime_secrets_in_file,
)
from easycat.validation.report import (
    ArtifactRef,
    ValidationCheck,
    ValidationFailure,
    ValidationStatus,
    redact_runtime_secrets,
)

# Marker written into a synthetic sample's free-form ``debug`` map so
# consumers can distinguish a pytest failure carrier from a measured turn.
LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY = "synthetic"
LATENCY_SYNTHETIC_FAILURE_SAMPLE = "pytest_failure"


@dataclass(frozen=True, slots=True)
class _LatencyPaths:
    """Resolved artifacts for one latency run."""

    junit: Path
    stdout: Path
    stderr: Path
    samples: Path
    reliability_samples: Path
    latency: Path

    @classmethod
    def create(cls, run_dir: Path, mode: LatencyMode) -> _LatencyPaths:
        latency_dir = run_dir / "latency"
        return cls(
            junit=run_dir / "junit.xml",
            stdout=run_dir / "stdout.log",
            stderr=run_dir / "stderr.log",
            samples=latency_dir / "samples.json",
            reliability_samples=latency_dir / "reliability.json",
            latency=latency_dir / f"{mode.value}.json",
        )

    def prepare(self) -> None:
        self.samples.parent.mkdir(parents=True, exist_ok=True)

    def command_environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "EASYCAT_LATENCY_SAMPLES_PATH": str(self.samples),
            "EASYCAT_RELIABILITY_SAMPLES_PATH": str(self.reliability_samples),
        }

    def write_redacted(
        self,
        result: CommandResult,
        secrets: Sequence[str],
    ) -> dict[str, ValidationFailure]:
        self.stdout.write_text(redact_runtime_secrets(result.stdout, secrets))
        self.stderr.write_text(redact_runtime_secrets(result.stderr, secrets))
        failures: dict[str, ValidationFailure] = {}
        artifact_specs: tuple[tuple[str, Path, TextArtifactFormat], ...] = (
            ("junit", self.junit, "text"),
            ("samples", self.samples, "json"),
            ("reliability", self.reliability_samples, "json"),
        )
        for name, path, artifact_format in artifact_specs:
            try:
                redact_runtime_secrets_in_file(
                    path,
                    secrets,
                    artifact_format=artifact_format,
                    raise_on_error=True,
                )
            except (ArtifactRedactionError, OSError) as exc:
                safe_path = redact_runtime_secrets(str(path), secrets)
                failures[name] = ValidationFailure(
                    name=f"artifact_redaction.{name}",
                    message=redact_runtime_secrets(
                        f"could not safely redact validation artifact {path}: {exc}",
                        secrets,
                    ),
                    failure_class="artifact_redaction_error",
                    details={"path": safe_path},
                )
        return failures

    def check_artifacts(
        self,
        *,
        excluded: frozenset[str] = frozenset(),
    ) -> dict[str, ArtifactRef]:
        artifacts = {
            "stdout": ArtifactRef(kind="stdout", path=str(self.stdout)),
            "stderr": ArtifactRef(kind="stderr", path=str(self.stderr)),
            "latency": ArtifactRef(kind="latency", path=str(self.latency)),
        }
        if "junit" not in excluded and self.junit.exists():
            artifacts["junit"] = ArtifactRef(kind="junit", path=str(self.junit))
        return artifacts


@dataclass(frozen=True, slots=True)
class _LatencyEvidence:
    """Samples and structured artifact failures loaded after pytest exits."""

    samples: tuple[LatencySample, ...]
    reliability_samples: tuple[ReliabilitySample, ...]
    sample_load_failure: ValidationFailure | None = None
    required_samples_failure: ValidationFailure | None = None
    reliability_failure: ValidationFailure | None = None
    reliability_budget_failure: ValidationFailure | None = None


@dataclass(frozen=True, slots=True)
class _LatencyProjection:
    """Persisted latency payload plus failures found while projecting it."""

    payload: dict[str, Any]
    budget_violations: tuple[Any, ...]
    budget_failure: ValidationFailure | None
    baseline_comparison: dict[str, Any] | None
    baseline_load_failure: ValidationFailure | None
    baseline_regression_failure: ValidationFailure | None


@dataclass(frozen=True, slots=True)
class _LatencyExecution:
    """Lane prologue and subprocess result kept as one orchestration value."""

    ctx: LaneRunContext
    artifacts_root: Path
    paths: _LatencyPaths
    command: list[str]
    secrets: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    duration_s: float
    result: CommandResult
    artifact_redaction_failures: Mapping[str, ValidationFailure]


def run_latency_validation(
    mode: LatencyMode | str,
    *,
    artifacts_dir: str | Path = ".easycat/validation",
    report_path: str | Path | None = None,
    require_samples: bool | None = None,
    baseline_path: str | Path | None = None,
    command_runner: CommandRunner | None = None,
    started_at: datetime | None = None,
) -> ValidationRunResult:
    """Run one latency lane and persist its measurements and validation report."""
    mode = LatencyMode(mode)
    # Sweep runs are evidence gates; smoke runs may legitimately skip every
    # credentialed measurement unless the caller explicitly requires samples.
    if require_samples is None:
        require_samples = mode is LatencyMode.SWEEP
    execution = _execute_latency_lane(
        mode=mode,
        artifacts_dir=artifacts_dir,
        report_path=report_path,
        command_runner=command_runner or run_subprocess,
        started_at=started_at or datetime.now(UTC),
    )
    evidence = _load_evidence(
        execution.paths,
        require_samples=require_samples,
        secrets=execution.secrets,
        excluded=frozenset(execution.artifact_redaction_failures),
    )
    failure_message = _pytest_failure_message(execution.result, execution.secrets)
    samples = _samples_with_pytest_failure(
        evidence.samples,
        mode=mode,
        failure_message=failure_message,
        pytest_exit_code=execution.result.exit_code,
    )
    projection = _project_latency(
        mode=mode,
        samples=samples,
        evidence=evidence,
        finished_at=execution.finished_at,
        baseline_path=Path(baseline_path) if baseline_path is not None else None,
        paths=execution.paths,
        artifacts_root=execution.artifacts_root,
        secrets=execution.secrets,
    )
    policy_failures = (
        *execution.artifact_redaction_failures.values(),
        *_policy_failures(evidence, projection),
    )
    exit_code = validation_exit_code_from_pytest(execution.result.exit_code)
    if policy_failures:
        exit_code = 1
    status: ValidationStatus = "pass" if exit_code == 0 else "fail"
    check_artifacts = execution.paths.check_artifacts(
        excluded=frozenset(execution.artifact_redaction_failures),
    )
    artifacts = execution.ctx.artifacts_with(check_artifacts)
    failures = _all_failures(
        mode=mode,
        result=execution.result,
        failure_message=failure_message,
        policy_failures=policy_failures,
    )
    return _finish_lane_run(
        execution.ctx,
        artifacts_root=execution.artifacts_root,
        command=execution.command,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        duration_s=execution.duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes=_tool_exit_codes(
            execution.result,
            evidence,
            projection,
            tuple(execution.artifact_redaction_failures.values()),
        ),
        checks=_latency_checks(
            mode=mode,
            pytest_exit_code=execution.result.exit_code,
            duration_s=execution.duration_s,
            command=execution.command,
            check_artifacts=check_artifacts,
            evidence=evidence,
            projection=projection,
            artifact_redaction_failures=tuple(execution.artifact_redaction_failures.values()),
        ),
        failures=failures,
        latency=projection.payload,
        artifacts=artifacts,
    )


def _execute_latency_lane(
    *,
    mode: LatencyMode,
    artifacts_dir: str | Path,
    report_path: str | Path | None,
    command_runner: CommandRunner,
    started_at: datetime,
) -> _LatencyExecution:
    artifacts_root = Path(artifacts_dir)
    ctx = _start_lane_run(
        f"latency-{mode.value}",
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    paths = _LatencyPaths.create(ctx.run_dir, mode)
    paths.prepare()
    command = _latency_command(mode, paths.junit)
    secrets = tuple(runtime_secret_values())
    result, duration_s, finished_at = run_timed_command(
        command_runner,
        command,
        env=paths.command_environment(),
    )
    artifact_redaction_failures = paths.write_redacted(result, secrets)
    return _LatencyExecution(
        ctx=ctx,
        artifacts_root=artifacts_root,
        paths=paths,
        command=command,
        secrets=secrets,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        result=result,
        artifact_redaction_failures=artifact_redaction_failures,
    )


def _latency_command(mode: LatencyMode, junit_path: Path) -> list[str]:
    return [
        *pytest_command_prefix(test_override_mode="root"),
        "-q",
        f"--junitxml={junit_path}",
        *[resolve_validation_test_arg(arg) for arg in latency_pytest_args(mode)],
    ]


def _load_evidence(
    paths: _LatencyPaths,
    *,
    require_samples: bool,
    secrets: Sequence[str],
    excluded: frozenset[str],
) -> _LatencyEvidence:
    sample_load_failure: ValidationFailure | None = None
    try:
        samples = (
            load_latency_samples(paths.samples.read_text())
            if "samples" not in excluded and paths.samples.exists()
            else []
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        samples = []
        sample_load_failure = ValidationFailure(
            name="latency.samples",
            message=redact_runtime_secrets(f"could not load latency samples: {exc}", secrets),
            failure_class="latency_artifact_error",
        )
    if "reliability" in excluded:
        loaded_reliability_samples, reliability_failure = None, None
    else:
        loaded_reliability_samples, reliability_failure = load_reliability(
            paths.reliability_samples
        )
    reliability_failure = _redact_validation_failure(reliability_failure, secrets)
    reliability_samples: list[ReliabilitySample] = loaded_reliability_samples or []
    reliability_budget: ValidationFailure | None = None
    if loaded_reliability_samples is not None:
        reliability_budget = reliability_budget_failure(reliability_samples)
    required_failure = None
    if require_samples and not samples and "samples" not in excluded:
        required_failure = ValidationFailure(
            name="latency.samples",
            message="required latency validation produced no samples",
            failure_class="latency_artifact_error",
        )
    return _LatencyEvidence(
        samples=tuple(samples),
        reliability_samples=tuple(reliability_samples),
        sample_load_failure=sample_load_failure,
        required_samples_failure=required_failure,
        reliability_failure=reliability_failure,
        reliability_budget_failure=reliability_budget,
    )


def _pytest_failure_message(result: CommandResult, secrets: Sequence[str]) -> str:
    message = result.stderr or result.stdout or f"pytest exited {result.exit_code}"
    return redact_runtime_secrets(message, secrets)


def _samples_with_pytest_failure(
    samples: Sequence[LatencySample],
    *,
    mode: LatencyMode,
    failure_message: str,
    pytest_exit_code: int,
) -> tuple[LatencySample, ...]:
    if validation_exit_code_from_pytest(pytest_exit_code) == 0 or samples:
        return tuple(samples)
    return (*samples, _latency_failure_sample(mode, failure_message))


def _project_latency(
    *,
    mode: LatencyMode,
    samples: Sequence[LatencySample],
    evidence: _LatencyEvidence,
    finished_at: datetime,
    baseline_path: Path | None,
    paths: _LatencyPaths,
    artifacts_root: Path,
    secrets: Sequence[str],
) -> _LatencyProjection:
    payload = build_latency_artifact(
        mode=mode,
        samples=list(samples),
        reliability_samples=list(evidence.reliability_samples),
        generated_at=finished_at,
    )
    baseline_comparison, baseline_load_failure = _baseline_result(
        payload,
        baseline_path,
        secrets,
    )
    if baseline_comparison is not None:
        payload["baseline"] = baseline_comparison
    payload = _redact_runtime_json(payload, secrets)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _write_atomic(paths.latency, serialized)
    _write_atomic(artifacts_root / "latency" / f"{mode.value}-latest.json", serialized)
    budget_violations = tuple(payload.get("budget_violations") or ())
    budget_failure = _budget_failure(budget_violations)
    return _LatencyProjection(
        payload=payload,
        budget_violations=budget_violations,
        budget_failure=budget_failure,
        baseline_comparison=baseline_comparison,
        baseline_load_failure=baseline_load_failure,
        baseline_regression_failure=_baseline_comparison_failure(baseline_comparison),
    )


def _baseline_result(
    current: Mapping[str, Any],
    baseline_path: Path | None,
    secrets: Sequence[str],
) -> tuple[dict[str, Any] | None, ValidationFailure | None]:
    if baseline_path is None:
        return None, None
    comparison, failure = _compare_against_baseline(
        current=current,
        baseline_path=baseline_path,
    )
    redacted_comparison = (
        _redact_runtime_json(comparison, secrets) if comparison is not None else None
    )
    return redacted_comparison, _redact_validation_failure(failure, secrets)


def _budget_failure(violations: Sequence[Any]) -> ValidationFailure | None:
    if not violations:
        return None
    return ValidationFailure(
        name="latency.budget",
        message="latency budget violated",
        failure_class="latency_budget",
        details={"violations": list(violations)},
    )


def _policy_failures(
    evidence: _LatencyEvidence,
    projection: _LatencyProjection,
) -> tuple[ValidationFailure, ...]:
    return tuple(
        failure
        for failure in (
            evidence.sample_load_failure,
            evidence.reliability_failure,
            evidence.reliability_budget_failure,
            evidence.required_samples_failure,
            projection.budget_failure,
            projection.baseline_load_failure,
            projection.baseline_regression_failure,
        )
        if failure is not None
    )


def _all_failures(
    *,
    mode: LatencyMode,
    result: CommandResult,
    failure_message: str,
    policy_failures: Sequence[ValidationFailure],
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    if result.exit_code != 0:
        failures.append(
            ValidationFailure(
                name=f"pytest.latency.{mode.value}",
                message=failure_message,
                failure_class=classify_latency_failure(failure_message),
            )
        )
    failures.extend(policy_failures)
    return failures


def _tool_exit_codes(
    result: CommandResult,
    evidence: _LatencyEvidence,
    projection: _LatencyProjection,
    artifact_redaction_failures: Sequence[ValidationFailure],
) -> dict[str, int]:
    return {
        "pytest": result.exit_code,
        **({"latency_samples": 1} if evidence.sample_load_failure is not None else {}),
        **({"reliability_samples": 1} if evidence.reliability_failure is not None else {}),
        **({"reliability_budget": 1} if evidence.reliability_budget_failure is not None else {}),
        **(
            {"required_latency_samples": 1}
            if evidence.required_samples_failure is not None
            else {}
        ),
        **({"latency_budget": 1} if projection.budget_failure is not None else {}),
        **({"latency_baseline": 1} if projection.baseline_load_failure is not None else {}),
        **(
            {"latency_baseline_regression": 1}
            if projection.baseline_regression_failure is not None
            else {}
        ),
        **({"artifact_redaction": 1} if artifact_redaction_failures else {}),
    }


def _redact_runtime_json(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_runtime_secrets(value, secrets)
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_runtime_json(item_value, secrets)
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_runtime_json(item, secrets) for item in value]
    return value


def _redact_validation_failure(
    failure: ValidationFailure | None,
    secrets: Sequence[str],
) -> ValidationFailure | None:
    if failure is None:
        return None
    details = _redact_runtime_json(dict(failure.details), secrets) if failure.details else {}
    return ValidationFailure(
        name=failure.name,
        message=redact_runtime_secrets(failure.message, secrets),
        failure_class=failure.failure_class,
        details=details,
    )


def _latency_checks(
    *,
    mode: LatencyMode,
    pytest_exit_code: int,
    duration_s: float,
    command: Sequence[str],
    check_artifacts: dict[str, ArtifactRef],
    evidence: _LatencyEvidence,
    projection: _LatencyProjection,
    artifact_redaction_failures: Sequence[ValidationFailure],
) -> list[ValidationCheck]:
    checks = [
        ValidationCheck(
            name=f"pytest.latency.{mode.value}",
            status="pass" if pytest_exit_code == 0 else "fail",
            duration_s=duration_s,
            command=command,
            artifacts=check_artifacts,
        )
    ]
    sample_failures = tuple(
        failure
        for failure in (evidence.sample_load_failure, evidence.required_samples_failure)
        if failure is not None
    )
    if sample_failures:
        checks.append(_latency_failure_check("latency.samples", sample_failures, check_artifacts))
    if artifact_redaction_failures:
        checks.append(
            _latency_failure_check(
                "validation.artifact_redaction",
                artifact_redaction_failures,
                check_artifacts,
            )
        )
    checks.extend(_reliability_checks(evidence, check_artifacts))
    budget_check = _latency_budget_check(mode, projection, check_artifacts)
    if budget_check is not None:
        checks.append(budget_check)
    baseline_check = _latency_baseline_check(projection)
    if baseline_check is not None:
        checks.append(baseline_check)
    return checks


def _reliability_checks(
    evidence: _LatencyEvidence,
    check_artifacts: Mapping[str, ArtifactRef],
) -> list[ValidationCheck]:
    checks: list[ValidationCheck] = []
    if evidence.reliability_failure is not None:
        checks.append(
            _latency_failure_check(
                "reliability.samples",
                [evidence.reliability_failure],
                check_artifacts,
            )
        )
    if evidence.reliability_budget_failure is not None:
        checks.append(
            _latency_failure_check(
                "reliability.budget",
                [evidence.reliability_budget_failure],
                check_artifacts,
            )
        )
    return checks


def _latency_budget_check(
    mode: LatencyMode,
    projection: _LatencyProjection,
    check_artifacts: Mapping[str, ArtifactRef],
) -> ValidationCheck | None:
    if mode is LatencyMode.SMOKE:
        return None
    budget_artifacts = (
        {"latency": check_artifacts["latency"]} if "latency" in check_artifacts else {}
    )
    return ValidationCheck(
        name="latency.budget",
        status="fail" if projection.budget_failure is not None else "pass",
        duration_s=0.0,
        artifacts=budget_artifacts,
        details=(
            {"violations": list(projection.budget_violations)}
            if projection.budget_violations
            else {}
        ),
    )


def _latency_baseline_check(projection: _LatencyProjection) -> ValidationCheck | None:
    comparison = projection.baseline_comparison
    load_failure = projection.baseline_load_failure
    if comparison is None and load_failure is None:
        return None
    baseline_failed = (
        load_failure is not None or projection.baseline_regression_failure is not None
    )
    details: dict[str, Any] = {}
    if comparison is not None:
        details = {
            "status": comparison.get("status"),
            "conditions": comparison.get("conditions", []),
        }
    elif load_failure is not None:
        details = {"message": load_failure.message}
    return ValidationCheck(
        name="latency.baseline",
        status="fail" if baseline_failed else "pass",
        duration_s=0.0,
        details=details,
    )


def _latency_failure_check(
    name: str,
    failures: Sequence[ValidationFailure],
    check_artifacts: Mapping[str, ArtifactRef],
) -> ValidationCheck:
    details = {
        "failures": [
            {
                "name": failure.name,
                "message": failure.message,
                **(
                    {"failure_class": failure.failure_class}
                    if failure.failure_class is not None
                    else {}
                ),
                **({"details": dict(failure.details)} if failure.details else {}),
            }
            for failure in failures
        ]
    }
    return ValidationCheck(
        name=name,
        status="fail",
        duration_s=0.0,
        artifacts=check_artifacts,
        details=details,
    )


def _compare_against_baseline(
    *,
    current: Mapping[str, Any],
    baseline_path: Path,
) -> tuple[dict[str, Any] | None, ValidationFailure | None]:
    try:
        raw = baseline_path.read_text()
    except OSError as exc:
        return None, ValidationFailure(
            name="latency.baseline",
            message=f"could not read latency baseline {baseline_path}: {exc}",
            failure_class="latency_baseline_error",
        )
    try:
        baseline_payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, ValidationFailure(
            name="latency.baseline",
            message=f"invalid latency baseline JSON {baseline_path}: {exc}",
            failure_class="latency_baseline_error",
        )
    if not isinstance(baseline_payload, Mapping):
        return None, ValidationFailure(
            name="latency.baseline",
            message=f"latency baseline {baseline_path} must be a JSON object",
            failure_class="latency_baseline_error",
        )
    try:
        comparison = compare_latency_baseline(
            current,
            baseline_payload,
            thresholds=LatencyComparisonThresholds(),
        )
    except (ValueError, KeyError, TypeError) as exc:
        return None, ValidationFailure(
            name="latency.baseline",
            message=f"could not compare latency baseline {baseline_path}: {exc}",
            failure_class="latency_baseline_error",
        )
    return comparison, None


def _baseline_comparison_failure(
    comparison: dict[str, Any] | None,
) -> ValidationFailure | None:
    if comparison is None:
        return None
    status = comparison.get("status")
    if status not in ("fail", "drift"):
        return None
    conditions = comparison.get("conditions")
    offending = [
        condition
        for condition in (conditions if isinstance(conditions, list) else [])
        if isinstance(condition, Mapping) and condition.get("status") == status
    ]
    failure_class = "easycat_latency_regression" if status == "fail" else "provider_api_drift"
    return ValidationFailure(
        name="latency.baseline",
        message=f"latency baseline comparison reported {status}",
        failure_class=failure_class,
        details={"status": status, "conditions": offending},
    )


def _latency_failure_sample(mode: LatencyMode, message: str) -> LatencySample:
    return LatencySample(
        sample_id=f"{mode.value}-failure-{uuid.uuid4().hex[:12]}",
        condition_id=f"latency_{mode.value}",
        warmup=False,
        timestamp_source="time.monotonic",
        stages=LatencyStageDurations(),
        debug={LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY: LATENCY_SYNTHETIC_FAILURE_SAMPLE},
        missing_stage_reason=message,
        failure_class=classify_latency_failure(message),
    )
