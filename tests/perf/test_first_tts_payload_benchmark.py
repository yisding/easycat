from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_benchmark() -> ModuleType:
    path = Path(__file__).parents[2] / "perf" / "bench_first_tts_payload.py"
    spec = importlib.util.spec_from_file_location("bench_first_tts_payload", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bounded_first_payload_dispatches_run_on_text_earlier() -> None:
    benchmark = _load_benchmark()
    result = benchmark.compare(
        "This answer keeps streaming useful words without producing sentence punctuation yet",
        delta_ms=40.0,
    )

    assert result["bounded_first_payload"]["latency_ms"] < result["sentence_only"]["latency_ms"]
    assert result["saved_ms"] > 0
    assert result["reduction_percent"] > 0


def test_compare_rejects_empty_text() -> None:
    benchmark = _load_benchmark()
    with pytest.raises(ValueError, match="at least one"):
        benchmark.compare("   ", delta_ms=40.0)
