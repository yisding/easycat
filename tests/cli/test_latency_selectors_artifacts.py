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


def test_validation_tasks_v21_current_state_tracks_latency_markers_and_selectors() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V2.1 Mark And Factor Latency Tests", 1)[1].split(
        "### V2.2 Add Canonical Latency Sample JSON", 1
    )[0]
    benchmark = (REPO_ROOT / "tests/e2e/test_plan_7_latency_benchmark.py").read_text(
        encoding="utf-8"
    )
    expected_markers = {
        "integration_socket",
        "integration_live",
        "latency",
        "provider_openai",
        "slow",
        "surface_agent",
        "surface_stt",
        "surface_transport",
        "surface_tts",
    }

    assert "Current verified state:" in section
    for marker_name in expected_markers:
        assert f"`{marker_name}`" in section
        assert f"pytest.mark.{marker_name}" in benchmark
    for selector in (
        latency_pytest_args(LatencyMode.SMOKE)[0],
        latency_pytest_args(LatencyMode.SWEEP)[0],
    ):
        assert selector in section
    assert "EASYCAT_LATENCY_SAMPLES_PATH" in section
    assert "structured latency artifacts" in section
    assert "does not emit a stable validation artifact" not in section


def test_validation_tasks_v22_current_state_tracks_latency_artifact_contract() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V2.2 Add Canonical Latency Sample JSON", 1)[1].split(
        "### V2.3 Add Baseline Comparison Helper", 1
    )[0]
    runner_source = (REPO_ROOT / "src/easycat/validation/runner.py").read_text(encoding="utf-8")
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
        missing_stage_reason="tts_ttfb_missing",
        failure_class="provider_timeout",
    )
    artifact = build_latency_artifact(
        mode=LatencyMode.SMOKE,
        samples=[sample],
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert "Current verified state:" in section
    for field in sample.to_dict():
        assert f"`{field}`" in section
    for field in artifact:
        assert f"`{field}`" in section
    assert artifact["kind"] == "latency_validation"
    assert artifact["clock_source"] == "time.monotonic"
    assert "`latency_validation`" in section
    assert "`clock_source=time.monotonic`" in section
    assert 'run_dir / "latency" / "samples.json"' in runner_source
    assert 'run_dir / "latency" / f"{mode.value}.json"' in runner_source
    assert 'artifacts_root / "latency" / f"{mode.value}-latest.json"' in runner_source
    assert '"latency": ArtifactRef(kind="latency", path=str(latency_path))' in runner_source
    assert "`runs/<run_id>/latency/samples.json`" in section
    assert "`runs/<run_id>/latency/<mode>.json`" in section
    assert "`latency/<mode>-latest.json`" in section
    assert "sample-count eligibility is too low" in section
    assert "eligible" in section
    assert "summaries" in section
    assert "budget checks" in section
    assert "baseline comparison data" in section


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
