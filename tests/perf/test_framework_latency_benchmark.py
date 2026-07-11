from __future__ import annotations

import sys
from pathlib import Path

import pytest

from perf.bench_framework_latency import (
    PINS,
    _validate_sample,
    percentile,
    run_benchmark,
    worker_specs,
)


def test_worker_specs_pin_competitors_in_isolated_environments(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    specs = worker_specs(worker_path=worker)

    easycat, livekit, pipecat = specs
    assert easycat.command == (sys.executable, str(worker), "--framework", "easycat")
    assert "--isolated" in livekit.command
    assert "--no-project" in livekit.command
    assert PINS["livekit"][0] in livekit.command
    assert all(pin in pipecat.command for pin in PINS["pipecat"])


def test_percentile_interpolates_and_validates_inputs() -> None:
    assert percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert percentile([1.0, 3.0], 0.5) == 2.0
    with pytest.raises(ValueError, match="samples"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="quantile"):
        percentile([1.0], 1.1)


@pytest.mark.parametrize(
    "sample",
    [
        {
            "latency_ms": -1.0,
            "provider_elapsed_ms": 0.0,
            "text": "Hello there.",
            "audio_bytes": 1,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 0.5,
            "text": "wrong",
            "audio_bytes": 1,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 0.5,
            "text": "Hello there.",
            "audio_bytes": 0,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 2.0,
            "text": "Hello there.",
            "audio_bytes": 1,
        },
    ],
)
def test_correctness_gate_rejects_invalid_samples(sample: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _validate_sample(sample)


def test_easycat_worker_smoke() -> None:
    result = run_benchmark(
        iterations=2,
        warmups=1,
        llm_delay_ms=1.0,
        tts_delay_ms=1.0,
        frameworks=("easycat",),
    )

    assert result["metric"] == "accepted_transcript_to_first_audio_ms"
    easycat = result["results"]["easycat"]
    assert len(easycat["latency_samples_ms"]) == 2
    assert len(easycat["framework_overhead_samples_ms"]) == 2
    assert easycat["latency_p50_ms"] > 2.0
