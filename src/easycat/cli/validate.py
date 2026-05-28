from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from easycat.cli._output import emit_json, json_envelope, stdout_console
from easycat.validation.latency import LatencyMode
from easycat.validation.runner import (
    ValidationRunResult,
    run_latency_validation,
    run_live_validation,
    run_validation_slice,
)

validate_app = typer.Typer(
    name="validate",
    help="Run validation checks and inspect validation reports.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _run_slice(
    slice_name: str,
    *,
    json_output: bool,
    report: Path | None,
    junit: Path | None,
    artifacts_dir: Path,
    junit_prefix: str | None,
) -> None:
    result = run_validation_slice(
        slice_name,
        artifacts_dir=artifacts_dir,
        report_path=report,
        junit_path=junit,
        junit_prefix=junit_prefix,
    )
    if report is not None and not report.exists():
        _write_report_copy(report, result)

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
        stdout_console.print(
            f"{slice_name}: {result.run.status}; report: {report or result.report_path}"
        )

    raise typer.Exit(result.exit_code)


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
        typer.Option("--artifacts-dir", help="Validation artifact root directory."),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
) -> None:
    """Run deterministic local validation for normal PR work."""
    _run_slice(
        "quick",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
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
        typer.Option("--artifacts-dir", help="Validation artifact root directory."),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
) -> None:
    """Run localhost socket integration validation."""
    _run_slice(
        "socket",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
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
        typer.Option("--artifacts-dir", help="Validation artifact root directory."),
    ] = Path(".easycat/validation"),
    junit_prefix: Annotated[
        str | None,
        typer.Option("--junit-prefix", help="Optional pytest JUnit prefix."),
    ] = None,
) -> None:
    """Run local stress validation and saturation-signal capture."""
    _run_slice(
        "stress",
        json_output=json_output,
        report=report,
        junit=junit,
        artifacts_dir=artifacts_dir,
        junit_prefix=junit_prefix,
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
        typer.Option("--require-samples", help="Fail when no latency samples are produced."),
    ] = False,
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", help="Validation artifact root directory."),
    ] = Path(".easycat/validation"),
) -> None:
    """Run live latency validation and write structured latency artifacts."""
    if smoke and sweep:
        stdout_console.print("choose only one of --smoke or --sweep")
        raise typer.Exit(2)

    mode = LatencyMode.SWEEP if sweep else LatencyMode.SMOKE
    result = run_latency_validation(
        mode,
        artifacts_dir=artifacts_dir,
        report_path=report,
        require_samples=require_samples,
    )
    if report is not None and not report.exists():
        _write_report_copy(report, result)

    if json_output:
        status = "ok" if result.exit_code == 0 else "error"
        emit_json(
            json_envelope(
                f"validate latency {mode.value}",
                status=status,
                exit_code=result.exit_code,
                report_path=str(report or result.report_path),
                validation=result.run.to_dict(),
            )
        )
    else:
        stdout_console.print(
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
        typer.Option("--artifacts-dir", help="Validation artifact root directory."),
    ] = Path(".easycat/validation"),
) -> None:
    """Run live provider canaries and emit capability reports."""
    result = run_live_validation(
        providers=provider,
        surfaces=surface,
        strict=strict,
        release=release,
        artifacts_dir=artifacts_dir,
        report_path=report,
    )
    if report is not None and not report.exists():
        _write_report_copy(report, result)

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
        stdout_console.print(f"live: {result.run.status}; report: {report or result.report_path}")

    raise typer.Exit(result.exit_code)


@validate_app.command(name="report")
def report_command(
    path: Annotated[Path, typer.Argument(help="Validation report JSON path.")],
) -> None:
    """Render a concise validation report summary."""
    payload = _load_report_payload(path)
    status = str(payload.get("status", "unknown"))
    exit_code = int(payload.get("exit_code", 1) or 0)

    stdout_console.print(f"{payload['kind']} {payload['run_id']}: {status}")
    stdout_console.print(f"command: {_format_command(payload.get('command'))}")
    stdout_console.print(f"duration: {payload.get('duration_s', 0):.2f}s")
    stdout_console.print(f"exit_code: {exit_code}")
    git = payload.get("git")
    if isinstance(git, dict):
        stdout_console.print(
            f"git: {git.get('branch', '')} {git.get('sha', '')} dirty={git.get('dirty')}"
        )

    for check in payload.get("checks", []):
        if isinstance(check, dict):
            stdout_console.print(
                f"- {check.get('name', 'unknown')}: {check.get('status', 'unknown')}"
            )

    skips = payload.get("skips") or []
    failures = payload.get("failures") or []
    for skip in skips:
        if isinstance(skip, dict):
            stdout_console.print(
                "skip: "
                f"{skip.get('name', 'unknown')} "
                f"expected={skip.get('expected')} "
                f"{skip.get('reason', '')}"
            )
    for failure in failures:
        if isinstance(failure, dict):
            failure_class = failure.get("failure_class") or ""
            stdout_console.print(
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
            stdout_console.print(" ".join(tokens))


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


def _load_report_payload(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text()
    except OSError as exc:
        stdout_console.print(f"validation report not found: {path} ({exc})")
        raise typer.Exit(2) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        stdout_console.print(f"invalid validation report JSON: {path} ({exc})")
        raise typer.Exit(2) from exc

    if not isinstance(payload, dict):
        stdout_console.print("invalid validation report JSON: expected object")
        raise typer.Exit(2)
    if payload.get("schema_version") != 1:
        stdout_console.print(
            f"unsupported validation report schema_version: {payload.get('schema_version')}"
        )
        raise typer.Exit(2)
    if payload.get("kind") != "validation_run":
        stdout_console.print(f"unknown validation report kind: {payload.get('kind')}")
        raise typer.Exit(2)
    if not payload.get("run_id"):
        stdout_console.print("invalid validation report JSON: missing run_id")
        raise typer.Exit(2)
    return payload


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


def _write_report_copy(path: Path, result: ValidationRunResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.run.to_json())
