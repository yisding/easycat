"""The offline percentile path is retrofitted onto build_budget_report (CONS-7)."""

from __future__ import annotations

from datetime import UTC, datetime

from easycat.budgets import build_budget_report
from easycat.validation.latency import (
    DEFAULT_BUDGETS,
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    build_latency_artifact,
)


def _make_sample(
    *, sample_id: str, total_ms: float, tts_ttfb_ms: float, llm_ttft_ms: float
) -> LatencySample:
    return LatencySample(
        sample_id=sample_id,
        condition_id="default",
        warmup=False,
        timestamp_source="monotonic",
        stages=LatencyStageDurations(
            total_ms=total_ms,
            tts_ttfb_ms=tts_ttfb_ms,
            llm_ttft_ms=llm_ttft_ms,
        ),
    )


def test_build_latency_artifact_offline_path_uses_shared_report() -> None:
    samples = [
        _make_sample(sample_id=f"s-{i}", total_ms=12000.0, tts_ttfb_ms=2500.0, llm_ttft_ms=4000.0)
        for i in range(10)
    ]
    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
    )

    artifact_violations = artifact["budget_violations"]
    assert artifact_violations, "expected offline budget violations"
    # The legacy artifact shape is preserved for downstream consumers.
    for entry in artifact_violations:
        assert set(entry) == {"stage", "percentile", "observed_ms", "budget_ms", "scope"}

    # The same percentile block evaluated directly through the shared builder
    # yields the same set of violated stages, proving the offline path is the
    # shared evaluator and not a parallel one.
    report = build_budget_report(
        budgets=list(DEFAULT_BUDGETS),
        percentiles=artifact["percentiles"],
    )
    shared_stages = {v.stage for v in report.violations if v.kind == "latency"}
    artifact_stages = {entry["stage"] for entry in artifact_violations}
    assert shared_stages == artifact_stages
    assert "total_ms" in shared_stages
