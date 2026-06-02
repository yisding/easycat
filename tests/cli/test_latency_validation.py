from __future__ import annotations

import json
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
from easycat.validation.runner import (
    LATENCY_SYNTHETIC_FAILURE_SAMPLE,
    LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY,
    CommandResult,
    ValidationRunResult,
    run_latency_validation,
)


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
    # The summary block no longer duplicates per-percentile numbers; the
    # `percentiles` block is the single source of truth (see _summarize_totals).
    assert artifact["summary"]["baseline"]["median_ms"] == 750.0
    assert "p50_ms" not in artifact["summary"]["baseline"]
    assert artifact["percentiles"]["overall"]["total_ms"]["count"] == 1
    # A single low-sample SMOKE run must never enforce tail budgets, so one
    # slow probe can't turn the default invocation into a hard fail.
    assert artifact["budget_violations"] == []


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


def test_latency_and_live_failure_classification_share_one_taxonomy() -> None:
    """Both classifiers must derive from the same canonical FailureCategory so
    auth/quota/timeout/drift tokens can never silently disagree between paths."""
    from easycat.validation.latency import FailureCategory, classify_failure_category
    from easycat.validation.runner import classify_live_failure

    cases = {
        "invalid_api_key": FailureCategory.AUTH,
        "429 rate limit hit": FailureCategory.QUOTA,
        "request timed out": FailureCategory.TIMEOUT,
        "schema drift detected": FailureCategory.DRIFT,
        "connection reset": FailureCategory.NETWORK,
    }
    for message, category in cases.items():
        assert classify_failure_category(message) is category
        # Each path emits its own (back-compatible) vocabulary, but both are
        # driven by the single category function above.
        assert isinstance(classify_latency_failure(message), str)
        assert isinstance(classify_live_failure(message), str)

    assert classify_live_failure("invalid_api_key") == "auth_or_quota"
    assert classify_live_failure("429 rate limit hit") == "provider_quota"
    assert classify_live_failure("schema drift detected") == "provider_drift"


def test_failure_classification_precedence_pins_cross_category_messages() -> None:
    """Pin the deliberate precedence for messages that match two categories.

    These are the cross-category conflicts the unified token table must resolve
    intentionally: QUOTA wins over AUTH (a 429 is the actionable signal even
    with an auth word), and DRIFT wins over NETWORK so schema-drift detection is
    never masked by an incidental network word.
    """
    from easycat.validation.latency import FailureCategory, classify_failure_category
    from easycat.validation.runner import classify_live_failure

    # QUOTA before AUTH: "429 unauthorized" carries both a quota token (429) and
    # an auth token (unauthorized); the quota signal must win.
    assert classify_failure_category("429 unauthorized") is FailureCategory.QUOTA
    assert classify_live_failure("429 unauthorized") == "provider_quota"
    assert classify_latency_failure("429 unauthorized") == "provider_rate_limit"

    # DRIFT before NETWORK: "schema mismatch on connection close" carries both a
    # drift token (schema) and a network token (connection); drift must win so
    # live validation still reports 'provider_drift'.
    assert (
        classify_failure_category("schema mismatch on connection close") is FailureCategory.DRIFT
    )
    assert classify_live_failure("schema mismatch on connection close") == "provider_drift"
    assert (
        classify_latency_failure("schema mismatch on connection close")
        == "easycat_latency_regression"
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

    assert result.exit_code == 1
    assert secret not in serialized_report
    assert secret not in serialized_latency
    assert secret not in serialized_latest_latency
    assert secret not in (result.run_dir / "stdout.log").read_text()
    assert secret not in (result.run_dir / "stderr.log").read_text()
    assert secret not in (result.run_dir / "junit.xml").read_text()
    assert "[REDACTED_SECRET]" in serialized_report
    assert "[REDACTED_SECRET]" in serialized_latency


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
    assert (result.run_dir / "latency" / "sweep.json").exists()
    assert (tmp_path / "latency" / "sweep-latest.json").exists()


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

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
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

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
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


def test_latency_runner_records_separate_pytest_and_budget_checks(tmp_path: Path) -> None:
    """When pytest passes but a budget violates, the pytest check stays `pass`
    and a distinct `latency.budget` check captures the failure."""

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
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
    checks_by_name = {check["name"]: check for check in report["checks"]}
    assert "pytest.latency.sweep" in checks_by_name, (
        "pytest check must remain its own ValidationCheck"
    )
    assert checks_by_name["pytest.latency.sweep"]["status"] == "pass", (
        "pytest exited 0 so its check must stay green; only the budget check fails"
    )
    assert "latency.budget" in checks_by_name, (
        "budget evaluation in sweep mode must record its own ValidationCheck"
    )
    assert checks_by_name["latency.budget"]["status"] == "fail"
    assert checks_by_name["latency.budget"]["details"]["violations"], (
        "budget check details must carry the violations list for the report"
    )


def test_latency_runner_smoke_mode_omits_budget_check(tmp_path: Path) -> None:
    """Smoke mode skips budget evaluation (single slow sample tolerated);
    no `latency.budget` check should be recorded."""

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
        samples = [
            LatencySample(
                sample_id="smoke-slow",
                condition_id="baseline",
                warmup=False,
                timestamp_source="event_monotonic",
                stages=LatencyStageDurations(
                    total_ms=20_000.0,
                    stt_ms=200.0,
                    tts_ttfb_ms=5_000.0,
                    llm_ttft_ms=9_000.0,
                ),
            ).to_dict(),
        ]
        samples_path.write_text(json.dumps(samples))
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_latency_validation(
        LatencyMode.SMOKE,
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert report["status"] == "pass"
    assert "latency_budget" not in report["tool_exit_codes"]
    assert "latency.budget" not in {check["name"] for check in report["checks"]}


def _baseline_aware_command_runner(total_ms: float):
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
        samples = [
            LatencySample(
                sample_id=f"baseline-{index}",
                condition_id="baseline",
                warmup=False,
                timestamp_source="time.monotonic",
                provider={"stt": "openai-realtime", "region": "us-east-1"},
                model={"llm": "gpt-5.4", "tts": "gpt-4o-mini-tts"},
                transport={"kind": "websocket"},
                debug={"journal": "off"},
                stages=LatencyStageDurations(total_ms=total_ms),
            ).to_dict()
            for index in range(3)
        ]
        samples_path.write_text(json.dumps(samples))
        return CommandResult(exit_code=0, stdout="", stderr="")

    return fake_command_runner


def test_latency_runner_flags_regression_against_supplied_baseline(tmp_path: Path) -> None:
    """A stored baseline drives regression detection through the runner."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(_latency_artifact_for_comparison(total_ms=1000.0)))

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=_baseline_aware_command_runner(1500.0),
        baseline_path=baseline_path,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["status"] == "fail"
    assert report["tool_exit_codes"]["latency_baseline_regression"] == 1
    baseline_failures = [
        failure for failure in report["failures"] if failure["name"] == "latency.baseline"
    ]
    assert baseline_failures
    assert baseline_failures[0]["failure_class"] == "easycat_latency_regression"
    assert report["latency"]["baseline"]["kind"] == "latency_baseline_comparison"
    assert report["latency"]["baseline"]["status"] == "fail"
    checks_by_name = {check["name"]: check for check in report["checks"]}
    assert checks_by_name["latency.baseline"]["status"] == "fail"


def test_latency_runner_passes_baseline_within_thresholds(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(_latency_artifact_for_comparison(total_ms=1000.0)))

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=_baseline_aware_command_runner(1010.0),
        baseline_path=baseline_path,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert report["status"] == "pass"
    assert report["latency"]["baseline"]["status"] == "pass"
    assert "latency_baseline_regression" not in report["tool_exit_codes"]


def test_latency_runner_reports_unreadable_baseline(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text("{not-json")

    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=_baseline_aware_command_runner(1000.0),
        baseline_path=baseline_path,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert report["tool_exit_codes"]["latency_baseline"] == 1
    baseline_failures = [
        failure for failure in report["failures"] if failure["name"] == "latency.baseline"
    ]
    assert baseline_failures
    assert baseline_failures[0]["failure_class"] == "latency_baseline_error"


def test_latency_runner_without_baseline_leaves_not_configured(tmp_path: Path) -> None:
    result = run_latency_validation(
        LatencyMode.SWEEP,
        artifacts_dir=tmp_path,
        command_runner=_baseline_aware_command_runner(1000.0),
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert report["latency"]["baseline"]["comparison"] == "not_configured"
    assert "latency.baseline" not in {check["name"] for check in report["checks"]}
