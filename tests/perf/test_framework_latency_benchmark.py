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
    rank_by_latency,
    run_benchmark,
    worker_specs,
)


def test_ranking_uses_raw_latency_not_overlap_adjusted_diagnostic() -> None:
    results = {
        "easycat": {
            "latency_p50_ms": 21.0,
            "latency_p95_ms": 23.0,
            "framework_overhead_p50_ms": 1.0,
            "framework_overhead_p95_ms": 1.5,
        },
        "livekit": {
            "latency_p50_ms": 20.0,
            "latency_p95_ms": 22.0,
            "framework_overhead_p50_ms": 2.0,
            "framework_overhead_p95_ms": 2.5,
        },
    }

    ranking, fastest = rank_by_latency(results, ("easycat", "livekit"))

    assert ranking == ["livekit", "easycat"]
    assert fastest == {"p50": False, "p95": False, "all": False}


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


async def test_easycat_timed_span_covers_end_of_speech_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured EasyCat sample must run the voice end-of-speech transition.

    Guards the boundary the cross-framework comparison depends on: the span
    timed by ``_sample_easycat`` enters through ``end_turn()`` into
    ``TurnRunner.handle_end_of_speech`` (not a direct ``run_streaming_agent``
    call), and the AgentRequestStarted event-bus dispatch lands inside the
    timed span, before first audio.
    """
    from easycat.session._turn_runner import TurnRunner
    from perf.framework_latency_worker import _sample_easycat

    transcripts: list[str] = []
    original = TurnRunner.handle_end_of_speech

    async def _spy(self: TurnRunner, turn=None) -> None:  # type: ignore[no-untyped-def]
        transcripts.append(turn.transcript_text if turn is not None else "")
        await original(self, turn=turn)

    monkeypatch.setattr(TurnRunner, "handle_end_of_speech", _spy)

    sample = await _sample_easycat(0.0, 0.0)

    assert transcripts == ["Hello"]
    assert sample["agent_request_started_in_timed_path"] is True
    _validate_sample({"kind": "sample", "framework": "easycat", **sample})


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
    assert result["comparison_metric"] == "accepted_transcript_to_first_audio_ms"
    assert result["ranking_by_latency_p50"] == ["easycat"]
    assert "not used for ranking" in result["methodology"]["framework_overhead"]
