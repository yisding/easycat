from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from easycat.validation.latency import (
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    build_latency_artifact,
    latency_pytest_args,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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
