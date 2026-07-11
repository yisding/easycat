from __future__ import annotations

from perf.bench_openai_tts_first_audio import compare


def test_openai_tts_first_audio_benchmark_models_rechunking_win() -> None:
    result = compare(network_chunk_bytes=480, interval_ms=10.0)

    assert result["legacy_fixed_chunk"] == {
        "network_chunks": 10,
        "latency_ms": 100.0,
        "payload_bytes": 4800,
    }
    assert result["low_latency_first_chunk"] == {
        "network_chunks": 2,
        "latency_ms": 20.0,
        "payload_bytes": 960,
    }
    assert result["steady_state_chunk_bytes"] == 4800
    assert result["saved_ms"] == 80.0
    assert result["reduction_percent"] == 80.0


def test_openai_tts_first_audio_benchmark_validates_inputs() -> None:
    for kwargs in (
        {"network_chunk_bytes": 0, "interval_ms": 10.0},
        {"network_chunk_bytes": 480, "interval_ms": -1.0},
    ):
        try:
            compare(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"compare accepted invalid inputs: {kwargs}")
