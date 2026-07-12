from __future__ import annotations

import sys
from pathlib import Path

import pytest

from perf.bench_framework_latency import (
    LOCK_EXCLUDE_NEWER,
    PINS,
    _lock_metadata,
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
    assert "--no-config" in livekit.command
    assert "--no-config" in pipecat.command
    assert livekit.command[livekit.command.index("--exclude-newer") + 1] == LOCK_EXCLUDE_NEWER
    assert pipecat.command[pipecat.command.index("--exclude-newer") + 1] == LOCK_EXCLUDE_NEWER
    assert "--isolated" in livekit.command
    assert "--locked" in livekit.command
    assert "--locked" in pipecat.command
    assert livekit.command[livekit.command.index("--python") + 1] == sys.executable
    assert pipecat.command[pipecat.command.index("--python") + 1] == sys.executable
    assert PINS == {
        "livekit": ("livekit-agents==1.6.4",),
        "pipecat": ("pipecat-ai==1.0.0", "websockets==15.0.1"),
    }


def test_competitor_lock_metadata_is_content_addressed() -> None:
    metadata = _lock_metadata()

    assert set(metadata) == {"livekit", "pipecat"}
    for lock in metadata.values():
        assert lock["path"].endswith("uv.lock")
        assert len(lock["sha256"]) == 64


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
            "framework": "easycat",
            "text": "Hello there.",
            "audio_bytes": 1,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 0.5,
            "framework": "easycat",
            "text": "wrong",
            "audio_bytes": 1,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 0.5,
            "framework": "easycat",
            "text": "Hello there.",
            "audio_bytes": 0,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 2.0,
            "framework": "easycat",
            "text": "Hello there.",
            "audio_bytes": 1,
        },
    ],
)
def test_correctness_gate_rejects_invalid_samples(sample: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _validate_sample(sample)


def test_correctness_gate_requires_easycat_voice_transition() -> None:
    sample = {
        "latency_ms": 1.0,
        "provider_elapsed_ms": 0.5,
        "framework": "easycat",
        "text": "Hello there.",
        "audio_bytes": 320,
        "agent_request_started_in_timed_path": False,
    }

    with pytest.raises(ValueError, match="voice-turn agent request transition"):
        _validate_sample(sample)

    sample["agent_request_started_in_timed_path"] = True
    _validate_sample(sample)


def test_easycat_worker_smoke_includes_public_voice_transition() -> None:
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
    assert (
        result["methodology"]["correctness_gate"]
        == "exact_framework_tts_text_nonempty_audio_and_easycat_voice_transition"
    )
