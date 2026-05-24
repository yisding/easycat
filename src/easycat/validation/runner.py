from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from easycat.validation.latency import (
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    build_latency_artifact,
    build_reliability_artifact,
    classify_latency_failure,
    latency_pytest_args,
    load_latency_samples,
    load_reliability_samples,
)
from easycat.validation.provider_reports import (
    ProviderSurfaceSpec,
    build_provider_capability_report,
    known_live_providers,
    known_live_surfaces,
    select_provider_surfaces,
)
from easycat.validation.report import (
    ArtifactRef,
    GitMetadata,
    ProviderCheck,
    ProviderCheckState,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationRun,
    ValidationSkip,
    redact_runtime_secrets,
    redact_text,
)

VALIDATION_SELECTORS = {
    "quick": (
        "not integration_socket and not integration_live and not slow and not stress and not flaky"
    ),
    "socket": "integration_socket and not integration_live and not flaky",
    "stress": "stress and not integration_live and not flaky",
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
    report_path: str | Path | None = None,
    junit_path: str | Path | None = None,
    junit_prefix: str | None = None,
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

    junit_path = Path(junit_path) if junit_path is not None else run_dir / "junit.xml"
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    run_report_path = run_dir / "report.json"
    requested_report_path = Path(report_path) if report_path is not None else None
    reliability_samples_path = run_dir / "reliability" / "samples.json"
    reliability_samples_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        *_pytest_command_prefix(),
        "-q",
        *_validation_test_paths(),
        f"--junitxml={junit_path}",
        "-m",
        VALIDATION_SELECTORS[slice_name],
    ]
    if junit_prefix:
        command.append(f"--junit-prefix={junit_prefix}")

    old_reliability_samples_path = os.environ.get("EASYCAT_RELIABILITY_SAMPLES_PATH")
    os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"] = str(reliability_samples_path)
    started_monotonic = time.perf_counter()
    try:
        result = command_runner(command)
    finally:
        if old_reliability_samples_path is None:
            os.environ.pop("EASYCAT_RELIABILITY_SAMPLES_PATH", None)
        else:
            os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"] = old_reliability_samples_path
    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)

    stdout_path.write_text(redact_text(result.stdout))
    stderr_path.write_text(redact_text(result.stderr))

    exit_code = validation_exit_code_from_pytest(result.exit_code)
    reliability_failure = _load_reliability_failure(reliability_samples_path)
    reliability_payload: dict[str, object] | None = None
    if reliability_samples_path.exists() and reliability_failure is None:
        reliability_samples = load_reliability_samples(reliability_samples_path.read_text())
        reliability_payload = build_reliability_artifact(
            samples=reliability_samples,
            generated_at=finished_at,
        )
    if reliability_failure is not None:
        exit_code = 1
    status = "pass" if exit_code == 0 else "fail"

    check_artifacts: dict[str, ArtifactRef] = {
        "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
        "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
    }
    if junit_path.exists():
        check_artifacts["junit"] = ArtifactRef(kind="junit", path=str(junit_path))
    if reliability_samples_path.exists():
        check_artifacts["reliability"] = ArtifactRef(
            kind="reliability",
            path=str(reliability_samples_path),
        )

    artifacts: dict[str, ArtifactRef] = {
        "report": ArtifactRef(kind="validation_report", path=str(run_report_path)),
        **check_artifacts,
    }
    if requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(requested_report_path),
        )

    failures = []
    if result.exit_code != 0:
        failures.append(
            ValidationFailure(
                name=f"pytest.{slice_name}",
                message=result.stderr or result.stdout or f"pytest exited {result.exit_code}",
            )
        )
    if reliability_failure is not None:
        failures.append(reliability_failure)

    run = ValidationRun(
        run_id=run_id,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes={
            "pytest": result.exit_code,
            **({"reliability_samples": 1} if reliability_failure is not None else {}),
        },
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
        reliability=reliability_payload,
        artifacts=artifacts,
    )

    _write_atomic(run_report_path, run.to_json())
    if requested_report_path is not None:
        _write_atomic(requested_report_path, run.to_json())
    _write_atomic(artifacts_root / "latest.json", run.to_json())
    result_report_path = requested_report_path or run_report_path
    return ValidationRunResult(
        run=run,
        run_dir=run_dir,
        report_path=result_report_path,
        exit_code=exit_code,
    )


def run_latency_validation(
    mode: LatencyMode | str,
    *,
    artifacts_dir: str | Path = ".easycat/validation",
    report_path: str | Path | None = None,
    require_samples: bool = False,
    command_runner: CommandRunner | None = None,
    started_at: datetime | None = None,
) -> ValidationRunResult:
    mode = LatencyMode(mode)
    command_runner = command_runner or _run_subprocess
    started_at = started_at or datetime.now(UTC)
    artifacts_root = Path(artifacts_dir)
    run_id = _make_run_id(f"latency-{mode.value}", started_at)
    run_dir = artifacts_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    junit_path = run_dir / "junit.xml"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    run_report_path = run_dir / "report.json"
    requested_report_path = Path(report_path) if report_path is not None else None
    samples_path = run_dir / "latency" / "samples.json"
    reliability_samples_path = run_dir / "latency" / "reliability.json"
    latency_path = run_dir / "latency" / f"{mode.value}.json"
    samples_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        *_pytest_command_prefix(),
        "-q",
        f"--junitxml={junit_path}",
        *[_resolve_validation_test_arg(arg) for arg in latency_pytest_args(mode)],
    ]

    old_samples_path = os.environ.get("EASYCAT_LATENCY_SAMPLES_PATH")
    old_reliability_samples_path = os.environ.get("EASYCAT_RELIABILITY_SAMPLES_PATH")
    os.environ["EASYCAT_LATENCY_SAMPLES_PATH"] = str(samples_path)
    os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"] = str(reliability_samples_path)
    started_monotonic = time.perf_counter()
    try:
        result = command_runner(command)
    finally:
        if old_samples_path is None:
            os.environ.pop("EASYCAT_LATENCY_SAMPLES_PATH", None)
        else:
            os.environ["EASYCAT_LATENCY_SAMPLES_PATH"] = old_samples_path
        if old_reliability_samples_path is None:
            os.environ.pop("EASYCAT_RELIABILITY_SAMPLES_PATH", None)
        else:
            os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"] = old_reliability_samples_path
    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)

    stdout_path.write_text(redact_text(result.stdout))
    stderr_path.write_text(redact_text(result.stderr))
    exit_code = validation_exit_code_from_pytest(result.exit_code)
    sample_load_failure: ValidationFailure | None = None
    try:
        samples = load_latency_samples(samples_path.read_text()) if samples_path.exists() else []
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        samples = []
        sample_load_failure = ValidationFailure(
            name="latency.samples",
            message=f"could not load latency samples: {exc}",
            failure_class="latency_artifact_error",
        )
    reliability_failure = _load_reliability_failure(reliability_samples_path)
    reliability_samples = []
    if reliability_samples_path.exists() and reliability_failure is None:
        reliability_samples = load_reliability_samples(reliability_samples_path.read_text())

    required_samples_failure: ValidationFailure | None = None
    if require_samples and not samples:
        required_samples_failure = ValidationFailure(
            name="latency.samples",
            message="required latency validation produced no samples",
            failure_class="latency_artifact_error",
        )

    failure_message = result.stderr or result.stdout or f"pytest exited {result.exit_code}"
    if exit_code != 0 and not samples:
        samples.append(_latency_failure_sample(mode, failure_message))

    latency_payload = build_latency_artifact(
        mode=mode,
        samples=samples,
        reliability_samples=reliability_samples,
        generated_at=finished_at,
    )
    _write_atomic(latency_path, json.dumps(latency_payload, indent=2, sort_keys=True) + "\n")
    _write_atomic(
        artifacts_root / "latency" / f"{mode.value}-latest.json",
        json.dumps(latency_payload, indent=2, sort_keys=True) + "\n",
    )

    budget_violations = latency_payload.get("budget_violations") or []
    budget_failure: ValidationFailure | None = None
    if budget_violations:
        budget_failure = ValidationFailure(
            name="latency.budget",
            message="latency budget violated",
            failure_class="latency_budget",
            details={"violations": list(budget_violations)},
        )

    if (
        sample_load_failure is not None
        or reliability_failure is not None
        or required_samples_failure is not None
        or budget_failure is not None
    ):
        exit_code = 1
    status = "pass" if exit_code == 0 else "fail"
    check_artifacts: dict[str, ArtifactRef] = {
        "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
        "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
        "latency": ArtifactRef(kind="latency", path=str(latency_path)),
    }
    if junit_path.exists():
        check_artifacts["junit"] = ArtifactRef(kind="junit", path=str(junit_path))

    failures = []
    if result.exit_code != 0:
        failures.append(
            ValidationFailure(
                name=f"pytest.latency.{mode.value}",
                message=failure_message,
                failure_class=classify_latency_failure(failure_message),
            )
        )
    if sample_load_failure is not None:
        failures.append(sample_load_failure)
    if reliability_failure is not None:
        failures.append(reliability_failure)
    if required_samples_failure is not None:
        failures.append(required_samples_failure)
    if budget_failure is not None:
        failures.append(budget_failure)

    artifacts: dict[str, ArtifactRef] = {
        "report": ArtifactRef(kind="validation_report", path=str(run_report_path)),
        **check_artifacts,
    }
    if requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(requested_report_path),
        )

    run = ValidationRun(
        run_id=run_id,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes={
            "pytest": result.exit_code,
            **({"latency_samples": 1} if sample_load_failure is not None else {}),
            **({"reliability_samples": 1} if reliability_failure is not None else {}),
            **({"required_latency_samples": 1} if required_samples_failure is not None else {}),
            **({"latency_budget": 1} if budget_failure is not None else {}),
        },
        git=_collect_git_metadata(),
        environment=_collect_environment_metadata(),
        checks=[
            ValidationCheck(
                name=f"pytest.latency.{mode.value}",
                status=status,
                duration_s=duration_s,
                command=command,
                artifacts=check_artifacts,
            )
        ],
        failures=failures,
        latency=latency_payload,
        artifacts=artifacts,
    )

    _write_atomic(run_report_path, run.to_json())
    if requested_report_path is not None:
        _write_atomic(requested_report_path, run.to_json())
    _write_atomic(artifacts_root / "latest.json", run.to_json())
    return ValidationRunResult(
        run=run,
        run_dir=run_dir,
        report_path=requested_report_path or run_report_path,
        exit_code=exit_code,
    )


def run_live_validation(
    *,
    providers: Sequence[str] | None = None,
    surfaces: Sequence[str] | None = None,
    strict: bool = False,
    release: bool = False,
    artifacts_dir: str | Path = ".easycat/validation",
    report_path: str | Path | None = None,
    command_runner: CommandRunner | None = None,
    started_at: datetime | None = None,
) -> ValidationRunResult:
    command_runner = command_runner or _run_subprocess
    started_at = started_at or datetime.now(UTC)
    artifacts_root = Path(artifacts_dir)
    run_id = _make_run_id("live", started_at)
    run_dir = artifacts_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    run_report_path = run_dir / "report.json"
    requested_report_path = Path(report_path) if report_path is not None else None
    provider_report_dir = run_dir / "providers"
    provider_report_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"

    selected = select_provider_surfaces(providers=providers, surfaces=surfaces)
    selector_errors = _live_selector_errors(providers=providers, surfaces=surfaces)
    runtime_secret_values = _runtime_secret_values()
    explicit_provider_request = bool(providers)
    started_monotonic = time.perf_counter()
    checks: list[ValidationCheck] = []
    skips: list[ValidationSkip] = []
    failures: list[ValidationFailure] = []
    provider_checks: list[ProviderCheck] = []
    provider_reports: list[dict[str, object]] = []
    artifacts: dict[str, ArtifactRef] = {
        "report": ArtifactRef(kind="validation_report", path=str(run_report_path)),
        "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
        "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
    }
    if requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(requested_report_path),
        )

    stdout_log: list[str] = []
    stderr_log: list[str] = []
    tool_exit_codes: dict[str, int] = {}

    for selector_error in selector_errors:
        failures.append(selector_error)
        checks.append(
            ValidationCheck(
                name=selector_error.name,
                status="fail",
                duration_s=0.0,
                details=selector_error.details,
            )
        )

    for spec in selected:
        check_name = f"provider.{spec.provider}.{spec.surface}"
        credential_present = bool(
            spec.credential_env_var and os.environ.get(spec.credential_env_var)
        )
        missing_required_secret = bool(spec.credential_env_var and not credential_present)
        required_missing_should_fail = missing_required_secret and (
            release or (strict and explicit_provider_request)
        )

        check_started = time.perf_counter()
        if required_missing_should_fail:
            duration_s = time.perf_counter() - check_started
            failure = ValidationFailure(
                name=check_name,
                message=(
                    f"{spec.credential_env_var} is required for {spec.provider} {spec.surface}"
                ),
                failure_class="auth_or_quota",
            )
            failures.append(failure)
            checks.append(
                ValidationCheck(
                    name=check_name,
                    status="fail",
                    duration_s=duration_s,
                    details={"credential_env_var": spec.credential_env_var},
                )
            )
            provider_checks.append(
                ProviderCheck(
                    provider=spec.provider,
                    surface=spec.surface,
                    state=ProviderCheckState.FAILED_MISSING_REQUIRED_SECRET,
                    credential_env=spec.credential_env_var,
                    required=True,
                    failure_class="auth_or_quota",
                )
            )
            report = build_provider_capability_report(
                spec,
                live_checked_at=datetime.now(UTC),
                credential_present=False,
                live_status=ProviderCheckState.FAILED_MISSING_REQUIRED_SECRET.value,
                failure_class="auth_or_quota",
            ).to_dict()
        elif missing_required_secret:
            duration_s = time.perf_counter() - check_started
            skip = ValidationSkip(
                name=check_name,
                reason=f"{spec.credential_env_var} missing",
                expected=True,
            )
            skips.append(skip)
            checks.append(
                ValidationCheck(
                    name=check_name,
                    status="skip",
                    duration_s=duration_s,
                    details={"credential_env_var": spec.credential_env_var},
                )
            )
            provider_checks.append(
                ProviderCheck(
                    provider=spec.provider,
                    surface=spec.surface,
                    state=ProviderCheckState.SKIPPED_MISSING_SECRET,
                    credential_env=spec.credential_env_var,
                    required=False,
                )
            )
            report = build_provider_capability_report(
                spec,
                live_checked_at=datetime.now(UTC),
                credential_present=False,
                live_status="expected_skip",
            ).to_dict()
        else:
            command = _live_pytest_command(spec)
            command_result = command_runner(command)
            duration_s = time.perf_counter() - check_started
            stdout_log.append(command_result.stdout)
            stderr_log.append(command_result.stderr)
            tool_exit_codes[f"pytest.{spec.provider}.{spec.surface}"] = command_result.exit_code
            if command_result.exit_code == 0:
                check_status = "pass"
                state: ProviderCheckState | str = ProviderCheckState.PASSED
                failure_class = None
            else:
                check_status = "fail"
                state = ProviderCheckState.FAILED
                failure_message = (
                    command_result.stderr
                    or command_result.stdout
                    or f"pytest exited {command_result.exit_code}"
                )
                failure_message = redact_runtime_secrets(
                    failure_message,
                    runtime_secret_values,
                )
                failure_class = classify_live_failure(failure_message)
                failures.append(
                    ValidationFailure(
                        name=check_name,
                        message=failure_message,
                        failure_class=failure_class,
                    )
                )

            checks.append(
                ValidationCheck(
                    name=check_name,
                    status=check_status,
                    duration_s=duration_s,
                    command=command,
                )
            )
            provider_checks.append(
                ProviderCheck(
                    provider=spec.provider,
                    surface=spec.surface,
                    state=state,
                    credential_env=spec.credential_env_var or None,
                    required=bool(spec.credential_env_var),
                    failure_class=failure_class,
                )
            )
            report = build_provider_capability_report(
                spec,
                live_checked_at=datetime.now(UTC),
                credential_present=credential_present,
                live_status=state.value if isinstance(state, ProviderCheckState) else state,
                failure_class=failure_class,
            ).to_dict()

        report_path_for_provider = provider_report_dir / f"{spec.artifact_key}.json"
        _write_atomic(
            report_path_for_provider,
            json.dumps(report, indent=2, sort_keys=True) + "\n",
        )
        artifacts[spec.artifact_key] = ArtifactRef(
            kind="provider_capability_report",
            path=str(report_path_for_provider),
        )
        provider_reports.append(report)

    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)
    exit_code = 1 if failures else 0
    status = "fail" if failures else "pass"

    stdout_path.write_text(redact_runtime_secrets("\n".join(stdout_log), runtime_secret_values))
    stderr_path.write_text(redact_runtime_secrets("\n".join(stderr_log), runtime_secret_values))
    run = ValidationRun(
        run_id=run_id,
        command=_live_validation_command(
            providers=providers,
            surfaces=surfaces,
            strict=strict,
            release=release,
        ),
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes=tool_exit_codes,
        git=_collect_git_metadata(),
        environment=_collect_environment_metadata(),
        checks=checks,
        skips=skips,
        failures=failures,
        providers=provider_checks,
        provider_reports=provider_reports,
        artifacts=artifacts,
    )

    _write_atomic(run_report_path, run.to_json())
    if requested_report_path is not None:
        _write_atomic(requested_report_path, run.to_json())
    _write_atomic(artifacts_root / "latest.json", run.to_json())
    return ValidationRunResult(
        run=run,
        run_dir=run_dir,
        report_path=requested_report_path or run_report_path,
        exit_code=exit_code,
    )


def classify_live_failure(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("quota", "rate limit", "429")):
        return "provider_quota"
    if any(token in lowered for token in ("unauthorized", "forbidden", "401", "403")):
        return "auth_or_quota"
    if any(token in lowered for token in ("schema", "unknown event", "drift")):
        return "provider_drift"
    if any(token in lowered for token in ("timeout", "dns", "network", "connection")):
        return "network"
    if any(token in lowered for token in ("assert", "failed", "traceback")):
        return "easycat_regression"
    return "environment"


def _load_reliability_failure(path: Path) -> ValidationFailure | None:
    if not path.exists():
        return None
    try:
        load_reliability_samples(path.read_text())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return ValidationFailure(
            name="reliability.samples",
            message=f"could not load reliability samples: {exc}",
            failure_class="reliability_artifact_error",
        )
    return None


def _live_selector_errors(
    *,
    providers: Sequence[str] | None,
    surfaces: Sequence[str] | None,
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    known_providers = known_live_providers()
    for provider in {provider.strip().lower() for provider in providers or () if provider.strip()}:
        if provider not in known_providers:
            failures.append(
                ValidationFailure(
                    name="provider.selector",
                    message=f"unknown live provider selector: {provider}",
                    failure_class="environment",
                    details={"provider": provider, "known_providers": sorted(known_providers)},
                )
            )

    known_surfaces = known_live_surfaces()
    for surface in {surface.strip().lower() for surface in surfaces or () if surface.strip()}:
        if surface not in known_surfaces:
            failures.append(
                ValidationFailure(
                    name="provider.selector",
                    message=f"unknown live surface selector: {surface}",
                    failure_class="environment",
                    details={"surface": surface, "known_surfaces": sorted(known_surfaces)},
                )
            )
    return failures


def _runtime_secret_values() -> tuple[str, ...]:
    return tuple(value for name in PROVIDER_ENV_VARS if (value := os.environ.get(name)))


def _latency_failure_sample(mode: LatencyMode, message: str) -> LatencySample:
    failure_class = classify_latency_failure(message)
    return LatencySample(
        sample_id=f"{mode.value}-failure-{uuid.uuid4().hex[:12]}",
        condition_id=f"latency_{mode.value}",
        warmup=False,
        timestamp_source="time.monotonic",
        stages=LatencyStageDurations(),
        missing_stage_reason=message,
        failure_class=failure_class,
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
    parser.add_argument("--report", help="Optional additional validation report JSON path.")
    parser.add_argument("--junit", help="Optional JUnit XML output path.")
    parser.add_argument("--junit-prefix", help="Optional pytest JUnit prefix.")
    args = parser.parse_args(argv)

    result = run_validation_slice(
        args.slice,
        artifacts_dir=args.artifacts_dir,
        report_path=args.report,
        junit_path=args.junit,
        junit_prefix=args.junit_prefix,
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


def _live_pytest_command(spec: ProviderSurfaceSpec) -> list[str]:
    command = [*_pytest_command_prefix(), "-q"]
    if spec.live_pytest_target:
        command.append(_resolve_validation_test_arg(spec.live_pytest_target))
    command.extend(["-m", _live_marker_expression(spec)])
    return command


def _pytest_command_prefix() -> list[str]:
    raw = os.environ.get("EASYCAT_VALIDATION_PYTEST_COMMAND")
    if raw:
        return shlex.split(raw)
    return ["uv", "run", "pytest"]


def _validation_test_paths() -> list[str]:
    raw = os.environ.get("EASYCAT_VALIDATION_TEST_PATHS")
    if not raw:
        return []
    return [path for path in raw.split(os.pathsep) if path]


def _resolve_validation_test_arg(arg: str) -> str:
    test_root = os.environ.get("EASYCAT_VALIDATION_TEST_ROOT")
    if not test_root or arg.startswith("/") or not arg.startswith("tests/"):
        return arg
    return str(Path(test_root) / arg.removeprefix("tests/"))


def _live_marker_expression(spec: ProviderSurfaceSpec) -> str:
    markers = ["integration_live"]
    provider_marker = _provider_marker(spec.provider)
    if provider_marker is not None:
        markers.append(provider_marker)
    markers.append(f"surface_{spec.surface.removesuffix('_bridge')}")
    markers.append("not flaky")
    return " and ".join(markers)


def _provider_marker(provider: str) -> str | None:
    normalized = provider.removeprefix("openai-")
    if provider.startswith("openai"):
        normalized = "openai"
    if normalized in {"openai", "deepgram", "elevenlabs", "cartesia"}:
        return f"provider_{normalized}"
    return None


def _live_validation_command(
    *,
    providers: Sequence[str] | None,
    surfaces: Sequence[str] | None,
    strict: bool,
    release: bool,
) -> list[str]:
    command = ["easycat", "validate", "live"]
    for provider in providers or ():
        command.extend(["--provider", provider])
    for surface in surfaces or ():
        command.extend(["--surface", surface])
    if strict:
        command.append("--strict")
    if release:
        command.append("--release")
    return command


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
