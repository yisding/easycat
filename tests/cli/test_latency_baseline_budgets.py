from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easycat.validation.latency import (
    LatencyComparisonThresholds,
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    compare_latency_baseline,
)
from easycat.validation.runner import CommandResult, run_latency_validation

from ._latency_validation_helpers import (
    _baseline_aware_command_runner,
    _latency_artifact_for_comparison,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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
