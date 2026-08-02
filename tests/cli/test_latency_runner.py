from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easycat.validation.latency import (
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
)
from easycat.validation.runner import (
    LATENCY_SYNTHETIC_FAILURE_SAMPLE,
    LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY,
    CommandResult,
    run_latency_validation,
)


def test_latency_runner_writes_report_and_smoke_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
        samples_path.write_text(
            json.dumps(
                [
                    LatencySample(
                        sample_id="sample-1",
                        condition_id="baseline",
                        warmup=False,
                        timestamp_source="event_monotonic",
                        stages=LatencyStageDurations(total_ms=750.0),
                    ).to_dict()
                ]
            )
        )
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 0
    report = json.loads(result.report_path.read_text())
    assert report["latency"]["mode"] == "smoke"
    assert report["latency"]["environment"]["ci"] is False
    assert report["latency"]["samples"][0]["sample_id"] == "sample-1"
    assert (result.run_dir / "latency" / "smoke.json").exists()
    assert (tmp_path / "latency" / "smoke-latest.json").exists()


def test_latency_runner_embeds_reliability_samples(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        sample = LatencySample(
            sample_id="sample-1",
            condition_id="baseline",
            warmup=False,
            timestamp_source="event_monotonic",
            stages=LatencyStageDurations(total_ms=750.0),
        )
        reliability = ReliabilitySample(
            sample_id="sample-1",
            condition_id="baseline",
            mode="latency",
            informational=True,
            eligible=False,
            signals=ReliabilitySignals(journal_degraded=False, active_sessions=1),
        )
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(json.dumps([sample.to_dict()]))
        reliability_path = Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"])
        reliability_path.parent.mkdir(parents=True, exist_ok=True)
        reliability_path.write_text(json.dumps([reliability.to_dict()]))
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert report["latency"]["reliability_samples"][0]["sample_id"] == "sample-1"
    assert report["latency"]["reliability_samples"][0]["signals"]["journal_degraded"] is False


def test_latency_runner_writes_failure_sample_when_pytest_fails_before_sample(
    tmp_path: Path,
) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=1, stdout="", stderr="invalid_api_key")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    report = json.loads(result.report_path.read_text())
    synthetic = report["latency"]["samples"][0]
    assert synthetic["missing_stage_reason"] == "invalid_api_key"
    assert synthetic["failure_class"] == "provider_auth"
    # The fabricated sample is tagged so consumers can filter it from counts.
    assert synthetic["debug"][LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY] == (
        LATENCY_SYNTHETIC_FAILURE_SAMPLE
    )
    assert report["failures"][0]["failure_class"] == "provider_auth"


def test_latency_runner_redacts_exact_runtime_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        sample = LatencySample(
            sample_id="sample-1",
            condition_id="baseline",
            warmup=False,
            timestamp_source="event_monotonic",
            stages=LatencyStageDurations(),
            debug={"exception": f"provider returned {secret}"},
            missing_stage_reason=f"provider returned {secret}",
        )
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(json.dumps([sample.to_dict()]))
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        junit_target = Path(junit_arg.removeprefix("--junitxml="))
        junit_target.parent.mkdir(parents=True, exist_ok=True)
        junit_target.write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<testsuites><testsuite><testcase><failure message="provider returned {secret}">'
            f"{secret}</failure></testcase></testsuite></testsuites>"
        )
        return CommandResult(exit_code=1, stdout=f"stdout {secret}", stderr=f"stderr {secret}")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    serialized_report = result.report_path.read_text()
    serialized_latency = (result.run_dir / "latency" / "smoke.json").read_text()
    serialized_latest_latency = (tmp_path / "latency" / "smoke-latest.json").read_text()
    serialized_source_samples = (result.run_dir / "latency" / "samples.json").read_text()

    assert result.exit_code == 1
    assert secret not in serialized_report
    assert secret not in serialized_latency
    assert secret not in serialized_latest_latency
    assert secret not in serialized_source_samples
    json.loads(serialized_source_samples)
    assert secret not in (result.run_dir / "stdout.log").read_text()
    assert secret not in (result.run_dir / "stderr.log").read_text()
    assert secret not in (result.run_dir / "junit.xml").read_text()
    assert "[REDACTED_SECRET]" in serialized_report
    assert "[REDACTED_SECRET]" in serialized_latency


def test_latency_runner_does_not_ingest_samples_when_redaction_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    original_write_text = Path.write_text
    samples_path: Path | None = None

    def guarded_write_text(path: Path, *args, **kwargs):
        if path == samples_path:
            raise PermissionError("simulated sample redaction write failure")
        return original_write_text(path, *args, **kwargs)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        nonlocal samples_path
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
        sample = LatencySample(
            sample_id="unsafe-sample",
            condition_id="baseline",
            warmup=False,
            timestamp_source="event_monotonic",
            stages=LatencyStageDurations(total_ms=750.0),
            debug={"credential_echo": secret},
        )
        original_write_text(samples_path, json.dumps([sample.to_dict()]))
        monkeypatch.setattr(Path, "write_text", guarded_write_text)
        return CommandResult(exit_code=0)

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["tool_exit_codes"]["artifact_redaction"] == 1
    assert report["latency"]["samples"] == []
    assert any(failure["name"] == "artifact_redaction.samples" for failure in report["failures"])
    assert secret not in result.report_path.read_text()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            '[{"credential_echo":"plain-runtime-token-value","too_large":' + "1" * 5000 + "}]",
            id="oversized-json-integer",
        ),
        pytest.param(
            '[{"credential_echo":"plain-runtime-token-value","nested":'
            + "[" * 2000
            + "0"
            + "]" * 2000
            + "}]",
            id="recursive-json",
        ),
    ],
)
def test_latency_runner_fails_closed_when_structured_redaction_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(payload)
        return CommandResult(exit_code=0)

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["tool_exit_codes"]["artifact_redaction"] == 1
    assert report["latency"]["samples"] == []
    assert any(
        failure["name"] == "artifact_redaction.samples"
        and failure["failure_class"] == "artifact_redaction_error"
        for failure in report["failures"]
    )
    assert secret not in result.report_path.read_text()


def test_latency_runner_reports_malformed_samples_without_crashing(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
        samples_path.write_text("{not-json")
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    report = json.loads(result.report_path.read_text())
    assert report["status"] == "fail"
    # SWEEP requires samples by default, so an unloadable artifact also trips the
    # required-samples gate alongside the load-error gate.
    assert report["tool_exit_codes"] == {
        "latency_samples": 1,
        "pytest": 0,
        "required_latency_samples": 1,
    }
    assert report["failures"][0]["name"] == "latency.samples"
    checks_by_name = {check["name"]: check for check in report["checks"]}
    assert checks_by_name["pytest.latency.sweep"]["status"] == "pass"
    assert checks_by_name["latency.samples"]["status"] == "fail"
    assert {
        failure["message"] for failure in checks_by_name["latency.samples"]["details"]["failures"]
    } == {
        report["failures"][0]["message"],
        "required latency validation produced no samples",
    }
    assert (result.run_dir / "latency" / "sweep.json").exists()
    assert (tmp_path / "latency" / "sweep-latest.json").exists()


def test_latency_runner_reports_deeply_nested_samples_without_crashing(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text("[" * 10_000 + "0" + "]" * 10_000)
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["tool_exit_codes"] == {
        "latency_samples": 1,
        "pytest": 0,
        "required_latency_samples": 1,
    }
    assert report["failures"][0]["failure_class"] == "latency_artifact_error"


@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
def test_latency_runner_rejects_non_finite_sample_measurements(
    tmp_path: Path,
    non_finite: str,
) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(
            "["
            '{"sample_id":"sample-1","condition_id":"baseline","warmup":false,'
            '"timestamp_source":"event_monotonic","stages":{"total_ms":'
            f"{non_finite}"
            "}}]"
        )
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["status"] == "fail"
    assert report["latency"]["samples"] == []
    assert report["tool_exit_codes"]["latency_samples"] == 1
    assert any(
        failure["failure_class"] == "latency_artifact_error" for failure in report["failures"]
    )


def test_latency_runner_reports_overflowing_sample_measurement(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        overflowing_integer = "9" * 400
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(
            "["
            '{"sample_id":"sample-1","condition_id":"baseline","warmup":false,'
            '"timestamp_source":"event_monotonic","stages":{"total_ms":'
            f"{overflowing_integer}"
            "}}]"
        )
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["status"] == "fail"
    assert report["latency"]["samples"] == []
    assert report["tool_exit_codes"]["latency_samples"] == 1
    assert any(
        failure["failure_class"] == "latency_artifact_error" for failure in report["failures"]
    )


def test_latency_runner_can_require_samples_for_release_gates(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="skipped", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        require_samples=True,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["status"] == "fail"
    assert report["tool_exit_codes"] == {"pytest": 0, "required_latency_samples": 1}
    assert report["failures"][0]["message"] == "required latency validation produced no samples"
    checks_by_name = {check["name"]: check for check in report["checks"]}
    assert checks_by_name["pytest.latency.sweep"]["status"] == "pass"
    assert checks_by_name["latency.samples"]["status"] == "fail"


def test_latency_runner_smoke_required_samples_failure_records_failed_check(
    tmp_path: Path,
) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="skipped", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        require_samples=True,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["status"] == "fail"
    checks_by_name = {check["name"]: check for check in report["checks"]}
    assert checks_by_name["pytest.latency.smoke"]["status"] == "pass"
    assert checks_by_name["latency.samples"]["status"] == "fail"
    assert "latency.budget" not in checks_by_name


def test_latency_runner_sweep_requires_samples_by_default(tmp_path: Path) -> None:
    """An empty SWEEP run must fail rather than silently report pass."""

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="skipped", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["status"] == "fail"
    assert report["tool_exit_codes"]["required_latency_samples"] == 1


def test_latency_runner_smoke_allows_empty_samples_by_default(tmp_path: Path) -> None:
    """SMOKE may legitimately produce no samples, so an empty run still passes."""

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="skipped", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert report["status"] == "pass"
    assert "required_latency_samples" not in report["tool_exit_codes"]


def test_latency_runner_sweep_require_samples_can_be_disabled(tmp_path: Path) -> None:
    """Passing require_samples=False explicitly overrides the SWEEP default."""

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="skipped", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        require_samples=False,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert "required_latency_samples" not in report["tool_exit_codes"]


def test_latency_runner_reports_malformed_reliability_samples_without_crashing(
    tmp_path: Path,
) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        sample = LatencySample(
            sample_id="sample-1",
            condition_id="baseline",
            warmup=False,
            timestamp_source="event_monotonic",
            stages=LatencyStageDurations(total_ms=750.0),
        )
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(json.dumps([sample.to_dict()]))
        Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"]).write_text("{not-json")
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    report = json.loads(result.report_path.read_text())
    assert report["status"] == "fail"
    assert report["tool_exit_codes"] == {"pytest": 0, "reliability_samples": 1}
    assert report["failures"][0]["name"] == "reliability.samples"
    checks_by_name = {check["name"]: check for check in report["checks"]}
    assert checks_by_name["pytest.latency.smoke"]["status"] == "pass"
    assert checks_by_name["reliability.samples"]["status"] == "fail"
    assert report["failures"][0]["message"] in (
        failure["message"]
        for failure in checks_by_name["reliability.samples"]["details"]["failures"]
    )
    assert report["latency"]["samples"][0]["sample_id"] == "sample-1"


def test_latency_runner_reports_deeply_nested_reliability_samples_without_crashing(
    tmp_path: Path,
) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        sample = LatencySample(
            sample_id="sample-1",
            condition_id="baseline",
            warmup=False,
            timestamp_source="event_monotonic",
            stages=LatencyStageDurations(total_ms=750.0),
        )
        Path(env["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(json.dumps([sample.to_dict()]))
        Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"]).write_text("[" * 10_000 + "0" + "]" * 10_000)
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["tool_exit_codes"] == {"pytest": 0, "reliability_samples": 1}
    assert report["failures"][0]["failure_class"] == "reliability_artifact_error"
