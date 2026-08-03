from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.validation.latency import LatencyMode, build_latency_artifact
from easycat.validation.report import ValidationRun
from easycat.validation.runner import ValidationRunResult


def test_validate_latency_cli_runs_smoke_and_writes_report(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "latency.json"
    called: dict[str, object] = {}

    def fake_run_latency_validation(mode: LatencyMode | str, **kwargs) -> ValidationRunResult:
        called["mode"] = mode
        called.update(kwargs)
        run = ValidationRun(
            run_id="20260522T120000Z-latency-smoke-12345",
            command=["uv", "run", "pytest", "-q"],
            started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 5, 22, 12, 0, 1, tzinfo=UTC),
            duration_s=1.0,
            status="pass",
            exit_code=0,
            latency=build_latency_artifact(
                mode=LatencyMode.SMOKE,
                samples=[],
                generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            ),
        )
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        # Mirror the real runner contract: it is the authoritative writer of
        # the requested ``--report`` path (the CLI no longer copies it).
        requested = kwargs.get("report_path")
        if requested is not None:
            Path(requested).write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=requested or result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_latency_validation", fake_run_latency_validation)

    result = cli.invoke(
        app,
        ["validate", "latency", "--smoke", "--report", str(report_path)],
    )

    assert result.exit_code == 0
    assert "latency smoke: pass" in result.stdout
    assert report_path.exists()
    assert called["mode"] == LatencyMode.SMOKE
    assert called["report_path"] == report_path
    # No --require-samples flag: defer to the runner's mode-aware default.
    assert called["require_samples"] is None


def test_validate_latency_cli_can_require_samples(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: dict[str, object] = {}

    def fake_run_latency_validation(mode: LatencyMode | str, **kwargs) -> ValidationRunResult:
        called["mode"] = mode
        called.update(kwargs)
        run = ValidationRun(
            run_id="20260522T120000Z-latency-sweep-12345",
            command=["uv", "run", "pytest", "-q"],
            started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 5, 22, 12, 0, 1, tzinfo=UTC),
            duration_s=1.0,
            status="pass",
            exit_code=0,
            latency=build_latency_artifact(
                mode=LatencyMode.SWEEP,
                samples=[],
                generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            ),
        )
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_latency_validation", fake_run_latency_validation)

    result = cli.invoke(app, ["validate", "latency", "--sweep", "--require-samples"])

    assert result.exit_code == 0
    assert called["mode"] == LatencyMode.SWEEP
    assert called["require_samples"] is True


def test_validate_latency_cli_rejects_smoke_and_sweep_together(cli: CliRunner) -> None:
    result = cli.invoke(app, ["validate", "latency", "--smoke", "--sweep"])

    assert result.exit_code == 2
    # Usage errors route to stderr like every other command (not stdout).
    assert "choose only one of --smoke or --sweep" in result.stderr
    assert "choose only one of --smoke or --sweep" not in result.stdout


def test_validate_latency_cli_rejects_smoke_and_sweep_json_envelope(
    cli: CliRunner,
) -> None:
    result = cli.invoke(app, ["validate", "latency", "--smoke", "--sweep", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    # ``command`` is the constant "validate latency" on the usage-error path,
    # matching the success path (never "validate latency <mode>").
    assert payload["command"] == "validate latency"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "choose only one of --smoke or --sweep" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_validate_latency_cli_json_uses_standard_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_latency_validation(mode: LatencyMode | str, **kwargs) -> ValidationRunResult:
        run = ValidationRun(
            run_id="20260522T120000Z-latency-sweep-12345",
            command=["uv", "run", "pytest", "-q"],
            started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 5, 22, 12, 0, 1, tzinfo=UTC),
            duration_s=1.0,
            status="pass",
            exit_code=0,
            latency=build_latency_artifact(
                mode=LatencyMode.SWEEP,
                samples=[],
                generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            ),
        )
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_latency_validation", fake_run_latency_validation)

    result = cli.invoke(app, ["validate", "latency", "--sweep", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # ``command`` is a constant; ``mode`` is carried as a separate field
    # (previously the mode was interpolated into ``command`` as
    # "validate latency sweep", which broke command-based dispatch).
    assert payload["command"] == "validate latency"
    assert "sweep" not in payload["command"]
    assert payload["mode"] == "sweep"
    assert payload["validation"]["latency"]["mode"] == "sweep"
