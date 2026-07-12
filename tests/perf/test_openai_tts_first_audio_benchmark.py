from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from perf.bench_openai_tts_first_audio import compare, main


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
        {"network_chunk_bytes": 480, "interval_ms": float("nan")},
        {"network_chunk_bytes": 480, "interval_ms": float("inf")},
    ):
        try:
            compare(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"compare accepted invalid inputs: {kwargs}")


def test_openai_tts_first_audio_benchmark_uses_exact_integer_ceiling() -> None:
    result = compare(network_chunk_bytes=10**400, interval_ms=1.0)

    assert result["legacy_fixed_chunk"]["network_chunks"] == 1
    assert result["low_latency_first_chunk"]["network_chunks"] == 1


def test_openai_tts_first_audio_benchmark_cli_writes_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output = tmp_path / "first-audio.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_openai_tts_first_audio.py",
            "--network-chunk-bytes",
            "240",
            "--interval-ms",
            "5",
            "--output",
            str(output),
        ],
    )

    main()

    stdout = capsys.readouterr().out
    assert output.read_text() == stdout
    payload = json.loads(stdout)
    assert payload["network_chunk_bytes"] == 240
    assert payload["network_interval_ms"] == 5.0
    assert payload["low_latency_first_chunk"]["network_chunks"] == 4


@pytest.mark.parametrize(
    "args",
    [
        ["--network-chunk-bytes", "0"],
        ["--interval-ms", "nan"],
        ["--interval-ms", "inf"],
    ],
)
def test_openai_tts_first_audio_benchmark_cli_rejects_invalid_values(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["bench_openai_tts_first_audio.py", *args])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "error:" in capsys.readouterr().err
