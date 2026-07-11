from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_benchmark() -> ModuleType:
    path = Path(__file__).parents[2] / "perf" / "bench_endpoint_latency.py"
    spec = importlib.util.spec_from_file_location("bench_endpoint_latency", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compare_reports_p50_endpoint_reduction() -> None:
    benchmark = _load_benchmark()
    result = benchmark.compare(
        [499.0, 500.0, 501.0],
        [249.0, 250.0, 251.0],
    )

    assert result["p50_saved_ms"] == 250.0
    assert result["p50_reduction_percent"] == 50.0


def test_summarize_rejects_empty_samples() -> None:
    benchmark = _load_benchmark()
    with pytest.raises(ValueError, match="must not be empty"):
        benchmark.summarize([])
