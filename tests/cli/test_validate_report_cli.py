from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.validation.report import (
    ArtifactRef,
    GitMetadata,
    ValidationCheck,
    ValidationFailure,
    ValidationSkip,
)

from ._validation_helpers import _validation_run


def test_validate_report_help_names_latest_report_path(cli: CliRunner) -> None:
    result = cli.invoke(app, ["validate", "report", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "Validation report JSON path" in help_text
    assert ".easycat/validation/latest.json" in help_text


def test_validate_report_cli_renders_summary(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    junit_path = tmp_path / "junit.xml"
    junit_path.write_text("<testsuite />")
    run = _validation_run(
        artifacts={
            "junit": ArtifactRef(kind="junit", path=str(junit_path)),
            "missing": ArtifactRef(kind="log", path=str(tmp_path / "missing.log")),
        },
        skips=[ValidationSkip(name="provider.openai", reason="OPENAI_API_KEY missing")],
    )
    report_path.write_text(run.to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 0
    assert "validation_run" in result.stdout
    assert "pytest.quick" in result.stdout
    assert "git: feature/validation abc123 dirty=True" in result.stdout
    assert "skip: provider.openai expected=True OPENAI_API_KEY missing" in result.stdout
    assert f"artifact junit: {junit_path}" in result.stdout
    assert f"artifact missing: {tmp_path / 'missing.log'} [missing]" in result.stdout


def test_validate_report_cli_renders_bracketed_text_literally(
    cli: CliRunner, tmp_path: Path
) -> None:
    report_path = tmp_path / "report.json"
    run = _validation_run(
        run_id="run[dev]",
        command=["uv", "run", "pytest", "easycat[openai-agents]"],
        git=GitMetadata(sha="abc[123]", branch="feature/[dev]", dirty=False),
        checks=[ValidationCheck(name="pytest[quick]", status="pass", duration_s=0.1)],
        skips=[
            ValidationSkip(
                name="provider[openai]",
                reason="install easycat[openai-agents]",
            )
        ],
        failures=[
            ValidationFailure(
                name="case[one]",
                message="failed with easycat[deepgram]",
                failure_class="class[dev]",
            )
        ],
    )
    report_path.write_text(run.to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 0
    assert "validation_run run[dev]: pass" in result.stdout
    assert "command: uv run pytest easycat[openai-agents]" in result.stdout
    assert "git: feature/[dev] abc[123] dirty=False" in result.stdout
    assert "- pytest[quick]: pass" in result.stdout
    assert "skip: provider[openai] expected=True install easycat[openai-agents]" in result.stdout
    assert "failure: case[one] class[dev] failed with easycat[deepgram]" in result.stdout


def test_validate_report_cli_json_uses_standard_stdout_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    run = _validation_run()
    report_path.write_text(run.to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate report"
    assert payload["status"] == "ok"
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 0
    assert payload["validation"]["kind"] == "validation_run"
    assert payload["validation"]["run_id"] == run.run_id
    assert "validation_run" not in result.stderr


def test_validate_report_cli_rejects_invalid_json(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("{no")

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 2
    assert "invalid validation report JSON" in result.stdout


def test_validate_report_cli_json_rejects_invalid_json_with_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("{no")

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate report"
    assert payload["status"] == "error"
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 2
    assert "invalid validation report JSON" in payload["message"]


def test_validate_report_cli_rejects_missing_report(cli: CliRunner, tmp_path: Path) -> None:
    result = cli.invoke(app, ["validate", "report", str(tmp_path / "missing.json")])

    assert result.exit_code == 2
    assert "validation report not found" in result.stdout


def test_validate_report_cli_json_rejects_missing_report_with_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "missing.json"

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate report"
    assert payload["status"] == "error"
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 2
    assert "validation report not found" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_validate_report_cli_rejects_unsupported_schema(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    payload = _validation_run().to_dict()
    payload["schema_version"] = 999
    report_path.write_text(json.dumps(payload))

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 2
    assert "unsupported validation report schema_version: 999" in result.stdout


def test_validate_report_cli_json_rejects_unsupported_schema_with_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    payload = _validation_run().to_dict()
    payload["schema_version"] = 999
    report_path.write_text(json.dumps(payload))

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate report"
    assert payload["status"] == "error"
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 2
    assert "unsupported validation report schema_version: 999" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_validate_report_cli_rejects_unknown_kind(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    payload = _validation_run().to_dict()
    payload["kind"] = "other"
    report_path.write_text(json.dumps(payload))

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 2
    assert "unknown validation report kind: other" in result.stdout


def test_validate_report_cli_json_rejects_unknown_kind_with_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    payload = _validation_run().to_dict()
    payload["kind"] = "other"
    report_path.write_text(json.dumps(payload))

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate report"
    assert payload["status"] == "error"
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 2
    assert "unknown validation report kind: other" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_validate_report_cli_renders_latency_percentiles(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    """When latency.percentiles is present, render a one-line summary per stage."""
    report_path = tmp_path / "report.json"
    run = _validation_run(
        latency={
            "schema_version": 1,
            "kind": "latency_validation",
            "mode": "sweep",
            "generated_at": "2026-05-22T12:00:00Z",
            "baseline": {"comparison": "not_configured"},
            "environment": {},
            "clock_source": "time.monotonic",
            "samples": [],
            "reliability_samples": [],
            "summary": {},
            "percentiles": {
                "overall": {
                    "total_ms": {
                        "p50": 500.0,
                        "p90": 900.0,
                        "p95": 1100.0,
                        "p99": 1300.0,
                        "count": 20,
                    },
                    "tts_ttfb_ms": {
                        "p50": 80.0,
                        "p90": 120.0,
                        "p95": 150.0,
                        "p99": 180.0,
                        "count": 20,
                    },
                },
                "by_condition": {},
            },
            "budget_violations": [],
        },
    )
    report_path.write_text(run.to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 0
    # Each stage rendered on its own line with p50/p95/p99 figures.
    assert "total_ms" in result.stdout
    assert "p50=500" in result.stdout
    assert "p95=1100" in result.stdout
    assert "p99=1300" in result.stdout
    assert "tts_ttfb_ms" in result.stdout
    assert "p95=150" in result.stdout


def test_validate_report_cli_renders_failed_run_details(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        _validation_run(
            status="fail",
            exit_code=1,
            tool_exit_codes={"pytest": 1},
            failures=[
                ValidationFailure(
                    name="pytest.quick",
                    message="1 test failed",
                    failure_class="easycat_regression",
                )
            ],
        ).to_json()
    )

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 1
    assert "failure: pytest.quick easycat_regression 1 test failed" in result.stdout


def test_validate_report_cli_json_renders_failed_run_details(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        _validation_run(
            status="fail",
            exit_code=1,
            tool_exit_codes={"pytest": 1},
            failures=[
                ValidationFailure(
                    name="pytest.quick",
                    message="1 test failed",
                    failure_class="easycat_regression",
                )
            ],
        ).to_json()
    )

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["command"] == "validate report"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1
    assert payload["validation"]["status"] == "fail"
    assert payload["validation"]["failures"][0]["failure_class"] == "easycat_regression"
