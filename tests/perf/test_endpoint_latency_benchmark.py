from __future__ import annotations

import asyncio
import importlib.util
import sys
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


def test_compare_rejects_zero_baseline() -> None:
    benchmark = _load_benchmark()
    with pytest.raises(ValueError, match="baseline p50 must be positive"):
        benchmark.compare([0.0], [0.0])


def test_run_rejects_zero_fixed_delay() -> None:
    benchmark = _load_benchmark()
    with pytest.raises(ValueError, match="full_ms must be positive"):
        asyncio.run(benchmark.run(samples=1, full_ms=0, punctuated_ms=0))


def test_run_interleaves_fixed_and_punctuated_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = _load_benchmark()
    sample_order: list[bool] = []

    async def fake_sample(*, punctuated: bool, full_ms: int, punctuated_ms: int) -> float:
        assert (full_ms, punctuated_ms) == (500, 200)
        sample_order.append(punctuated)
        return 200.0 if punctuated else 500.0

    monkeypatch.setattr(benchmark, "_sample", fake_sample)

    result = asyncio.run(benchmark.run(samples=3, full_ms=500, punctuated_ms=200))

    assert sample_order == [False, True, False, True, False, True]
    assert result["p50_saved_ms"] == 300.0


def test_cli_rejects_zero_fixed_delay(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setattr(sys, "argv", ["bench_endpoint_latency.py", "--fixed-ms", "0"])

    with pytest.raises(SystemExit, match="2"):
        benchmark.main()

    assert "fixed delay must be positive" in capsys.readouterr().err
