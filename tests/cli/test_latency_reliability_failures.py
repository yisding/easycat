from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from easycat.validation.latency import (
    DEFAULT_RELIABILITY_BUDGETS,
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
    append_reliability_sample,
    build_latency_artifact,
    build_reliability_artifact,
    classify_latency_failure,
    evaluate_reliability_budgets,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_validation_tasks_v24_current_state_tracks_reliability_sampling_contract() -> None:
    from easycat.validation import capture_reliability_sample

    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V2.4 Add Reliability Sampling To Latency/Stress Runs", 1)[1].split(
        "## V3: Provider And Protocol Contracts", 1
    )[0]
    runner_source = (REPO_ROOT / "src/easycat/validation/runner.py").read_text(encoding="utf-8")
    stress_source = (REPO_ROOT / "tests/e2e/test_plan_2_sustained_stress.py").read_text(
        encoding="utf-8"
    )
    latency_sample = LatencySample(
        sample_id="latency-1",
        condition_id="baseline",
        warmup=False,
        timestamp_source="event_monotonic",
        stages=LatencyStageDurations(total_ms=750.0),
    )
    reliability_sample = ReliabilitySample(
        sample_id="sample-1",
        condition_id="baseline",
        mode="stress",
        informational=False,
        eligible=True,
        signals=ReliabilitySignals(
            event_loop_lag_ms=12.5,
            queue_depth=3,
            dropped_frames=0,
            journal_degraded=False,
            active_sessions=2,
            memory_growth_kib=1024,
        ),
    )
    latency_artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=[latency_sample],
        reliability_samples=[reliability_sample],
    )
    reliability_artifact = build_reliability_artifact(samples=[reliability_sample])
    smoke_sample = capture_reliability_sample(
        sample_id="smoke-1",
        condition_id="baseline",
        mode=LatencyMode.SMOKE,
        event_loop_lag_ms=12.5,
    )
    sweep_sample = capture_reliability_sample(
        sample_id="sweep-1",
        condition_id="baseline",
        mode=LatencyMode.SWEEP,
        event_loop_lag_ms=12.5,
    )
    unavailable_sample = capture_reliability_sample(
        sample_id="empty-1",
        condition_id="baseline",
        mode="stress",
    )
    ineligible_bad_sample = ReliabilitySample(
        sample_id="bad-informational",
        condition_id="baseline",
        mode="smoke",
        informational=True,
        eligible=False,
        signals=ReliabilitySignals(event_loop_lag_ms=10_000.0, dropped_frames=99),
    )
    eligible_bad_sample = ReliabilitySample(
        sample_id="bad-eligible",
        condition_id="baseline",
        mode="stress",
        informational=False,
        eligible=True,
        signals=ReliabilitySignals(event_loop_lag_ms=10_000.0, dropped_frames=99),
    )
    violations = evaluate_reliability_budgets(
        [eligible_bad_sample],
        DEFAULT_RELIABILITY_BUDGETS,
    )

    assert smoke_sample.informational is True
    assert smoke_sample.eligible is False
    assert sweep_sample.informational is False
    assert sweep_sample.eligible is True
    assert unavailable_sample.signals.unavailable_reason
    assert latency_artifact["reliability_samples"][0]["sample_id"] == "sample-1"
    assert reliability_artifact["kind"] == "reliability_validation"
    assert (
        evaluate_reliability_budgets(
            [ineligible_bad_sample],
            DEFAULT_RELIABILITY_BUDGETS,
        )
        == []
    )
    assert {violation.scope for violation in violations} == {
        "overall",
        "condition:baseline",
    }
    assert {violation.signal for violation in violations} >= {
        "event_loop_lag_ms",
        "dropped_frames",
    }

    assert "Current verified state:" in section
    for field in reliability_sample.to_dict():
        assert f"`{field}`" in section
    for field in reliability_sample.signals.to_dict():
        assert f"`{field}`" in section
    for field in reliability_artifact:
        assert f"`{field}`" in section
    for token in (
        "ReliabilitySample.to_dict()",
        "ReliabilitySignals.to_dict()",
        "capture_reliability_sample(...)",
        "build_latency_artifact(...)",
        "build_reliability_artifact(...)",
        "evaluate_reliability_budgets(...)",
        "EASYCAT_RELIABILITY_SAMPLES_PATH",
        "runs/<run_id>/latency/reliability.json",
        "runs/<run_id>/reliability/samples.json",
        "reliability_validation",
        "reliability_samples",
        "reliability.samples",
        "reliability.budget",
        "reliability_budget",
        "EventLoopLagSampler",
        "event_loop_lag_ms",
        "memory_growth_kib",
        "dropped_frames",
        "journal_degraded",
        "condition:<condition_id>",
    ):
        assert f"`{token}`" in section
    assert "informational and ineligible" in section
    assert "non-smoke modes such as `sweep` and" in section
    assert "informational/ineligible" in section
    assert 'run_dir / "latency" / "reliability.json"' in runner_source
    assert 'run_dir / "reliability" / "samples.json"' in runner_source
    assert "EventLoopLagSampler" in stress_source
    assert "informational=True" in stress_source
    assert "eligible=False" in stress_source


def test_validation_tasks_v52_current_state_tracks_stress_saturation_signals() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V5.2 Add Stress Saturation Signals", 1)[1].split(
        "### V5.3 Add Release Validation Workflow", 1
    )[0]
    runner_source = (REPO_ROOT / "src/easycat/validation/runner.py").read_text(encoding="utf-8")
    stress_source = (REPO_ROOT / "tests/e2e/test_plan_2_sustained_stress.py").read_text(
        encoding="utf-8"
    )
    sampler_guard_source = (
        REPO_ROOT / "tests/validation/test_stress_uses_public_sampler.py"
    ).read_text(encoding="utf-8")
    validate_tests_source = (REPO_ROOT / "tests/cli/test_validate_runner.py").read_text(
        encoding="utf-8"
    )

    assert "Current verified state:" in section
    assert '"stress": "stress and not integration_live and not flaky"' in runner_source
    assert "EASYCAT_RELIABILITY_SAMPLES_PATH" in runner_source
    assert "test_validation_runner_embeds_reliability_samples_for_stress_slices" in (
        validate_tests_source
    )
    assert "test_stress_test_imports_public_event_loop_lag_sampler" in sampler_guard_source
    assert "from easycat.validation.reliability import EventLoopLagSampler" in stress_source
    assert stress_source.count("@pytest.mark.stress") >= 4
    for token in (
        "ReliabilitySample",
        "ReliabilitySignals",
        "append_reliability_sample",
        "informational=True",
        "eligible=False",
        "fifty_turns_single_session_scripted",
        "concurrent_sessions_journal_isolation",
        "ten_turns_live_openai",
        "event_loop_lag_ms",
        "queue_depth",
        "dropped_frames",
        "journal_degraded",
        "active_sessions",
        "memory_growth_kib",
    ):
        assert token in stress_source
        assert f"`{token}`" in section
    for token in (
        "easycat validate stress",
        "stress and not integration_live and not flaky",
        "ReliabilitySample",
        "EASYCAT_RELIABILITY_SAMPLES_PATH",
        "ReliabilitySignals",
        "event_loop_lag_ms",
        "queue_depth",
        "dropped_frames",
        "journal_degraded",
        "active_sessions",
        "memory_growth_kib",
        "informational=True",
        "eligible=False",
        "capture_reliability_sample(...)",
        "evaluate_reliability_budgets(...)",
        "reliability.budget",
        "reliability",
        "tests/validation/test_stress_uses_public_sampler.py",
    ):
        assert f"`{token}`" in section


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
