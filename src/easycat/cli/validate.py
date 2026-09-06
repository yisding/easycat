from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import IO, Annotated, Literal, NoReturn, TypeVar, cast

import typer
from rich.markup import escape

from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stderr_console,
    stdout_console,
)
from easycat.validation._runner_support import (
    ValidationSourceCheckoutError,
    ensure_validation_source_checkout,
)
from easycat.validation.latency import LatencyMode
from easycat.validation.report import redact_runtime_secrets
from easycat.validation.runner import (
    ValidationRunResult,
    run_latency_validation,
    run_live_validation,
    run_release_validation,
    run_validation_slice,
)

validate_app = typer.Typer(
    name="validate",
    help="Run validation checks and inspect validation reports.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_ARTIFACTS_DIR_HELP = (
    "Validation artifact root directory; writes runs/<id>/report.json and latest.json."
)
_SHOW_OUTPUT_HELP = "Also print captured validation stdout/stderr while keeping artifacts."
_MAX_STREAMED_LOG_BYTES = 10 * 1024 * 1024
_ValidationResultT = TypeVar("_ValidationResultT")


def _print_literal(line: str) -> None:
    stdout_console.print(escape(line))


def _run_from_source_checkout(
    command: str,
    *,
    json_output: bool,
    test_override_mode: Literal["paths", "root", "both"],
    operation: Callable[[], _ValidationResultT],
) -> _ValidationResultT:
    try:
        ensure_validation_source_checkout(test_override_mode=test_override_mode)
        return operation()
    except ValidationSourceCheckoutError as exc:
        emit_command_error(
            command,
            str(exc),
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2) from None


def _run_slice(
    slice_name: str,
    *,
    json_output: bool,
    report: Path | None,
    junit: Path | None,
    artifacts_dir: Path,
    junit_prefix: str | None,
    show_output: bool,
) -> None:
    result = _run_from_source_checkout(
        f"validate {slice_name}",
        json_output=json_output,
        test_override_mode="paths",
        operation=lambda: run_validation_slice(
            slice_name,
            artifacts_dir=artifacts_dir,
            report_path=report,
            junit_path=junit,
            junit_prefix=junit_prefix,
        ),
    )

    if show_output:
        _stream_validation_output(result, json_output=json_output)

    if json_output:
        status = "ok" if result.exit_code == 0 else "error"
        emit_json(
            json_envelope(
                f"validate {slice_name}",
                status=status,
                exit_code=result.exit_code,
                report_path=str(report or result.report_path),
                validation=result.run.to_dict(),
            )
        )
    else:
        _print_literal(
            f"{slice_name}: {result.run.status}; report: {report or result.report_path}"
        )

    raise typer.Exit(result.exit_code)


def _stream_validation_output(
    result: ValidationRunResult,
    *,
    json_output: bool,
    include_child_reports: bool = False,
) -> None:
    streamed_paths: set[Path] = set()
    stdout_log = _artifact_path(result, "stdout")
    stderr_log = _artifact_path(result, "stderr")
    _stream_artifact_logs(
        stdout_log,
        stderr_log,
        json_output=json_output,
        streamed_paths=streamed_paths,
        allowed_dir=result.run_dir,
    )

    if not include_child_reports:
        return

    for report_path in _child_report_paths(result):
        payload = _read_validation_report(report_path)
        if payload is None:
            continue
        _stream_artifact_logs(
            _payload_artifact_path(payload, "stdout", allowed_dir=report_path.parent),
            _payload_artifact_path(payload, "stderr", allowed_dir=report_path.parent),
            json_output=json_output,
            streamed_paths=streamed_paths,
            allowed_dir=report_path.parent,
        )


def _stream_artifact_logs(
    stdout_log: Path | None,
    stderr_log: Path | None,
    *,
    json_output: bool,
    streamed_paths: set[Path],
    allowed_dir: Path,
) -> None:
    if stdout_log is not None and _streamable_log_path(stdout_log, allowed_dir=allowed_dir):
        target = stderr_console.file if json_output else stdout_console.file
        _write_log_once(target, stdout_log, streamed_paths=streamed_paths)
    if stderr_log is not None and _streamable_log_path(stderr_log, allowed_dir=allowed_dir):
        _write_log_once(stderr_console.file, stderr_log, streamed_paths=streamed_paths)


def _artifact_path(result: ValidationRunResult, name: str) -> Path | None:
    artifact = result.run.artifacts.get(name)
    if artifact is None:
        for check in result.run.checks:
            artifact = check.artifacts.get(name)
            if artifact is not None:
                break
    if artifact is None:
        return None
    return Path(artifact.path)


def _child_report_paths(result: ValidationRunResult) -> list[Path]:
    parent_reports = {
        _path_key(path)
        for path in (
            result.report_path,
            _artifact_path(result, "report"),
            _artifact_path(result, "requested_report"),
        )
        if path is not None
    }
    paths: list[Path] = []
    seen: set[Path] = set(parent_reports)

    def append_report(path: Path) -> None:
        key = _path_key(path)
        if key in seen:
            return
        seen.add(key)
        paths.append(path)

    for check in result.run.checks:
        for artifact in check.artifacts.values():
            if artifact.kind == "validation_report":
                append_report(Path(artifact.path))

    for name, artifact in result.run.artifacts.items():
        if name in {"report", "requested_report"}:
            continue
        if artifact.kind == "validation_report":
            append_report(Path(artifact.path))

    return paths


def _read_validation_report(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        payload = json.loads(raw)
    except (RecursionError, ValueError):
        return None

    return payload if isinstance(payload, dict) else None


def _payload_artifact_path(
    payload: dict[str, object],
    name: str,
    *,
    allowed_dir: Path,
) -> Path | None:
    path = _named_payload_artifact_path(payload.get("artifacts"), name, allowed_dir=allowed_dir)
    if path is not None:
        return path

    checks = payload.get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if not isinstance(check, dict):
            continue
        path = _named_payload_artifact_path(check.get("artifacts"), name, allowed_dir=allowed_dir)
        if path is not None:
            return path
    return None


def _named_payload_artifact_path(
    artifacts: object,
    name: str,
    *,
    allowed_dir: Path,
) -> Path | None:
    if not isinstance(artifacts, dict):
        return None
    artifact = artifacts.get(name)
    if not isinstance(artifact, dict) or artifact.get("kind") != name:
        return None
    path = artifact.get("path")
    if not isinstance(path, str) or not path:
        return None

    candidate = Path(path)
    return candidate if _streamable_log_path(candidate, allowed_dir=allowed_dir) else None


def _streamable_log_path(path: Path, *, allowed_dir: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_dir = allowed_dir.resolve(strict=True)
        path_stat = path.lstat()
    except OSError:
        return False

    if path_stat.st_size > _MAX_STREAMED_LOG_BYTES:
        return False
    if path.is_symlink() or not path.is_file():
        return False
    return resolved_path.is_relative_to(resolved_dir)


def _read_captured_log(path: Path) -> str:
    """Read a captured child-process log, tolerating bytes it did not choose.

    Slices capture stdout/stderr byte-for-byte, so a tool emitting cp1252 or
    latin-1 text — or plain binary noise — leaves a log that is not valid
    UTF-8. A strict read raised ``UnicodeDecodeError`` *after* the validation
    run had already finished, turning a completed validation into a traceback,
    so decode lossily instead and keep the streaming step non-fatal (gh 1108).
    Mirrors ``_load_report_payload``'s ``(OSError, UnicodeError)`` handling.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[could not read {path}: {exc}]\n"


def _write_log_once(target: IO[str], path: Path, *, streamed_paths: set[Path]) -> None:
    key = _path_key(path)
    if key in streamed_paths:
        return
    streamed_paths.add(key)
    _write_log(target, redact_runtime_secrets(_read_captured_log(path)))


def _path_key(path: Path) -> Path:
    return path.resolve(strict=False)


def _write_log(target: IO[str], text: str) -> None:
    if not text:
        return
    target.write(text)
    if not text.endswith("\n"):
        target.write("\n")
    target.flush()


@validate_app.command()
def quick(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    junit: Annotated[
        Path | None,
        typer.Option("--junit", help="Optional JUnit XML output path."),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
    show_output: Annotated[
        bool,
        typer.Option(
            "--show-output",
            help=_SHOW_OUTPUT_HELP,
        ),
    ] = False,
) -> None:
    """Run deterministic local validation for normal PR work."""
    _run_slice(
        "quick",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
        show_output=show_output,
    )


@validate_app.command()
def socket(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    junit: Annotated[
        Path | None,
        typer.Option("--junit", help="Optional JUnit XML output path."),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
    show_output: Annotated[
        bool,
        typer.Option(
            "--show-output",
            help=_SHOW_OUTPUT_HELP,
        ),
    ] = False,
) -> None:
    """Run localhost socket integration validation."""
    _run_slice(
        "socket",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
        show_output=show_output,
    )


@validate_app.command()
def stress(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    junit: Annotated[
        Path | None,
        typer.Option("--junit", help="Optional JUnit XML output path."),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
    show_output: Annotated[
        bool,
        typer.Option(
            "--show-output",
            help=_SHOW_OUTPUT_HELP,
        ),
    ] = False,
) -> None:
    """Run local stress validation and saturation-signal capture."""
    _run_slice(
        "stress",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
        show_output=show_output,
    )


@validate_app.command()
def contracts(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    junit: Annotated[
        Path | None,
        typer.Option("--junit", help="Optional JUnit XML output path."),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
    show_output: Annotated[
        bool,
        typer.Option(
            "--show-output",
            help=_SHOW_OUTPUT_HELP,
        ),
    ] = False,
) -> None:
    """Run offline provider, protocol, and bridge contract validation."""
    _run_slice(
        "contracts",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
        show_output=show_output,
    )


@validate_app.command()
def latency(
    smoke: Annotated[
        bool,
        typer.Option("--smoke", help="Run the low-cost latency smoke probe."),
    ] = False,
    sweep: Annotated[
        bool,
        typer.Option("--sweep", help="Run the broader latency condition sweep."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    require_samples: Annotated[
        bool,
        typer.Option(
            "--require-samples",
            help="Fail when no latency samples are produced (default for --sweep).",
        ),
    ] = False,
    baseline: Annotated[
        Path | None,
        typer.Option(
            "--baseline",
            help="Stored latency artifact to compare against for regression/drift detection.",
        ),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    show_output: Annotated[
        bool,
        typer.Option("--show-output", help=_SHOW_OUTPUT_HELP),
    ] = False,
) -> None:
    """Run live latency validation and write structured latency artifacts."""
    if smoke and sweep:
        emit_command_error(
            "validate latency",
            "choose only one of --smoke or --sweep",
            json_output=json_output,
        )
        raise typer.Exit(2)

    mode = LatencyMode.SWEEP if sweep else LatencyMode.SMOKE
    # When the flag is not passed, defer to the runner's mode-aware default
    # (SWEEP requires samples, SMOKE does not) instead of forcing it off.
    result = _run_from_source_checkout(
        "validate latency",
        json_output=json_output,
        test_override_mode="root",
        operation=lambda: run_latency_validation(
            mode,
            artifacts_dir=artifacts_dir,
            report_path=report,
            require_samples=True if require_samples else None,
            baseline_path=baseline,
        ),
    )

    if show_output:
        _stream_validation_output(result, json_output=json_output)

    if json_output:
        status = "ok" if result.exit_code == 0 else "error"
        emit_json(
            json_envelope(
                "validate latency",
                status=status,
                exit_code=result.exit_code,
                mode=mode.value,
                report_path=str(report or result.report_path),
                validation=result.run.to_dict(),
            )
        )
    else:
        _print_literal(
            f"latency {mode.value}: {result.run.status}; report: {report or result.report_path}"
        )

    raise typer.Exit(result.exit_code)


@validate_app.command()
def live(
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Provider to validate; may be repeated."),
    ] = None,
    surface: Annotated[
        list[str] | None,
        typer.Option("--surface", help="Provider surface to validate; may be repeated."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Fail explicitly requested providers with missing secrets."),
    ] = False,
    release: Annotated[
        bool,
        typer.Option("--release", help="Fail missing required live prerequisites."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    show_output: Annotated[
        bool,
        typer.Option("--show-output", help=_SHOW_OUTPUT_HELP),
    ] = False,
) -> None:
    """Run live provider canaries and emit capability reports."""
    result = _run_from_source_checkout(
        "validate live",
        json_output=json_output,
        test_override_mode="root",
        operation=lambda: run_live_validation(
            providers=provider,
            surfaces=surface,
            strict=strict,
            release=release,
            artifacts_dir=artifacts_dir,
            report_path=report,
        ),
    )

    if show_output:
        _stream_validation_output(result, json_output=json_output)

    if json_output:
        status = "ok" if result.exit_code == 0 else "error"
        emit_json(
            json_envelope(
                "validate live",
                status=status,
                exit_code=result.exit_code,
                report_path=str(report or result.report_path),
                validation=result.run.to_dict(),
            )
        )
    else:
        _print_literal(f"live: {result.run.status}; report: {report or result.report_path}")

    raise typer.Exit(result.exit_code)


@validate_app.command()
def release(
    python_version: Annotated[
        str | None,
        typer.Option("--python", help="Python version for the clean release venv."),
    ] = None,
    extra: Annotated[
        list[str] | None,
        typer.Option("--extra", help="Package extra to install; may be repeated."),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Required live provider; may be repeated."),
    ] = None,
    surface: Annotated[
        list[str] | None,
        typer.Option("--surface", help="Required live provider surface; may be repeated."),
    ] = None,
    latency_smoke: Annotated[
        bool,
        typer.Option("--latency-smoke", help="Use latency smoke instead of the sweep gate."),
    ] = False,
    latency_sweep: Annotated[
        bool,
        typer.Option("--latency-sweep", help="Use the release latency sweep gate."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Optional additional validation report JSON path."),
    ] = None,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help=_ARTIFACTS_DIR_HELP),
    ] = Path(".easycat/validation"),
    show_output: Annotated[
        bool,
        typer.Option("--show-output", help=_SHOW_OUTPUT_HELP),
    ] = False,
) -> None:
    """Build, install, and run the strict release validation gate."""
    if latency_smoke and latency_sweep:
        emit_command_error(
            "validate release",
            "choose only one of --latency-smoke or --latency-sweep",
            json_output=json_output,
            human_console=stdout_console,
        )
        raise typer.Exit(2)

    mode = LatencyMode.SMOKE if latency_smoke else LatencyMode.SWEEP
    result = _run_from_source_checkout(
        "validate release",
        json_output=json_output,
        test_override_mode="both",
        operation=lambda: run_release_validation(
            artifacts_dir=artifacts_dir,
            report_path=report,
            python_version=python_version,
            extras=extra,
            providers=provider,
            surfaces=surface,
            latency_mode=mode,
        ),
    )

    if show_output:
        _stream_validation_output(
            result,
            json_output=json_output,
            include_child_reports=True,
        )

    if json_output:
        status = "ok" if result.exit_code == 0 else "error"
        emit_json(
            json_envelope(
                "validate release",
                status=status,
                exit_code=result.exit_code,
                report_path=str(report or result.report_path),
                validation=result.run.to_dict(),
            )
        )
    else:
        _print_literal(f"release: {result.run.status}; report: {report or result.report_path}")

    raise typer.Exit(result.exit_code)


@validate_app.command(name="report")
def report_command(
    path: Annotated[
        Path,
        typer.Argument(
            help="Validation report JSON path, for example .easycat/validation/latest.json."
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the standard machine-readable stdout envelope."),
    ] = False,
) -> None:
    """Render a concise validation report summary."""
    payload = _load_report_payload(path, json_output=json_output)
    status = str(payload.get("status", "unknown"))
    exit_code = _report_exit_code(payload, path, json_output=json_output)

    if json_output:
        emit_json(
            json_envelope(
                "validate report",
                status="ok" if status == "pass" else "error",
                report_path=str(path),
                exit_code=exit_code,
                validation=payload,
            )
        )
        raise typer.Exit(0 if status == "pass" else 1)

    _print_literal(f"{payload['kind']} {payload['run_id']}: {status}")
    _print_literal(f"command: {_format_command(payload.get('command'))}")
    _print_literal(f"duration: {_format_duration(payload.get('duration_s'))}")
    _print_literal(f"exit_code: {exit_code}")
    git = payload.get("git")
    if isinstance(git, dict):
        _print_literal(
            f"git: {git.get('branch', '')} {git.get('sha', '')} dirty={git.get('dirty')}"
        )

    for check in cast("list[object]", payload.get("checks", [])):
        if isinstance(check, dict):
            _print_literal(f"- {check.get('name', 'unknown')}: {check.get('status', 'unknown')}")

    skips = cast("list[object]", payload.get("skips") or [])
    failures = cast("list[object]", payload.get("failures") or [])
    for skip in skips:
        if isinstance(skip, dict):
            _print_literal(
                "skip: "
                f"{skip.get('name', 'unknown')} "
                f"expected={skip.get('expected')} "
                f"{skip.get('reason', '')}"
            )
    for failure in failures:
        if isinstance(failure, dict):
            failure_class = failure.get("failure_class") or ""
            _print_literal(
                f"failure: {failure.get('name', 'unknown')} "
                f"{failure_class} {failure.get('message', '')}"
            )

    _render_latency_percentiles(payload.get("latency"))
    _render_artifacts(payload.get("artifacts"))

    raise typer.Exit(0 if status == "pass" else 1)


def _render_latency_percentiles(latency: object) -> None:
    if not isinstance(latency, dict):
        return
    percentiles = latency.get("percentiles")
    if not isinstance(percentiles, dict):
        return
    overall = percentiles.get("overall")
    if not isinstance(overall, dict):
        return
    for stage, stats in sorted(overall.items()):
        if not isinstance(stats, dict):
            continue
        tokens = [stage]
        for percentile in ("p50", "p90", "p95", "p99"):
            value = stats.get(percentile)
            if value is None:
                continue
            tokens.append(f"{percentile}={_format_percentile_value(value)}")
        if len(tokens) > 1:
            _print_literal(" ".join(tokens))


def _format_percentile_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}"
    return str(value)


def _report_exit_code(
    payload: dict[str, object],
    path: Path,
    *,
    json_output: bool,
) -> int:
    raw_exit_code = payload.get("exit_code", 1)
    if raw_exit_code is None:
        return 0
    try:
        if isinstance(raw_exit_code, bool):
            raise ValueError  # noqa: TRY004 domain-specific validation error
        if isinstance(raw_exit_code, int):
            return raw_exit_code
        if isinstance(raw_exit_code, float):
            if not math.isfinite(raw_exit_code) or not raw_exit_code.is_integer():
                raise ValueError
            return int(raw_exit_code)
        if isinstance(raw_exit_code, str):
            return int(raw_exit_code)
        raise ValueError
    except (TypeError, ValueError, OverflowError):
        _report_load_error(
            path,
            "invalid validation report JSON: exit_code must be an integer",
            json_output=json_output,
        )


def _report_load_error(path: Path, message: str, *, json_output: bool) -> NoReturn:
    emit_command_error(
        "validate report",
        message,
        json_output=json_output,
        human_console=stdout_console,
        report_path=str(path),
    )
    raise typer.Exit(2)


def _load_report_payload(path: Path, *, json_output: bool = False) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _report_load_error(
            path,
            f"validation report not found: {path} ({exc})",
            json_output=json_output,
        )

    try:
        payload = json.loads(raw)
    except (RecursionError, ValueError) as exc:
        _report_load_error(
            path,
            f"invalid validation report JSON: {path} ({exc})",
            json_output=json_output,
        )

    if not isinstance(payload, dict):
        _report_load_error(
            path,
            "invalid validation report JSON: expected object",
            json_output=json_output,
        )
    if payload.get("schema_version") != 1:
        _report_load_error(
            path,
            f"unsupported validation report schema_version: {payload.get('schema_version')}",
            json_output=json_output,
        )
    if payload.get("kind") != "validation_run":
        _report_load_error(
            path,
            f"unknown validation report kind: {payload.get('kind')}",
            json_output=json_output,
        )
    if not payload.get("run_id"):
        _report_load_error(
            path,
            "invalid validation report JSON: missing run_id",
            json_output=json_output,
        )
    return payload


def _format_duration(duration: object) -> str:
    """Render a finite non-negative numeric duration with a safe fallback."""
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return "0.00s"
    try:
        value = float(duration)
    except OverflowError:
        return "0.00s"
    if value < 0 or not math.isfinite(value):
        return "0.00s"
    return f"{value:.2f}s"


def _format_command(command: object) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _render_artifacts(artifacts: object) -> None:
    if not isinstance(artifacts, dict):
        return
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path")
        if not path:
            continue
        suffix = "" if Path(str(path)).exists() else " [missing]"
        stdout_console.file.write(f"artifact {name}: {path}{suffix}\n")
        stdout_console.file.flush()
