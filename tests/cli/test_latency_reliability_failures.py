from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from easycat.validation.latency import (
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
    append_reliability_sample,
    build_latency_artifact,
    classify_latency_failure,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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
