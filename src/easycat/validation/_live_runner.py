"""Live provider validation orchestration and capability-report projection."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from easycat._provider_catalog import provider_names
from easycat.validation._environment import runtime_secret_values
from easycat.validation._failure_classification import (
    FailureCategory,
    classify_failure_category,
)
from easycat.validation._lane_harness import (
    LaneRunContext,
    ValidationRunResult,
    _finish_lane_run,
    _start_lane_run,
    _write_atomic,
)
from easycat.validation._runner_support import (
    CommandResult,
    CommandRunner,
    pytest_command_prefix,
    resolve_validation_test_arg,
    run_subprocess,
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
    ValidationStatus,
    redact_runtime_secrets,
)

_LIVE_FAILURE_CLASSES: dict[FailureCategory, str] = {
    FailureCategory.AUTH: "auth_or_quota",
    FailureCategory.QUOTA: "provider_quota",
    FailureCategory.TIMEOUT: "network",
    FailureCategory.NETWORK: "network",
    FailureCategory.DRIFT: "provider_drift",
    FailureCategory.REGRESSION: "easycat_regression",
    FailureCategory.OTHER: "environment",
}


@dataclass(frozen=True, slots=True)
class _LiveRequest:
    """Normalized provider selectors and credential enforcement policy."""

    providers: tuple[str, ...]
    surfaces: tuple[str, ...]
    strict: bool
    release: bool

    @classmethod
    def create(
        cls,
        *,
        providers: Sequence[str] | None,
        surfaces: Sequence[str] | None,
        strict: bool,
        release: bool,
    ) -> _LiveRequest:
        return cls(
            providers=tuple(providers or ()),
            surfaces=tuple(surfaces or ()),
            strict=strict,
            release=release,
        )

    @property
    def explicit_provider(self) -> bool:
        return bool(self.providers)

    def requires_credential(self, *, missing: bool) -> bool:
        return missing and (self.release or (self.strict and self.explicit_provider))

    def command(self) -> list[str]:
        command = ["easycat", "validate", "live"]
        for provider in self.providers:
            command.extend(["--provider", provider])
        for surface in self.surfaces:
            command.extend(["--surface", surface])
        if self.strict:
            command.append("--strict")
        if self.release:
            command.append("--release")
        return command


@dataclass(frozen=True, slots=True)
class _LivePaths:
    """Resolved logs and per-provider artifact directory for a live run."""

    provider_reports: Path
    stdout: Path
    stderr: Path

    @classmethod
    def create(cls, run_dir: Path) -> _LivePaths:
        return cls(
            provider_reports=run_dir / "providers",
            stdout=run_dir / "stdout.log",
            stderr=run_dir / "stderr.log",
        )

    def prepare(self) -> None:
        self.provider_reports.mkdir(parents=True, exist_ok=True)

    def provider_report(self, spec: ProviderSurfaceSpec) -> Path:
        return self.provider_reports / f"{spec.artifact_key}.json"

    def write_logs(
        self,
        stdout: Sequence[str],
        stderr: Sequence[str],
        secrets: Sequence[str],
    ) -> None:
        self.stdout.write_text(redact_runtime_secrets("\n".join(stdout), secrets))
        self.stderr.write_text(redact_runtime_secrets("\n".join(stderr), secrets))


@dataclass(frozen=True, slots=True)
class _ProviderOutcome:
    """All report projections produced by checking one provider surface."""

    spec: ProviderSurfaceSpec
    check: ValidationCheck
    provider_check: ProviderCheck
    report: dict[str, object]
    failure: ValidationFailure | None = None
    skip: ValidationSkip | None = None
    command_result: CommandResult | None = None


@dataclass(slots=True)
class _LiveAccumulator:
    """Mutable collection boundary for independently evaluated provider outcomes."""

    artifacts: dict[str, ArtifactRef]
    checks: list[ValidationCheck] = field(default_factory=list)
    skips: list[ValidationSkip] = field(default_factory=list)
    failures: list[ValidationFailure] = field(default_factory=list)
    provider_checks: list[ProviderCheck] = field(default_factory=list)
    provider_reports: list[dict[str, object]] = field(default_factory=list)
    tool_exit_codes: dict[str, int] = field(default_factory=dict)
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, ctx: LaneRunContext, paths: _LivePaths) -> _LiveAccumulator:
        artifacts = ctx.artifacts_with(
            {
                "stdout": ArtifactRef(kind="stdout", path=str(paths.stdout)),
                "stderr": ArtifactRef(kind="stderr", path=str(paths.stderr)),
            }
        )
        return cls(artifacts=artifacts)

    def add_selector_failure(self, failure: ValidationFailure) -> None:
        self.failures.append(failure)
        self.checks.append(
            ValidationCheck(
                name=failure.name,
                status="fail",
                duration_s=0.0,
                details=failure.details,
            )
        )

    def add_provider(self, outcome: _ProviderOutcome, paths: _LivePaths) -> None:
        self.checks.append(outcome.check)
        self.provider_checks.append(outcome.provider_check)
        self.provider_reports.append(outcome.report)
        if outcome.failure is not None:
            self.failures.append(outcome.failure)
        if outcome.skip is not None:
            self.skips.append(outcome.skip)
        if outcome.command_result is not None:
            result = outcome.command_result
            self.stdout.append(result.stdout)
            self.stderr.append(result.stderr)
            self.tool_exit_codes[f"pytest.{outcome.spec.provider}.{outcome.spec.surface}"] = (
                result.exit_code
            )
        report_path = paths.provider_report(outcome.spec)
        _write_atomic(
            report_path,
            json.dumps(outcome.report, indent=2, sort_keys=True) + "\n",
        )
        self.artifacts[outcome.spec.artifact_key] = ArtifactRef(
            kind="provider_capability_report",
            path=str(report_path),
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
    """Validate selected live provider surfaces and persist capability reports."""
    request = _LiveRequest.create(
        providers=providers,
        surfaces=surfaces,
        strict=strict,
        release=release,
    )
    started_at = started_at or datetime.now(UTC)
    artifacts_root = Path(artifacts_dir)
    ctx = _start_lane_run(
        "live",
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    paths = _LivePaths.create(ctx.run_dir)
    paths.prepare()
    secrets = tuple(runtime_secret_values())
    outcomes = _LiveAccumulator.create(ctx, paths)
    started_monotonic = time.perf_counter()
    for failure in _selector_errors(request):
        outcomes.add_selector_failure(failure)
    for spec in select_provider_surfaces(
        providers=request.providers,
        surfaces=request.surfaces,
    ):
        outcome = _check_provider(
            spec,
            request=request,
            command_runner=command_runner or run_subprocess,
            secrets=secrets,
        )
        outcomes.add_provider(outcome, paths)
    duration_s = time.perf_counter() - started_monotonic
    finished_at = datetime.now(UTC)
    paths.write_logs(outcomes.stdout, outcomes.stderr, secrets)
    exit_code = 1 if outcomes.failures else 0
    status: ValidationStatus = "fail" if outcomes.failures else "pass"
    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
        command=request.command(),
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        status=status,
        exit_code=exit_code,
        tool_exit_codes=outcomes.tool_exit_codes,
        checks=outcomes.checks,
        skips=outcomes.skips,
        failures=outcomes.failures,
        providers=outcomes.provider_checks,
        provider_reports=outcomes.provider_reports,
        artifacts=outcomes.artifacts,
    )


def classify_live_failure(message: str) -> str:
    """Classify provider failures into stable, low-cardinality report values."""
    return _LIVE_FAILURE_CLASSES[classify_failure_category(message)]


def _check_provider(
    spec: ProviderSurfaceSpec,
    *,
    request: _LiveRequest,
    command_runner: CommandRunner,
    secrets: Sequence[str],
) -> _ProviderOutcome:
    credential_present = bool(spec.credential_env_var and os.environ.get(spec.credential_env_var))
    missing_credential = bool(spec.credential_env_var and not credential_present)
    started = time.perf_counter()
    if request.requires_credential(missing=missing_credential):
        return _missing_credential_failure(spec, duration_s=time.perf_counter() - started)
    if missing_credential:
        return _missing_credential_skip(spec, duration_s=time.perf_counter() - started)
    return _execute_provider(
        spec,
        credential_present=credential_present,
        command_runner=command_runner,
        secrets=secrets,
        started=started,
    )


def _missing_credential_failure(
    spec: ProviderSurfaceSpec,
    *,
    duration_s: float,
) -> _ProviderOutcome:
    name = _provider_check_name(spec)
    failure = ValidationFailure(
        name=name,
        message=f"{spec.credential_env_var} is required for {spec.provider} {spec.surface}",
        failure_class="auth_or_quota",
    )
    return _ProviderOutcome(
        spec=spec,
        check=ValidationCheck(
            name=name,
            status="fail",
            duration_s=duration_s,
            details={"credential_env_var": spec.credential_env_var},
        ),
        provider_check=ProviderCheck(
            provider=spec.provider,
            surface=spec.surface,
            state=ProviderCheckState.FAILED_MISSING_REQUIRED_SECRET,
            credential_env=spec.credential_env_var,
            required=True,
            failure_class="auth_or_quota",
        ),
        report=_provider_report(
            spec,
            credential_present=False,
            state=ProviderCheckState.FAILED_MISSING_REQUIRED_SECRET,
            failure_class="auth_or_quota",
        ),
        failure=failure,
    )


def _missing_credential_skip(
    spec: ProviderSurfaceSpec,
    *,
    duration_s: float,
) -> _ProviderOutcome:
    name = _provider_check_name(spec)
    skip = ValidationSkip(
        name=name,
        reason=f"{spec.credential_env_var} missing",
        expected=True,
    )
    return _ProviderOutcome(
        spec=spec,
        check=ValidationCheck(
            name=name,
            status="skip",
            duration_s=duration_s,
            details={"credential_env_var": spec.credential_env_var},
        ),
        provider_check=ProviderCheck(
            provider=spec.provider,
            surface=spec.surface,
            state=ProviderCheckState.SKIPPED_MISSING_SECRET,
            credential_env=spec.credential_env_var,
            required=False,
        ),
        report=_provider_report(
            spec,
            credential_present=False,
            state="expected_skip",
        ),
        skip=skip,
    )


def _execute_provider(
    spec: ProviderSurfaceSpec,
    *,
    credential_present: bool,
    command_runner: CommandRunner,
    secrets: Sequence[str],
    started: float,
) -> _ProviderOutcome:
    command = _live_pytest_command(spec)
    result = command_runner(command, env={**os.environ})
    duration_s = time.perf_counter() - started
    if result.exit_code == 0:
        return _executed_provider_outcome(
            spec,
            command=command,
            result=result,
            duration_s=duration_s,
            credential_present=credential_present,
            state=ProviderCheckState.PASSED,
        )
    failure_message = redact_runtime_secrets(
        result.stderr or result.stdout or f"pytest exited {result.exit_code}",
        secrets,
    )
    failure_class = classify_live_failure(failure_message)
    return _executed_provider_outcome(
        spec,
        command=command,
        result=result,
        duration_s=duration_s,
        credential_present=credential_present,
        state=ProviderCheckState.FAILED,
        failure=ValidationFailure(
            name=_provider_check_name(spec),
            message=failure_message,
            failure_class=failure_class,
        ),
        failure_class=failure_class,
    )


def _executed_provider_outcome(
    spec: ProviderSurfaceSpec,
    *,
    command: Sequence[str],
    result: CommandResult,
    duration_s: float,
    credential_present: bool,
    state: ProviderCheckState,
    failure: ValidationFailure | None = None,
    failure_class: str | None = None,
) -> _ProviderOutcome:
    name = _provider_check_name(spec)
    status: ValidationStatus = "pass" if state is ProviderCheckState.PASSED else "fail"
    return _ProviderOutcome(
        spec=spec,
        check=ValidationCheck(
            name=name,
            status=status,
            duration_s=duration_s,
            command=command,
        ),
        provider_check=ProviderCheck(
            provider=spec.provider,
            surface=spec.surface,
            state=state,
            credential_env=spec.credential_env_var or None,
            required=bool(spec.credential_env_var),
            failure_class=failure_class,
        ),
        report=_provider_report(
            spec,
            credential_present=credential_present,
            state=state,
            failure_class=failure_class,
        ),
        failure=failure,
        command_result=result,
    )


def _provider_report(
    spec: ProviderSurfaceSpec,
    *,
    credential_present: bool,
    state: ProviderCheckState | str,
    failure_class: str | None = None,
) -> dict[str, object]:
    live_status = state.value if isinstance(state, ProviderCheckState) else state
    return build_provider_capability_report(
        spec,
        live_checked_at=datetime.now(UTC),
        credential_present=credential_present,
        live_status=live_status,
        failure_class=failure_class,
    ).to_dict()


def _selector_errors(request: _LiveRequest) -> tuple[ValidationFailure, ...]:
    failures: list[ValidationFailure] = []
    known_providers = known_live_providers()
    for provider in {
        provider.strip().lower() for provider in request.providers if provider.strip()
    }:
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
    for surface in {surface.strip().lower() for surface in request.surfaces if surface.strip()}:
        if surface not in known_surfaces:
            failures.append(
                ValidationFailure(
                    name="provider.selector",
                    message=f"unknown live surface selector: {surface}",
                    failure_class="environment",
                    details={"surface": surface, "known_surfaces": sorted(known_surfaces)},
                )
            )
    return tuple(failures)


def _provider_check_name(spec: ProviderSurfaceSpec) -> str:
    return f"provider.{spec.provider}.{spec.surface}"


def _live_pytest_command(spec: ProviderSurfaceSpec) -> list[str]:
    command = [*pytest_command_prefix(), "-q"]
    if spec.live_pytest_target:
        command.append(resolve_validation_test_arg(spec.live_pytest_target))
    command.extend(["-m", _live_marker_expression(spec)])
    return command


def _live_marker_expression(spec: ProviderSurfaceSpec) -> str:
    markers = ["integration_live"]
    provider_marker = _provider_marker(spec.provider)
    if provider_marker is not None:
        markers.append(provider_marker)
    markers.append(f"surface_{spec.surface.removesuffix('_bridge')}")
    markers.append("not flaky")
    return " and ".join(markers)


def _provider_marker(provider: str) -> str | None:
    # OpenAI variants share one provider marker; the catalog remains the
    # authoritative list for provider-specific marker names.
    normalized = "openai" if provider.startswith("openai") else provider
    if normalized in provider_names():
        return f"provider_{normalized}"
    return None
