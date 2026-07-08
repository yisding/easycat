from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easycat._provider_catalog import provider_names
from easycat.validation._lane_harness import (
    PROVIDER_ENV_VARS,
    ValidationRunResult,
    _finish_lane_run,
    _start_lane_run,
    _write_atomic,
)
from easycat.validation.latency import (
    DEFAULT_RELIABILITY_BUDGETS,
    FailureCategory,
    LatencyComparisonThresholds,
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilityBudget,
    ReliabilitySample,
    build_latency_artifact,
    build_reliability_artifact,
    classify_failure_category,
    classify_latency_failure,
    compare_latency_baseline,
    evaluate_reliability_budgets,
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
    ProviderCheck,
    ProviderCheckState,
    ValidationCheck,
    ValidationFailure,
    ValidationSkip,
    redact_runtime_secrets,
)

VALIDATION_SELECTORS = {
    "quick": (
        "not integration_socket and not integration_live and not integration_external "
        "and not contract and not slow and not stress and not flaky"
    ),
    "socket": "integration_socket and not integration_live and not flaky",
    "stress": "stress and not integration_live and not flaky",
    "contracts": "contract and not integration_live and not flaky",
}

DEFAULT_RELEASE_EXTRAS = ("openai", "openai-agents")
DEFAULT_RELEASE_PROVIDERS = ("openai",)
DEFAULT_RELEASE_SURFACES = ("stt", "tts")
RELEASE_SLICES = ("quick", "stress", "contracts")
RELEASE_TEST_DEPENDENCIES = ("pytest", "pytest-asyncio", "hypothesis")
_RELEASE_IMPORT_SMOKE = """
import os
import pathlib

import easycat

package_path = pathlib.Path(easycat.__file__).resolve()
source_root = pathlib.Path(os.environ["EASYCAT_RELEASE_SOURCE_ROOT"]).resolve()
print(package_path)
assert "site-packages" in str(package_path), package_path
assert not package_path.is_relative_to(source_root), package_path
"""
_RELEASE_PUBLIC_API_SMOKE = """
import os
import pathlib
import re

import easycat

source_root = pathlib.Path(os.environ["EASYCAT_RELEASE_SOURCE_ROOT"]).resolve()
doc_path = source_root / "docs" / "public-api.md"
section = doc_path.read_text(encoding="utf-8").split("## Top-Level Allowlist", 1)[1]
documented = tuple(re.findall(r"^- `([^`]+)`", section, flags=re.MULTILINE))
assert documented, "docs/public-api.md has no top-level allowlist entries"
missing = sorted(set(easycat.__all__) - set(documented))
extra = sorted(set(documented) - set(easycat.__all__))
assert not missing, "docs/public-api.md missing exports: " + ", ".join(missing)
assert not extra, "docs/public-api.md lists non-exported names: " + ", ".join(extra)
for name in documented:
    getattr(easycat, name)
print(f"validated {len(documented)} documented public API exports")
"""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[..., CommandResult]


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
    ctx = _start_lane_run(
        slice_name,
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    run_dir = ctx.run_dir

    junit_path = Path(junit_path) if junit_path is not None else run_dir / "junit.xml"
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    reliability_samples_path = run_dir / "reliability" / "samples.json"
    reliability_samples_path.parent.mkdir(parents=True, exist_ok=True)
    webrtc_stats_path = run_dir / "webrtc" / "stats.jsonl"
    if slice_name == "socket":
        webrtc_stats_path.parent.mkdir(parents=True, exist_ok=True)

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

    command_env = {
        **os.environ,
        "EASYCAT_RELIABILITY_SAMPLES_PATH": str(reliability_samples_path),
    }
    if slice_name == "socket":
        command_env["EASYCAT_WEBRTC_STATS_PATH"] = str(webrtc_stats_path)
    runtime_secret_values = _runtime_secret_values()
    started_monotonic = time.perf_counter()
    result = command_runner(command, env=command_env)
    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)

    stdout_path.write_text(redact_runtime_secrets(result.stdout, runtime_secret_values))
    stderr_path.write_text(redact_runtime_secrets(result.stderr, runtime_secret_values))
    if junit_path.exists():
        junit_path.write_text(
            redact_runtime_secrets(junit_path.read_text(), runtime_secret_values)
        )

    exit_code = validation_exit_code_from_pytest(result.exit_code)
    reliability_failure = _load_reliability_failure(reliability_samples_path)
    reliability_budget_failure: ValidationFailure | None = None
    reliability_payload: dict[str, object] | None = None
    if reliability_samples_path.exists() and reliability_failure is None:
        reliability_samples = load_reliability_samples(reliability_samples_path.read_text())
        reliability_payload = build_reliability_artifact(
            samples=reliability_samples,
            generated_at=finished_at,
        )
        reliability_budget_failure = _reliability_budget_failure(reliability_samples)
    if reliability_failure is not None or reliability_budget_failure is not None:
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
    if slice_name == "socket" and webrtc_stats_path.exists():
        check_artifacts["webrtc_stats"] = ArtifactRef(
            kind="webrtc_stats",
            path=str(webrtc_stats_path),
        )

    artifacts: dict[str, ArtifactRef] = {**ctx.artifacts, **check_artifacts}
    if ctx.requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(ctx.requested_report_path),
        )

    failures = []
    if result.exit_code != 0:
        failures.append(
            ValidationFailure(
                name=f"pytest.{slice_name}",
                message=redact_runtime_secrets(
                    result.stderr or result.stdout or f"pytest exited {result.exit_code}",
                    runtime_secret_values,
                ),
            )
        )
    if reliability_failure is not None:
        failures.append(reliability_failure)
    if reliability_budget_failure is not None:
        failures.append(reliability_budget_failure)

    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes={
            "pytest": result.exit_code,
            **({"reliability_samples": 1} if reliability_failure is not None else {}),
            **({"reliability_budget": 1} if reliability_budget_failure is not None else {}),
        },
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
    mode = LatencyMode(mode)
    # A SWEEP that legitimately produces zero samples (skipped tests, missing
    # credentials) must fail rather than silently report pass with an empty
    # percentiles block. SMOKE may legitimately produce no samples, so it stays
    # opt-in. An explicit ``require_samples`` value always wins.
    if require_samples is None:
        require_samples = mode is LatencyMode.SWEEP
    command_runner = command_runner or _run_subprocess
    started_at = started_at or datetime.now(UTC)
    artifacts_root = Path(artifacts_dir)
    ctx = _start_lane_run(
        f"latency-{mode.value}",
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    run_dir = ctx.run_dir

    junit_path = run_dir / "junit.xml"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
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

    command_env = {
        **os.environ,
        "EASYCAT_LATENCY_SAMPLES_PATH": str(samples_path),
        "EASYCAT_RELIABILITY_SAMPLES_PATH": str(reliability_samples_path),
    }
    runtime_secret_values = _runtime_secret_values()
    started_monotonic = time.perf_counter()
    result = command_runner(command, env=command_env)
    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)

    stdout_path.write_text(redact_runtime_secrets(result.stdout, runtime_secret_values))
    stderr_path.write_text(redact_runtime_secrets(result.stderr, runtime_secret_values))
    if junit_path.exists():
        junit_path.write_text(
            redact_runtime_secrets(junit_path.read_text(), runtime_secret_values)
        )
    exit_code = validation_exit_code_from_pytest(result.exit_code)
    sample_load_failure: ValidationFailure | None = None
    try:
        samples = load_latency_samples(samples_path.read_text()) if samples_path.exists() else []
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        samples = []
        sample_load_failure = ValidationFailure(
            name="latency.samples",
            message=redact_runtime_secrets(
                f"could not load latency samples: {exc}",
                runtime_secret_values,
            ),
            failure_class="latency_artifact_error",
        )
    reliability_failure = _redact_validation_failure(
        _load_reliability_failure(reliability_samples_path),
        runtime_secret_values,
    )
    reliability_samples: list[ReliabilitySample] = []
    reliability_budget_failure: ValidationFailure | None = None
    if reliability_samples_path.exists() and reliability_failure is None:
        reliability_samples = load_reliability_samples(reliability_samples_path.read_text())
        reliability_budget_failure = _reliability_budget_failure(reliability_samples)

    required_samples_failure: ValidationFailure | None = None
    if require_samples and not samples:
        required_samples_failure = ValidationFailure(
            name="latency.samples",
            message="required latency validation produced no samples",
            failure_class="latency_artifact_error",
        )

    failure_message = result.stderr or result.stdout or f"pytest exited {result.exit_code}"
    failure_message = redact_runtime_secrets(failure_message, runtime_secret_values)
    if exit_code != 0 and not samples:
        samples.append(_latency_failure_sample(mode, failure_message))

    latency_payload = build_latency_artifact(
        mode=mode,
        samples=samples,
        reliability_samples=reliability_samples,
        generated_at=finished_at,
    )

    baseline_load_failure: ValidationFailure | None = None
    baseline_comparison: dict[str, Any] | None = None
    if baseline_path is not None:
        baseline_comparison, baseline_load_failure = _compare_against_baseline(
            current=latency_payload,
            baseline_path=Path(baseline_path),
        )
        if baseline_comparison is not None:
            baseline_comparison = _redact_runtime_json(
                baseline_comparison,
                runtime_secret_values,
            )
            latency_payload["baseline"] = baseline_comparison
        baseline_load_failure = _redact_validation_failure(
            baseline_load_failure,
            runtime_secret_values,
        )

    latency_payload = _redact_runtime_json(latency_payload, runtime_secret_values)

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

    baseline_regression_failure = _baseline_comparison_failure(baseline_comparison)

    if (
        sample_load_failure is not None
        or reliability_failure is not None
        or reliability_budget_failure is not None
        or required_samples_failure is not None
        or budget_failure is not None
        or baseline_load_failure is not None
        or baseline_regression_failure is not None
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
    if reliability_budget_failure is not None:
        failures.append(reliability_budget_failure)
    if required_samples_failure is not None:
        failures.append(required_samples_failure)
    if budget_failure is not None:
        failures.append(budget_failure)
    if baseline_load_failure is not None:
        failures.append(baseline_load_failure)
    if baseline_regression_failure is not None:
        failures.append(baseline_regression_failure)

    artifacts: dict[str, ArtifactRef] = {**ctx.artifacts, **check_artifacts}
    if ctx.requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(ctx.requested_report_path),
        )

    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
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
            **({"reliability_budget": 1} if reliability_budget_failure is not None else {}),
            **({"required_latency_samples": 1} if required_samples_failure is not None else {}),
            **({"latency_budget": 1} if budget_failure is not None else {}),
            **({"latency_baseline": 1} if baseline_load_failure is not None else {}),
            **(
                {"latency_baseline_regression": 1}
                if baseline_regression_failure is not None
                else {}
            ),
        },
        checks=_latency_checks(
            mode=mode,
            pytest_exit_code=result.exit_code,
            duration_s=duration_s,
            command=command,
            check_artifacts=check_artifacts,
            sample_load_failure=sample_load_failure,
            required_samples_failure=required_samples_failure,
            reliability_failure=reliability_failure,
            reliability_budget_failure=reliability_budget_failure,
            budget_failure=budget_failure,
            budget_violations=budget_violations,
            baseline_comparison=baseline_comparison,
            baseline_load_failure=baseline_load_failure,
            baseline_regression_failure=baseline_regression_failure,
        ),
        failures=failures,
        latency=latency_payload,
        artifacts=artifacts,
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
    ctx = _start_lane_run(
        "live",
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    run_dir = ctx.run_dir

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
        **ctx.artifacts,
        "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
        "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
    }
    if ctx.requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(ctx.requested_report_path),
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
            command_result = command_runner(command, env={**os.environ})
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
    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
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
        checks=checks,
        skips=skips,
        failures=failures,
        providers=provider_checks,
        provider_reports=provider_reports,
        artifacts=artifacts,
    )


def run_release_validation(
    *,
    artifacts_dir: str | Path = ".easycat/validation",
    report_path: str | Path | None = None,
    python_version: str | None = None,
    extras: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    surfaces: Sequence[str] | None = None,
    latency_mode: LatencyMode | str = LatencyMode.SWEEP,
    command_runner: CommandRunner | None = None,
    started_at: datetime | None = None,
) -> ValidationRunResult:
    """Run the strict release gate against a cleanly installed wheel."""
    command_runner = command_runner or _run_subprocess
    started_at = started_at or datetime.now(UTC)
    started_monotonic = time.perf_counter()
    artifacts_root = Path(artifacts_dir)
    ctx = _start_lane_run(
        "release",
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    run_id = ctx.run_id
    run_dir = ctx.run_dir

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    dist_dir = run_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    release_tmp_dir = Path(tempfile.gettempdir()) / f"easycat-release-{run_id}"
    venv_dir = release_tmp_dir / "venv"
    outside_dir = release_tmp_dir / "outside-source"
    outside_dir.mkdir(parents=True, exist_ok=True)

    source_root = Path.cwd().resolve()
    python_version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
    release_extras = tuple(extras) if extras is not None else DEFAULT_RELEASE_EXTRAS
    release_providers = tuple(providers) if providers is not None else DEFAULT_RELEASE_PROVIDERS
    release_surfaces = tuple(surfaces) if surfaces is not None else DEFAULT_RELEASE_SURFACES
    latency_mode = LatencyMode(latency_mode)
    runtime_secret_values = _runtime_secret_values()

    checks: list[ValidationCheck] = []
    failures: list[ValidationFailure] = []
    artifacts: dict[str, ArtifactRef] = {
        **ctx.artifacts,
        "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
        "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
        "dist": ArtifactRef(kind="directory", path=str(dist_dir)),
    }
    if ctx.requested_report_path is not None:
        artifacts["requested_report"] = ArtifactRef(
            kind="validation_report",
            path=str(ctx.requested_report_path),
        )
    tool_exit_codes: dict[str, int] = {}
    stdout_log: list[str] = []
    stderr_log: list[str] = []

    def record_command(
        name: str,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> bool:
        started = time.perf_counter()
        result = command_runner(command, env=env, cwd=cwd)
        duration_s = time.perf_counter() - started
        tool_exit_codes[name] = result.exit_code
        status = "pass" if result.exit_code == 0 else "fail"
        details = {"cwd": str(cwd)} if cwd is not None else {}
        checks.append(
            ValidationCheck(
                name=name,
                status=status,
                duration_s=duration_s,
                command=command,
                details=details,
            )
        )
        if result.stdout:
            stdout_log.append(f"{name}\n{result.stdout}")
        if result.stderr:
            stderr_log.append(f"{name}\n{result.stderr}")
        if result.exit_code != 0:
            failures.append(
                ValidationFailure(
                    name=name,
                    message=redact_runtime_secrets(
                        result.stderr or result.stdout or f"{name} exited {result.exit_code}",
                        runtime_secret_values,
                    ),
                    failure_class="release_validation",
                    details=details,
                )
            )
        return result.exit_code == 0

    def record_failure(
        name: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        tool_exit_codes[name] = 1
        checks.append(
            ValidationCheck(
                name=name,
                status="fail",
                duration_s=0.0,
                details=details or {},
            )
        )
        failures.append(
            ValidationFailure(
                name=name,
                message=message,
                failure_class="release_validation",
                details=details or {},
            )
        )

    build_ok = record_command(
        "release.build",
        ["uv", "build", "--sdist", "--wheel", "-o", str(dist_dir)],
        env={**os.environ},
        cwd=source_root,
    )
    wheel_path = _release_wheel_path(dist_dir) if build_ok else None
    if build_ok and wheel_path is None:
        record_failure(
            "release.wheel",
            f"uv build did not create exactly one wheel under {dist_dir}",
            details={"dist_dir": str(dist_dir)},
        )
    dist_paths = _release_dist_paths(dist_dir) if build_ok else []
    if build_ok and dist_paths:
        record_command(
            "release.metadata",
            ["uvx", "twine", "check", *[str(path) for path in dist_paths]],
            env={**os.environ},
            cwd=source_root,
        )
    elif build_ok:
        record_failure(
            "release.metadata",
            f"uv build did not create any metadata-checkable distributions under {dist_dir}",
            details={"dist_dir": str(dist_dir)},
        )

    venv_ok = False
    install_ok = False
    smoke_ok = False
    if wheel_path is not None:
        venv_ok = record_command(
            "release.venv",
            ["uv", "venv", str(venv_dir), "--python", python_version],
            env={**os.environ},
            cwd=source_root,
        )
    venv_python = _venv_executable(venv_dir, "python")
    venv_easycat = _venv_executable(venv_dir, "easycat")
    if venv_ok and wheel_path is not None:
        install_ok = record_command(
            "release.install",
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(venv_python),
                _release_package_spec(wheel_path, release_extras),
            ],
            env={**os.environ},
            cwd=source_root,
        )
        install_ok = (
            record_command(
                "release.install-test-tools",
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(venv_python),
                    *RELEASE_TEST_DEPENDENCIES,
                ],
                env={**os.environ},
                cwd=source_root,
            )
            and install_ok
        )

    outside_env = {
        **os.environ,
        "PYTHONPATH": "",
        "EASYCAT_RELEASE_SOURCE_ROOT": str(source_root),
    }
    if install_ok:
        import_ok = record_command(
            "release.import-smoke",
            [str(venv_python), "-c", _RELEASE_IMPORT_SMOKE],
            env=outside_env,
            cwd=outside_dir,
        )
        public_api_ok = record_command(
            "release.public-api-smoke",
            [str(venv_python), "-c", _RELEASE_PUBLIC_API_SMOKE],
            env=outside_env,
            cwd=outside_dir,
        )
        help_ok = record_command(
            "release.help-smoke",
            [str(venv_easycat), "--help"],
            env=outside_env,
            cwd=outside_dir,
        )
        module_ok = record_command(
            "release.module-smoke",
            [str(venv_python), "-m", "easycat"],
            env=outside_env,
            cwd=outside_dir,
        )
        init_ok = record_command(
            "release.init-smoke",
            [
                str(venv_easycat),
                "init",
                str(outside_dir / "easycat-scaffold-smoke"),
                "--template",
                "text-chat",
                "--no-git",
                "--json",
            ],
            env=outside_env,
            cwd=outside_dir,
        )
        doctor_ok = record_command(
            "release.doctor",
            [str(venv_easycat), "doctor", "--json"],
            env=outside_env,
            cwd=outside_dir,
        )
        cli_smoke_ok = record_command(
            "release.cli-smoke",
            [str(venv_python), "-m", "pytest", str(source_root / "tests/cli/test_app.py"), "-q"],
            env=outside_env,
            cwd=outside_dir,
        )
        smoke_ok = (
            import_ok
            and public_api_ok
            and help_ok
            and module_ok
            and init_ok
            and doctor_ok
            and cli_smoke_ok
        )

    def child_command_runner(command: list[str], *, env: Mapping[str, str]) -> CommandResult:
        child_env = {
            **env,
            "PYTHONPATH": "",
            "EASYCAT_VALIDATION_PYTEST_COMMAND": shlex.join([str(venv_python), "-m", "pytest"]),
            "EASYCAT_VALIDATION_TEST_PATHS": str(source_root / "tests"),
            "EASYCAT_VALIDATION_TEST_ROOT": str(source_root / "tests"),
        }
        return command_runner(command, env=child_env, cwd=outside_dir)

    def record_child_result(name: str, result: ValidationRunResult) -> None:
        artifact_key = name.removeprefix("release.").replace(".", "_") + "_report"
        report_ref = ArtifactRef(kind="validation_report", path=str(result.report_path))
        artifacts[artifact_key] = report_ref
        tool_exit_codes[name] = result.exit_code
        checks.append(
            ValidationCheck(
                name=name,
                status=result.run.status,
                duration_s=result.run.duration_s,
                command=result.run.command,
                artifacts={"report": report_ref},
                details={"run_id": result.run.run_id},
            )
        )
        if result.exit_code != 0:
            failures.append(
                ValidationFailure(
                    name=name,
                    message=f"{name} failed; see {result.report_path}",
                    failure_class="release_validation",
                    details={"report_path": str(result.report_path)},
                )
            )

    if smoke_ok:
        validation_env = {
            "PYTHONPATH": "",
            "EASYCAT_VALIDATION_PYTEST_COMMAND": shlex.join([str(venv_python), "-m", "pytest"]),
            "EASYCAT_VALIDATION_TEST_PATHS": str(source_root / "tests"),
            "EASYCAT_VALIDATION_TEST_ROOT": str(source_root / "tests"),
        }
        with _temporary_environ(validation_env):
            for slice_name in RELEASE_SLICES:
                child = run_validation_slice(
                    slice_name,
                    artifacts_dir=artifacts_root,
                    junit_prefix=f"release-{slice_name}",
                    command_runner=child_command_runner,
                )
                record_child_result(f"release.{slice_name}", child)

            live_child = run_live_validation(
                providers=release_providers,
                surfaces=release_surfaces,
                release=True,
                artifacts_dir=artifacts_root,
                command_runner=child_command_runner,
            )
            record_child_result("release.live", live_child)

            latency_child = run_latency_validation(
                latency_mode,
                artifacts_dir=artifacts_root,
                require_samples=True,
                command_runner=child_command_runner,
            )
            record_child_result(f"release.latency.{latency_mode.value}", latency_child)

    finished_at = datetime.now(UTC)
    duration_s = time.perf_counter() - started_monotonic
    exit_code = 1 if failures else 0
    status = "fail" if failures else "pass"

    stdout_path.write_text(redact_runtime_secrets("\n".join(stdout_log), runtime_secret_values))
    stderr_path.write_text(redact_runtime_secrets("\n".join(stderr_log), runtime_secret_values))
    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
        command=_release_validation_command(
            python_version=python_version,
            extras=release_extras,
            providers=release_providers,
            surfaces=release_surfaces,
            latency_mode=latency_mode,
        ),
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes=tool_exit_codes,
        checks=checks,
        failures=failures,
        artifacts=artifacts,
    )


# Live-path vocabulary (preserved for back-compat and `_capability_status`).
_LIVE_FAILURE_CLASSES: dict[FailureCategory, str] = {
    FailureCategory.AUTH: "auth_or_quota",
    FailureCategory.QUOTA: "provider_quota",
    FailureCategory.TIMEOUT: "network",
    FailureCategory.NETWORK: "network",
    FailureCategory.DRIFT: "provider_drift",
    FailureCategory.REGRESSION: "easycat_regression",
    FailureCategory.OTHER: "environment",
}


def classify_live_failure(message: str) -> str:
    return _LIVE_FAILURE_CLASSES[classify_failure_category(message)]


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


def _reliability_budget_failure(
    samples: Sequence[ReliabilitySample],
    budgets: Sequence[ReliabilityBudget] = DEFAULT_RELIABILITY_BUDGETS,
) -> ValidationFailure | None:
    """Surface eligible reliability samples that breach a reliability budget.

    Mirrors the latency budget gate: a saturated event loop, a memory leak,
    dropped audio frames, or a degraded journal in any eligible sample fails
    the run instead of silently passing once the samples merely parse.
    """
    violations = evaluate_reliability_budgets(samples, budgets)
    if not violations:
        return None
    return ValidationFailure(
        name="reliability.budget",
        message="reliability budget violated",
        failure_class="reliability_budget",
        details={"violations": [violation.to_dict() for violation in violations]},
    )


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


def _redact_runtime_json(value: Any, secrets: Sequence[str]) -> Any:
    """Apply exact runtime-secret redaction to a JSON-compatible payload."""

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


def _runtime_secret_values() -> tuple[str, ...]:
    return tuple(value for name in PROVIDER_ENV_VARS if (value := os.environ.get(name)))


def _latency_checks(
    *,
    mode: LatencyMode,
    pytest_exit_code: int,
    duration_s: float,
    command: Sequence[str],
    check_artifacts: dict[str, ArtifactRef],
    sample_load_failure: ValidationFailure | None,
    required_samples_failure: ValidationFailure | None,
    reliability_failure: ValidationFailure | None,
    reliability_budget_failure: ValidationFailure | None,
    budget_failure: ValidationFailure | None,
    budget_violations: Sequence[Any],
    baseline_comparison: dict[str, Any] | None = None,
    baseline_load_failure: ValidationFailure | None = None,
    baseline_regression_failure: ValidationFailure | None = None,
) -> list[ValidationCheck]:
    """Split pytest + budget + baseline evaluation into distinct checks.

    A budget failure used to share the single `pytest.latency.<mode>` check
    with pytest, so a passing pytest exit (0) with a budget violation would
    surface as a failed pytest check — misattributing the failure. Reporting
    them separately lets consumers tell which gate actually failed.

    Budget evaluation is skipped for SMOKE mode upstream in
    `build_latency_artifact`, so no `latency.budget` check is recorded for
    smoke runs. A `latency.baseline` check is only recorded when a baseline
    artifact was supplied (or failed to load).
    """
    checks: list[ValidationCheck] = [
        ValidationCheck(
            name=f"pytest.latency.{mode.value}",
            status="pass" if pytest_exit_code == 0 else "fail",
            duration_s=duration_s,
            command=command,
            artifacts=check_artifacts,
        )
    ]

    sample_failures = [
        failure
        for failure in (sample_load_failure, required_samples_failure)
        if failure is not None
    ]
    if sample_failures:
        checks.append(_latency_failure_check("latency.samples", sample_failures, check_artifacts))
    if reliability_failure is not None:
        checks.append(
            _latency_failure_check("reliability.samples", [reliability_failure], check_artifacts)
        )
    if reliability_budget_failure is not None:
        checks.append(
            _latency_failure_check(
                "reliability.budget",
                [reliability_budget_failure],
                check_artifacts,
            )
        )

    if mode is not LatencyMode.SMOKE:
        budget_artifacts: dict[str, ArtifactRef] = {}
        if "latency" in check_artifacts:
            budget_artifacts["latency"] = check_artifacts["latency"]
        checks.append(
            ValidationCheck(
                name="latency.budget",
                status="fail" if budget_failure is not None else "pass",
                duration_s=0.0,
                artifacts=budget_artifacts,
                details={"violations": list(budget_violations)} if budget_violations else {},
            )
        )
    if baseline_comparison is not None or baseline_load_failure is not None:
        baseline_failed = (
            baseline_load_failure is not None or baseline_regression_failure is not None
        )
        details: dict[str, Any] = {}
        if baseline_comparison is not None:
            details = {
                "status": baseline_comparison.get("status"),
                "conditions": baseline_comparison.get("conditions", []),
            }
        elif baseline_load_failure is not None:
            details = {"message": baseline_load_failure.message}
        checks.append(
            ValidationCheck(
                name="latency.baseline",
                status="fail" if baseline_failed else "pass",
                duration_s=0.0,
                details=details,
            )
        )
    return checks


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
    """Load a stored baseline artifact and compare the current run against it.

    Returns ``(comparison, failure)`` where ``comparison`` is the
    ``compare_latency_baseline`` result (embedded into the latency artifact's
    ``baseline`` field) and ``failure`` describes an unreadable/invalid
    baseline file. Exactly one of the two is meaningful per call.
    """
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
    """Surface a fail/drift baseline comparison as a ValidationFailure."""
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


# Marker key/value written into a synthetic failure sample's free-form ``debug``
# map so consumers can filter the fabricated entry out of the ``samples`` list.
# It never measured a real turn; it only carries the pytest failure reason.
LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY = "synthetic"
LATENCY_SYNTHETIC_FAILURE_SAMPLE = "pytest_failure"


def _latency_failure_sample(mode: LatencyMode, message: str) -> LatencySample:
    failure_class = classify_latency_failure(message)
    return LatencySample(
        sample_id=f"{mode.value}-failure-{uuid.uuid4().hex[:12]}",
        condition_id=f"latency_{mode.value}",
        warmup=False,
        timestamp_source="time.monotonic",
        stages=LatencyStageDurations(),
        debug={LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY: LATENCY_SYNTHETIC_FAILURE_SAMPLE},
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


def _release_wheel_path(dist_dir: Path) -> Path | None:
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        return None
    return wheels[0]


def _release_dist_paths(dist_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in dist_dir.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )


def _release_package_spec(wheel_path: Path, extras: Sequence[str]) -> str:
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"easycat{suffix} @ file://{wheel_path.resolve()}"


def _venv_executable(venv_dir: Path, executable: str) -> Path:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" and not executable.endswith(".exe") else ""
    return venv_dir / bin_dir / f"{executable}{suffix}"


def _release_validation_command(
    *,
    python_version: str,
    extras: Sequence[str],
    providers: Sequence[str],
    surfaces: Sequence[str],
    latency_mode: LatencyMode,
) -> list[str]:
    command = ["easycat", "validate", "release", "--python", python_version]
    for extra in extras:
        command.extend(["--extra", extra])
    for provider in providers:
        command.extend(["--provider", provider])
    for surface in surfaces:
        command.extend(["--surface", surface])
    if latency_mode is LatencyMode.SMOKE:
        command.append("--latency-smoke")
    else:
        command.append("--latency-sweep")
    return command


@contextmanager
def _temporary_environ(overrides: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _run_subprocess(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> CommandResult:
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
    # OpenAI variants (openai-realtime, openai-agents, ...) all share the
    # provider_openai marker; the known-provider set comes from the
    # STT/TTS provider catalogs instead of a hardcoded copy.
    normalized = "openai" if provider.startswith("openai") else provider
    if normalized in provider_names():
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
