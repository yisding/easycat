from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.validation.latency import (
    LatencyComparisonThresholds,
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
    append_reliability_sample,
    build_latency_artifact,
    classify_latency_failure,
    compare_latency_baseline,
    latency_pytest_args,
)
from easycat.validation.report import ValidationRun
from easycat.validation.runner import CommandResult, ValidationRunResult, run_latency_validation


def test_latency_pytest_args_smoke_selects_single_probe() -> None:
    assert latency_pytest_args(LatencyMode.SMOKE) == [
        "tests/e2e/test_plan_7_latency_benchmark.py::test_single_full_stack_latency_probe"
    ]


def test_latency_pytest_args_sweep_selects_matrix_probe() -> None:
    assert latency_pytest_args(LatencyMode.SWEEP) == [
        "tests/e2e/test_plan_7_latency_benchmark.py::test_latency_benchmark_by_pipeline_flags"
    ]


def test_latency_sample_serializes_canonical_fields() -> None:
    sample = LatencySample(
        sample_id="sample-1",
        condition_id="baseline",
        warmup=False,
        timestamp_source="event_monotonic",
        provider={"stt": "openai-realtime", "tts": "openai", "agent": "openai"},
        model={"llm": "gpt-5.4", "tts": "gpt-4o-mini-tts"},
        transport={"kind": "websocket"},
        debug={"level": "full"},
        stages=LatencyStageDurations(total_ms=750.0, stt_ms=120.0),
    )

    payload = sample.to_dict()

    assert payload["sample_id"] == "sample-1"
    assert payload["condition_id"] == "baseline"
    assert payload["warmup"] is False
    assert payload["timestamp_source"] == "event_monotonic"
    assert payload["provider"]["stt"] == "openai-realtime"
    assert payload["model"]["llm"] == "gpt-5.4"
    assert payload["transport"] == {"kind": "websocket"}
    assert payload["debug"] == {"level": "full"}
    assert payload["stages"]["total_ms"] == 750.0
    assert payload["missing_stage_reason"] is None
    assert payload["failure_class"] is None


def test_latency_artifact_marks_low_sample_percentiles_ineligible() -> None:
    sample = LatencySample(
        sample_id="sample-1",
        condition_id="baseline",
        warmup=False,
        timestamp_source="event_monotonic",
        stages=LatencyStageDurations(total_ms=750.0),
    )

    artifact = build_latency_artifact(
        mode=LatencyMode.SMOKE,
        samples=[sample],
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert artifact["kind"] == "latency_validation"
    assert artifact["mode"] == "smoke"
    assert artifact["baseline"]["comparison"] == "not_configured"
    assert artifact["clock_source"] == "time.monotonic"
    assert artifact["samples"][0]["sample_id"] == "sample-1"
    assert artifact["summary"]["baseline"]["count"] == 1
    assert artifact["summary"]["baseline"]["p50_ms"]["eligible"] is False
    assert artifact["summary"]["baseline"]["p90_ms"]["eligible"] is False
    assert artifact["summary"]["baseline"]["p95_ms"]["eligible"] is False
    assert artifact["summary"]["baseline"]["p99_ms"]["eligible"] is False


def test_latency_artifact_preserves_missing_stage_and_failure_class() -> None:
    sample = LatencySample(
        sample_id="sample-1",
        condition_id="baseline",
        warmup=False,
        timestamp_source="event_monotonic",
        stages=LatencyStageDurations(stt_ms=140.0, total_ms=None),
        missing_stage_reason="first_tts_audio_missing",
        failure_class="provider_timeout",
    )

    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=[sample],
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert artifact["samples"][0]["stages"]["total_ms"] is None
    assert artifact["samples"][0]["missing_stage_reason"] == "first_tts_audio_missing"
    assert artifact["samples"][0]["failure_class"] == "provider_timeout"
    assert artifact["summary"] == {}


def test_reliability_sample_serializes_saturation_signals() -> None:
    sample = ReliabilitySample(
        sample_id="sample-1",
        condition_id="baseline",
        mode="latency",
        informational=True,
        eligible=False,
        signals=ReliabilitySignals(
            event_loop_lag_ms=12.5,
            queue_depth=3,
            dropped_frames=1,
            journal_degraded=False,
            active_sessions=2,
            memory_growth_kib=1024,
        ),
    )

    payload = sample.to_dict()

    assert payload["sample_id"] == "sample-1"
    assert payload["condition_id"] == "baseline"
    assert payload["mode"] == "latency"
    assert payload["informational"] is True
    assert payload["eligible"] is False
    assert payload["signals"]["event_loop_lag_ms"] == 12.5
    assert payload["signals"]["queue_depth"] == 3
    assert payload["signals"]["dropped_frames"] == 1
    assert payload["signals"]["journal_degraded"] is False
    assert payload["signals"]["active_sessions"] == 2
    assert payload["signals"]["memory_growth_kib"] == 1024
    assert "unavailable_reason" not in payload["signals"]


def test_append_reliability_sample_accumulates_json(tmp_path: Path) -> None:
    first = ReliabilitySample(
        sample_id="sample-1",
        condition_id="stress",
        mode="stress",
        informational=True,
        eligible=False,
        signals=ReliabilitySignals(journal_degraded=False),
    )
    second = ReliabilitySample(
        sample_id="sample-2",
        condition_id="stress",
        mode="stress",
        informational=True,
        eligible=False,
        signals=ReliabilitySignals(unavailable_reason="queue_depth_unavailable"),
    )

    destination = tmp_path / "reliability.json"
    append_reliability_sample(destination, first)
    append_reliability_sample(destination, second)

    payload = json.loads(destination.read_text())
    assert [item["sample_id"] for item in payload] == ["sample-1", "sample-2"]


def test_latency_artifact_attaches_reliability_samples_with_unavailable_reason() -> None:
    latency_sample = LatencySample(
        sample_id="sample-1",
        condition_id="baseline",
        warmup=False,
        timestamp_source="event_monotonic",
        stages=LatencyStageDurations(total_ms=750.0),
    )
    reliability_sample = ReliabilitySample(
        sample_id="sample-1",
        condition_id="baseline",
        mode="latency",
        informational=True,
        eligible=False,
        signals=ReliabilitySignals(unavailable_reason="queue_depth_unavailable"),
    )

    artifact = build_latency_artifact(
        mode=LatencyMode.SMOKE,
        samples=[latency_sample],
        reliability_samples=[reliability_sample],
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert artifact["reliability_samples"][0]["sample_id"] == "sample-1"
    assert (
        artifact["reliability_samples"][0]["signals"]["unavailable_reason"]
        == "queue_depth_unavailable"
    )


def test_latency_failure_classification_handles_provider_failures() -> None:
    assert classify_latency_failure("invalid_api_key") == "provider_auth"
    assert classify_latency_failure("quota exceeded") == "provider_rate_limit"
    assert classify_latency_failure("request timed out") == "provider_timeout"
    assert classify_latency_failure("baseline p50 exceeded") == "easycat_latency_regression"


def test_latency_runner_writes_report_and_smoke_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)

    def fake_command_runner(command: list[str]) -> CommandResult:
        samples_path = Path(os.environ["EASYCAT_LATENCY_SAMPLES_PATH"])
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
    def fake_command_runner(command: list[str]) -> CommandResult:
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
        Path(os.environ["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(json.dumps([sample.to_dict()]))
        reliability_path = Path(os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"])
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
    def fake_command_runner(command: list[str]) -> CommandResult:
        return CommandResult(exit_code=1, stdout="", stderr="invalid_api_key")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    report = json.loads(result.report_path.read_text())
    assert report["latency"]["samples"][0]["missing_stage_reason"] == "invalid_api_key"
    assert report["latency"]["samples"][0]["failure_class"] == "provider_auth"
    assert report["failures"][0]["failure_class"] == "provider_auth"


def test_latency_runner_reports_malformed_samples_without_crashing(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str]) -> CommandResult:
        samples_path = Path(os.environ["EASYCAT_LATENCY_SAMPLES_PATH"])
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
    assert report["tool_exit_codes"] == {"latency_samples": 1, "pytest": 0}
    assert report["failures"][0]["name"] == "latency.samples"
    assert (result.run_dir / "latency" / "sweep.json").exists()
    assert (tmp_path / "latency" / "sweep-latest.json").exists()


def test_latency_runner_can_require_samples_for_release_gates(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str]) -> CommandResult:
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


def test_latency_runner_reports_malformed_reliability_samples_without_crashing(
    tmp_path: Path,
) -> None:
    def fake_command_runner(command: list[str]) -> CommandResult:
        sample = LatencySample(
            sample_id="sample-1",
            condition_id="baseline",
            warmup=False,
            timestamp_source="event_monotonic",
            stages=LatencyStageDurations(total_ms=750.0),
        )
        Path(os.environ["EASYCAT_LATENCY_SAMPLES_PATH"]).write_text(json.dumps([sample.to_dict()]))
        Path(os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"]).write_text("{not-json")
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
    assert report["latency"]["samples"][0]["sample_id"] == "sample-1"


def test_validate_latency_cli_runs_smoke_and_writes_report(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "latency.json"
    called: dict[str, object] = {}

    def fake_run_latency_validation(mode: LatencyMode | str, **kwargs) -> ValidationRunResult:  # noqa: ANN003
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
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
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
    assert called["require_samples"] is False


def test_validate_latency_cli_can_require_samples(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: dict[str, object] = {}

    def fake_run_latency_validation(mode: LatencyMode | str, **kwargs) -> ValidationRunResult:  # noqa: ANN003
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
    assert "choose only one of --smoke or --sweep" in result.stdout


def test_validate_latency_cli_json_uses_standard_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_latency_validation(mode: LatencyMode | str, **kwargs) -> ValidationRunResult:  # noqa: ANN003
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
    assert payload["command"] == "validate latency sweep"
    assert payload["validation"]["latency"]["mode"] == "sweep"


def _latency_artifact_for_comparison(
    *,
    total_ms: float,
    count: int = 3,
    condition_id: str = "baseline",
    provider: dict[str, str] | None = None,
    model: dict[str, str] | None = None,
    transport: dict[str, str] | None = None,
    debug: dict[str, str] | None = None,
) -> dict[str, object]:
    samples = [
        LatencySample(
            sample_id=f"{condition_id}-{index}",
            condition_id=condition_id,
            warmup=False,
            timestamp_source="time.monotonic",
            provider=provider or {"stt": "openai-realtime", "region": "us-east-1"},
            model=model or {"llm": "gpt-5.4", "tts": "gpt-4o-mini-tts"},
            transport=transport or {"kind": "websocket"},
            debug=debug or {"journal": "off"},
            stages=LatencyStageDurations(total_ms=total_ms),
        )
        for index in range(count)
    ]
    return build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
        baseline={
            "comparison": "baseline",
            "conditions": {condition_id: {"version": "2026-05-22"}},
        },
    )


def test_latency_baseline_comparison_passes_within_thresholds() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    current = _latency_artifact_for_comparison(total_ms=1040.0)

    comparison = compare_latency_baseline(
        current,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=100,
        ),
    )

    assert comparison["status"] == "pass"
    assert comparison["conditions"][0]["status"] == "pass"


def test_latency_baseline_comparison_requires_relative_and_absolute_regression() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    relative_only = _latency_artifact_for_comparison(total_ms=1210.0)
    absolute_only = _latency_artifact_for_comparison(total_ms=1150.0)

    relative_comparison = compare_latency_baseline(
        relative_only,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=300,
        ),
    )
    absolute_comparison = compare_latency_baseline(
        absolute_only,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=100,
        ),
    )

    assert relative_comparison["status"] == "pass"
    assert relative_comparison["conditions"][0]["regression"]["relative"] is True
    assert relative_comparison["conditions"][0]["regression"]["absolute"] is False
    assert absolute_comparison["status"] == "pass"
    assert absolute_comparison["conditions"][0]["regression"]["relative"] is False
    assert absolute_comparison["conditions"][0]["regression"]["absolute"] is True


def test_latency_baseline_comparison_fails_eligible_regression() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    current = _latency_artifact_for_comparison(total_ms=1300.0)

    comparison = compare_latency_baseline(
        current,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=200,
        ),
    )

    condition = comparison["conditions"][0]
    assert comparison["status"] == "fail"
    assert condition["status"] == "fail"
    assert condition["failure_class"] == "easycat_latency_regression"
    # Schema check: per-condition results carry the percentile keys.
    assert condition["percentile"] == "p95"
    assert condition["current_p95_ms"] == pytest.approx(1300.0)
    assert condition["baseline_p95_ms"] == pytest.approx(1000.0)
    assert condition["delta_ms"] == pytest.approx(300.0)
    assert "current_median_ms" not in condition
    assert "baseline_median_ms" not in condition


def test_latency_baseline_comparison_marks_low_sample_counts_informational() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0, count=2)
    current = _latency_artifact_for_comparison(total_ms=1500.0, count=2)

    comparison = compare_latency_baseline(
        current,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=200,
            min_samples=3,
        ),
    )

    assert comparison["status"] == "info"
    assert comparison["conditions"][0]["status"] == "info"
    assert comparison["conditions"][0]["reason"] == "ineligible_sample_count"


def test_latency_baseline_comparison_refuses_mismatched_conditions() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    current = _latency_artifact_for_comparison(
        total_ms=1000.0,
        provider={"stt": "openai-realtime", "region": "eu-west-1"},
    )

    comparison = compare_latency_baseline(current, baseline)

    assert comparison["status"] == "drift"
    assert comparison["conditions"][0]["status"] == "drift"
    assert comparison["conditions"][0]["failure_class"] == "provider_api_drift"
    assert comparison["conditions"][0]["refresh_required"] is True


def test_latency_baseline_comparison_refuses_mixed_signatures_in_one_condition() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    current = _latency_artifact_for_comparison(total_ms=1000.0)
    current["samples"][1]["provider"]["region"] = "eu-west-1"  # type: ignore[index]

    comparison = compare_latency_baseline(current, baseline)

    assert comparison["status"] == "drift"
    assert comparison["conditions"][0]["reason"] == "mixed_condition_signature"
    assert comparison["conditions"][0]["refresh_required"] is True


def test_latency_baseline_comparison_refuses_mixed_baseline_signatures() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    baseline["samples"][1]["provider"]["region"] = "eu-west-1"  # type: ignore[index]
    current = _latency_artifact_for_comparison(total_ms=1000.0)

    comparison = compare_latency_baseline(current, baseline)

    assert comparison["status"] == "drift"
    assert comparison["conditions"][0]["reason"] == "mixed_condition_signature"


def test_latency_baseline_comparison_refuses_unversioned_condition_baseline() -> None:
    baseline = _latency_artifact_for_comparison(total_ms=1000.0)
    baseline["baseline"] = {"comparison": "baseline"}
    current = _latency_artifact_for_comparison(total_ms=1000.0)

    comparison = compare_latency_baseline(current, baseline)

    assert comparison["status"] == "drift"
    assert comparison["conditions"][0]["reason"] == "baseline_version_missing"
    assert comparison["conditions"][0]["refresh_required"] is True


def test_latency_runner_fails_when_budget_violated(tmp_path: Path) -> None:
    """Pytest passes but per-stage budgets blow out -> exit 1 with budget failure."""

    def fake_command_runner(command: list[str]) -> CommandResult:
        samples_path = Path(os.environ["EASYCAT_LATENCY_SAMPLES_PATH"])
        # Ten non-warmup samples with values comfortably above every stage in
        # DEFAULT_BUDGETS (total p95 8000 ms, tts_ttfb p95 1500 ms, llm_ttft p95
        # 2500 ms).
        samples = [
            LatencySample(
                sample_id=f"sample-{index}",
                condition_id="baseline",
                warmup=False,
                timestamp_source="event_monotonic",
                stages=LatencyStageDurations(
                    total_ms=12000.0,
                    stt_ms=200.0,
                    tts_ttfb_ms=2500.0,
                    llm_ttft_ms=4000.0,
                ),
            ).to_dict()
            for index in range(10)
        ]
        samples_path.write_text(json.dumps(samples))
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
    assert report["tool_exit_codes"]["pytest"] == 0
    assert report["tool_exit_codes"]["latency_budget"] == 1
    budget_failures = [
        failure for failure in report["failures"] if failure["name"] == "latency.budget"
    ]
    assert budget_failures, "expected a latency.budget failure entry"
    assert budget_failures[0]["failure_class"] == "latency_budget"
    violations = budget_failures[0]["details"]["violations"]
    assert violations, "expected at least one violation in failure details"
    stages = {entry["stage"] for entry in violations}
    assert "total_ms" in stages


def test_latency_runner_passes_when_budgets_satisfied(tmp_path: Path) -> None:
    """When all latency budgets are met, no budget failure is appended."""

    def fake_command_runner(command: list[str]) -> CommandResult:
        samples_path = Path(os.environ["EASYCAT_LATENCY_SAMPLES_PATH"])
        samples = [
            LatencySample(
                sample_id=f"sample-{index}",
                condition_id="baseline",
                warmup=False,
                timestamp_source="event_monotonic",
                stages=LatencyStageDurations(
                    total_ms=400.0,
                    stt_ms=50.0,
                    tts_ttfb_ms=100.0,
                    llm_ttft_ms=300.0,
                ),
            ).to_dict()
            for index in range(10)
        ]
        samples_path.write_text(json.dumps(samples))
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert report["status"] == "pass"
    assert "latency_budget" not in report["tool_exit_codes"]
    assert not [failure for failure in report["failures"] if failure["name"] == "latency.budget"]
