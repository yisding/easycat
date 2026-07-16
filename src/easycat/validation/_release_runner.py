"""Installed-artifact release validation orchestration."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easycat.validation._environment import runtime_secret_values
from easycat.validation._lane_harness import (
    LaneRunContext,
    ValidationRunResult,
    _finish_lane_run,
    _start_lane_run,
)
from easycat.validation._latency_models import LatencyMode
from easycat.validation._latency_runner import run_latency_validation
from easycat.validation._live_runner import run_live_validation
from easycat.validation._runner_support import CommandResult, CommandRunner, run_subprocess
from easycat.validation._slice_runner import run_validation_slice
from easycat.validation.report import (
    ArtifactRef,
    ValidationCheck,
    ValidationFailure,
    ValidationStatus,
    redact_runtime_secrets,
)

DEFAULT_RELEASE_EXTRAS = ("openai", "openai-agents")
DEFAULT_RELEASE_PROVIDERS = ("openai",)
DEFAULT_RELEASE_SURFACES = ("stt", "tts")
RELEASE_SLICES = ("quick", "stress", "contracts")
# pytest-xdist is required because the quick slice runs with -n auto.
RELEASE_TEST_DEPENDENCIES = ("pytest", "pytest-asyncio", "pytest-xdist", "hypothesis")

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


@dataclass(frozen=True, slots=True)
class _ReleaseRequest:
    """Normalized release selectors and installed-runtime configuration."""

    python_version: str
    extras: tuple[str, ...]
    providers: tuple[str, ...]
    surfaces: tuple[str, ...]
    latency_mode: LatencyMode

    @classmethod
    def create(
        cls,
        *,
        python_version: str | None,
        extras: Sequence[str] | None,
        providers: Sequence[str] | None,
        surfaces: Sequence[str] | None,
        latency_mode: LatencyMode | str,
    ) -> _ReleaseRequest:
        return cls(
            python_version=python_version or f"{sys.version_info.major}.{sys.version_info.minor}",
            extras=tuple(extras) if extras is not None else DEFAULT_RELEASE_EXTRAS,
            providers=(tuple(providers) if providers is not None else DEFAULT_RELEASE_PROVIDERS),
            surfaces=tuple(surfaces) if surfaces is not None else DEFAULT_RELEASE_SURFACES,
            latency_mode=LatencyMode(latency_mode),
        )

    def command(self) -> list[str]:
        command = ["easycat", "validate", "release", "--python", self.python_version]
        for extra in self.extras:
            command.extend(["--extra", extra])
        for provider in self.providers:
            command.extend(["--provider", provider])
        for surface in self.surfaces:
            command.extend(["--surface", surface])
        command.append(
            "--latency-smoke" if self.latency_mode is LatencyMode.SMOKE else "--latency-sweep"
        )
        return command


@dataclass(frozen=True, slots=True)
class _ReleasePaths:
    """Persistent artifact paths for one release validation run."""

    source_root: Path
    stdout: Path
    stderr: Path
    dist: Path

    @classmethod
    def create(cls, ctx: LaneRunContext) -> _ReleasePaths:
        return cls(
            source_root=Path.cwd().resolve(),
            stdout=ctx.run_dir / "stdout.log",
            stderr=ctx.run_dir / "stderr.log",
            dist=ctx.run_dir / "dist",
        )

    def prepare(self) -> None:
        self.dist.mkdir(parents=True, exist_ok=True)

    def artifacts(self, ctx: LaneRunContext) -> dict[str, ArtifactRef]:
        artifacts = {
            **ctx.artifacts,
            "stdout": ArtifactRef(kind="stdout", path=str(self.stdout)),
            "stderr": ArtifactRef(kind="stderr", path=str(self.stderr)),
            "dist": ArtifactRef(kind="directory", path=str(self.dist)),
        }
        if ctx.requested_report_path is not None:
            artifacts["requested_report"] = ArtifactRef(
                kind="validation_report",
                path=str(ctx.requested_report_path),
            )
        return artifacts


@dataclass(frozen=True, slots=True)
class _ReleaseWorkspace:
    """Disposable venv and out-of-tree execution workspace."""

    venv: Path
    outside_source: Path

    @classmethod
    def create(cls, root: Path) -> _ReleaseWorkspace:
        workspace = cls(
            venv=root / "venv",
            outside_source=root / "outside-source",
        )
        workspace.outside_source.mkdir(parents=True, exist_ok=True)
        return workspace

    def executable(self, name: str) -> Path:
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        suffix = ".exe" if os.name == "nt" and not name.endswith(".exe") else ""
        return self.venv / bin_dir / f"{name}{suffix}"

    def outside_environment(self, source_root: Path) -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONPATH": "",
            "EASYCAT_RELEASE_SOURCE_ROOT": str(source_root),
        }

    def validation_overrides(self, source_root: Path) -> dict[str, str]:
        python = self.executable("python")
        return {
            "PYTHONPATH": "",
            "EASYCAT_VALIDATION_PYTEST_COMMAND": shlex.join([str(python), "-m", "pytest"]),
            "EASYCAT_VALIDATION_TEST_PATHS": str(source_root / "tests"),
            "EASYCAT_VALIDATION_TEST_ROOT": str(source_root / "tests"),
        }


@dataclass(slots=True)
class _ReleaseRecorder:
    """Accumulate release commands, child reports, failures, and redacted logs."""

    command_runner: CommandRunner
    secrets: Sequence[str]
    artifacts: dict[str, ArtifactRef]
    checks: list[ValidationCheck] = field(default_factory=list)
    failures: list[ValidationFailure] = field(default_factory=list)
    tool_exit_codes: dict[str, int] = field(default_factory=dict)
    stdout_log: list[str] = field(default_factory=list)
    stderr_log: list[str] = field(default_factory=list)

    def record_command(
        self,
        name: str,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> bool:
        started = time.perf_counter()
        result = self.command_runner(command, env=env, cwd=cwd)
        duration_s = time.perf_counter() - started
        self.tool_exit_codes[name] = result.exit_code
        status: ValidationStatus = "pass" if result.exit_code == 0 else "fail"
        details = {"cwd": str(cwd)} if cwd is not None else {}
        self.checks.append(
            ValidationCheck(
                name=name,
                status=status,
                duration_s=duration_s,
                command=command,
                details=details,
            )
        )
        if result.stdout:
            self.stdout_log.append(f"{name}\n{result.stdout}")
        if result.stderr:
            self.stderr_log.append(f"{name}\n{result.stderr}")
        if result.exit_code != 0:
            self.failures.append(
                ValidationFailure(
                    name=name,
                    message=redact_runtime_secrets(
                        result.stderr or result.stdout or f"{name} exited {result.exit_code}",
                        self.secrets,
                    ),
                    failure_class="release_validation",
                    details=details,
                )
            )
        return result.exit_code == 0

    def record_failure(
        self,
        name: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        failure_details = dict(details or {})
        self.tool_exit_codes[name] = 1
        self.checks.append(
            ValidationCheck(
                name=name,
                status="fail",
                duration_s=0.0,
                details=failure_details,
            )
        )
        self.failures.append(
            ValidationFailure(
                name=name,
                message=message,
                failure_class="release_validation",
                details=failure_details,
            )
        )

    def record_child(self, name: str, result: ValidationRunResult) -> None:
        artifact_key = name.removeprefix("release.").replace(".", "_") + "_report"
        report_ref = ArtifactRef(kind="validation_report", path=str(result.report_path))
        self.artifacts[artifact_key] = report_ref
        self.tool_exit_codes[name] = result.exit_code
        self.checks.append(
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
            self.failures.append(
                ValidationFailure(
                    name=name,
                    message=f"{name} failed; see {result.report_path}",
                    failure_class="release_validation",
                    details={"report_path": str(result.report_path)},
                )
            )

    def write_logs(self, paths: _ReleasePaths) -> None:
        paths.stdout.write_text(redact_runtime_secrets("\n".join(self.stdout_log), self.secrets))
        paths.stderr.write_text(redact_runtime_secrets("\n".join(self.stderr_log), self.secrets))


@dataclass(slots=True)
class _ReleaseOrchestrator:
    """Execute the release gate as ordered build, install, smoke, and child phases."""

    request: _ReleaseRequest
    paths: _ReleasePaths
    workspace: _ReleaseWorkspace
    recorder: _ReleaseRecorder

    def run(self, artifacts_root: Path) -> None:
        wheel = self._build_distributions()
        if wheel is None or not self._install_wheel(wheel):
            return
        if not self._run_installed_smokes():
            return
        self._run_child_lanes(artifacts_root)

    def _build_distributions(self) -> Path | None:
        build_ok = self.recorder.record_command(
            "release.build",
            [
                "uv",
                "build",
                "--sdist",
                "--wheel",
                "--no-sources",
                "-o",
                str(self.paths.dist),
            ],
            env={**os.environ},
            cwd=self.paths.source_root,
        )
        if not build_ok:
            return None

        wheel = _single_distribution(self.paths.dist, "*.whl")
        if wheel is None:
            self.recorder.record_failure(
                "release.wheel",
                f"uv build did not create exactly one wheel under {self.paths.dist}",
                details={"dist_dir": str(self.paths.dist)},
            )
        sdist = _single_distribution(self.paths.dist, "*.tar.gz")
        if sdist is None:
            self.recorder.record_failure(
                "release.sdist",
                f"uv build did not create exactly one sdist under {self.paths.dist}",
                details={"dist_dir": str(self.paths.dist)},
            )

        distributions = _release_dist_paths(self.paths.dist)
        metadata_ok = False
        if distributions:
            metadata_ok = self.recorder.record_command(
                "release.metadata",
                ["uvx", "twine", "check", *[str(path) for path in distributions]],
                env={**os.environ},
                cwd=self.paths.source_root,
            )
        else:
            self.recorder.record_failure(
                "release.metadata",
                f"uv build did not create distributions under {self.paths.dist}",
                details={"dist_dir": str(self.paths.dist)},
            )
        return wheel if wheel is not None and sdist is not None and metadata_ok else None

    def _install_wheel(self, wheel: Path) -> bool:
        if not self.recorder.record_command(
            "release.venv",
            [
                "uv",
                "venv",
                str(self.workspace.venv),
                "--python",
                self.request.python_version,
            ],
            env={**os.environ},
            cwd=self.paths.source_root,
        ):
            return False

        python = self.workspace.executable("python")
        if not self.recorder.record_command(
            "release.install",
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                _release_package_spec(wheel, self.request.extras),
            ],
            env={**os.environ},
            cwd=self.paths.source_root,
        ):
            return False

        return self.recorder.record_command(
            "release.install-test-tools",
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                *RELEASE_TEST_DEPENDENCIES,
            ],
            env={**os.environ},
            cwd=self.paths.source_root,
        )

    def _run_installed_smokes(self) -> bool:
        python = self.workspace.executable("python")
        easycat = self.workspace.executable("easycat")
        outside = self.workspace.outside_source
        env = self.workspace.outside_environment(self.paths.source_root)
        results = (
            self.recorder.record_command(
                "release.import-smoke",
                [str(python), "-c", _RELEASE_IMPORT_SMOKE],
                env=env,
                cwd=outside,
            ),
            self.recorder.record_command(
                "release.public-api-smoke",
                [str(python), "-c", _RELEASE_PUBLIC_API_SMOKE],
                env=env,
                cwd=outside,
            ),
            self.recorder.record_command(
                "release.help-smoke",
                [str(easycat), "--help"],
                env=env,
                cwd=outside,
            ),
            self.recorder.record_command(
                "release.module-smoke",
                [str(python), "-m", "easycat"],
                env=env,
                cwd=outside,
            ),
            self.recorder.record_command(
                "release.init-smoke",
                [
                    str(easycat),
                    "init",
                    str(outside / "easycat-scaffold-smoke"),
                    "--template",
                    "text-chat",
                    "--no-git",
                    "--json",
                ],
                env=env,
                cwd=outside,
            ),
            self.recorder.record_command(
                "release.doctor",
                [str(easycat), "doctor", "--json"],
                env=env,
                cwd=outside,
            ),
            self.recorder.record_command(
                "release.cli-smoke",
                [
                    str(python),
                    "-m",
                    "pytest",
                    str(self.paths.source_root / "tests/cli/test_app.py"),
                    "-q",
                ],
                env=env,
                cwd=outside,
            ),
        )
        return all(results)

    def _run_child_lanes(self, artifacts_root: Path) -> None:
        overrides = self.workspace.validation_overrides(self.paths.source_root)

        def child_command_runner(
            command: list[str],
            *,
            env: Mapping[str, str] | None = None,
            cwd: str | Path | None = None,
        ) -> CommandResult:
            child_env = {**(env or os.environ), **overrides}
            return self.recorder.command_runner(
                command,
                env=child_env,
                cwd=cwd or self.workspace.outside_source,
            )

        with _temporary_environ(overrides):
            for slice_name in RELEASE_SLICES:
                child = run_validation_slice(
                    slice_name,
                    artifacts_dir=artifacts_root,
                    junit_prefix=f"release-{slice_name}",
                    command_runner=child_command_runner,
                )
                self.recorder.record_child(f"release.{slice_name}", child)

            live_child = run_live_validation(
                providers=self.request.providers,
                surfaces=self.request.surfaces,
                release=True,
                artifacts_dir=artifacts_root,
                command_runner=child_command_runner,
            )
            self.recorder.record_child("release.live", live_child)

            latency_child = run_latency_validation(
                self.request.latency_mode,
                artifacts_dir=artifacts_root,
                require_samples=True,
                command_runner=child_command_runner,
            )
            self.recorder.record_child(
                f"release.latency.{self.request.latency_mode.value}",
                latency_child,
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
    """Run the strict release gate against cleanly installed distributions."""
    started_at = started_at or datetime.now(UTC)
    started_monotonic = time.perf_counter()
    artifacts_root = Path(artifacts_dir)
    ctx = _start_lane_run(
        "release",
        started_at=started_at,
        artifacts_root=artifacts_root,
        report_path=report_path,
    )
    request = _ReleaseRequest.create(
        python_version=python_version,
        extras=extras,
        providers=providers,
        surfaces=surfaces,
        latency_mode=latency_mode,
    )
    paths = _ReleasePaths.create(ctx)
    paths.prepare()
    recorder = _ReleaseRecorder(
        command_runner=command_runner or run_subprocess,
        secrets=runtime_secret_values(),
        artifacts=paths.artifacts(ctx),
    )

    with tempfile.TemporaryDirectory(prefix=f"easycat-release-{ctx.run_id}-") as root:
        workspace = _ReleaseWorkspace.create(Path(root))
        _ReleaseOrchestrator(
            request=request,
            paths=paths,
            workspace=workspace,
            recorder=recorder,
        ).run(artifacts_root)

    finished_at = datetime.now(UTC)
    recorder.write_logs(paths)
    exit_code = 1 if recorder.failures else 0
    status: ValidationStatus = "fail" if recorder.failures else "pass"
    return _finish_lane_run(
        ctx,
        artifacts_root=artifacts_root,
        command=request.command(),
        started_at=started_at,
        finished_at=finished_at,
        duration_s=time.perf_counter() - started_monotonic,
        status=status,
        exit_code=exit_code,
        tool_exit_codes=recorder.tool_exit_codes,
        checks=recorder.checks,
        failures=recorder.failures,
        artifacts=recorder.artifacts,
    )


def _single_distribution(dist_dir: Path, pattern: str) -> Path | None:
    matches = sorted(dist_dir.glob(pattern))
    return matches[0] if len(matches) == 1 else None


def _release_dist_paths(dist_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in dist_dir.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )


def _release_package_spec(wheel_path: Path, extras: Sequence[str]) -> str:
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"easycat{suffix} @ file://{wheel_path.resolve()}"


@contextmanager
def _temporary_environ(overrides: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for name, previous_value in previous.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value
