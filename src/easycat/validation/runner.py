from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easycat.validation import _environment, _latency_runner, _live_runner, _runner_support
from easycat.validation._lane_harness import (
    ValidationRunResult,
    _finish_lane_run,
    _start_lane_run,
)
from easycat.validation._runner_support import CommandResult, CommandRunner
from easycat.validation._slice_runner import VALIDATION_SELECTORS, run_validation_slice
from easycat.validation.latency import LatencyMode
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
LATENCY_SYNTHETIC_FAILURE_SAMPLE = _latency_runner.LATENCY_SYNTHETIC_FAILURE_SAMPLE
LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY = _latency_runner.LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY
run_latency_validation = _latency_runner.run_latency_validation
classify_live_failure = _live_runner.classify_live_failure
run_live_validation = _live_runner.run_live_validation
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
    command_runner = command_runner or _runner_support.run_subprocess
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
    runtime_secret_values = _environment.runtime_secret_values()

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
        status: ValidationStatus = "pass" if result.exit_code == 0 else "fail"
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

    def child_command_runner(
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> CommandResult:
        child_env = {
            **(env or os.environ),
            "PYTHONPATH": "",
            "EASYCAT_VALIDATION_PYTEST_COMMAND": shlex.join([str(venv_python), "-m", "pytest"]),
            "EASYCAT_VALIDATION_TEST_PATHS": str(source_root / "tests"),
            "EASYCAT_VALIDATION_TEST_ROOT": str(source_root / "tests"),
        }
        return command_runner(command, env=child_env, cwd=cwd or outside_dir)

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
    status: ValidationStatus = "fail" if failures else "pass"

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
        for name, previous_value in previous.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value
