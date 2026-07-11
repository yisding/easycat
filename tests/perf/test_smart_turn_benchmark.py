from __future__ import annotations

import pytest

from perf.bench_smart_turn import compare_results, summarize_samples


def test_summarize_samples_reports_latency_distribution() -> None:
    summary = summarize_samples([1.0, 2.0, 3.0, 4.0, 10.0])

    assert summary == {
        "runs": 5,
        "p50_ms": 3.0,
        "p90_ms": 10.0,
        "p99_ms": 10.0,
        "mean_ms": 4.0,
        "min_ms": 1.0,
        "max_ms": 10.0,
    }


def test_summarize_samples_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize_samples([])


def test_compare_results_requires_identical_model_and_both_percentiles() -> None:
    easycat = {"model_sha256": "same", "p50_ms": 60.0, "p90_ms": 70.0}
    pipecat = {"model_sha256": "same", "p50_ms": 120.0, "p90_ms": 140.0}

    assert compare_results(easycat, pipecat) == {
        "models_identical": True,
        "easycat_faster": True,
        "easycat_p50_improvement_percent": 50.0,
        "easycat_p90_improvement_percent": 50.0,
    }

    pipecat["model_sha256"] = "different"
    comparison = compare_results(easycat, pipecat)
    assert comparison["models_identical"] is False
    assert comparison["easycat_faster"] is False
