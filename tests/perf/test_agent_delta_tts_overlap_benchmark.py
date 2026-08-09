from __future__ import annotations

import pytest

from perf.bench_agent_delta_tts_overlap import compare


@pytest.mark.parametrize(
    ("handler_ms", "iterations", "message"),
    [(-1.0, 1, "handler_ms"), (1.0, 0, "iterations")],
)
@pytest.mark.asyncio
async def test_agent_delta_overlap_benchmark_rejects_invalid_inputs(
    handler_ms: float,
    iterations: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await compare(handler_ms=handler_ms, iterations=iterations)


@pytest.mark.latency
@pytest.mark.asyncio
async def test_agent_delta_overlap_benchmark_hides_async_handler_delay() -> None:
    result = await compare(handler_ms=50.0, iterations=4)

    assert result["warmup_runs_per_mode"] == 1
    assert result["serial"]["p50_ms"] >= 40.0
    assert result["overlapped"]["p50_ms"] < result["serial"]["p50_ms"]
    assert result["saved_p50_ms"] >= 25.0
