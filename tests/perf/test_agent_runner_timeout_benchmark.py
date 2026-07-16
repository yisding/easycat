from __future__ import annotations

import pytest

from perf.bench_agent_runner_timeout import compare


@pytest.mark.asyncio
async def test_agent_runner_timeout_benchmark_rejects_invalid_iterations() -> None:
    with pytest.raises(ValueError, match="iterations"):
        await compare(iterations=0)


@pytest.mark.asyncio
async def test_agent_runner_timeout_benchmark_reports_both_guards() -> None:
    result = await compare(iterations=5)

    assert result["warmup_runs_per_mode"] == 10
    assert len(result["wait_for"]["samples_us"]) == 5
    assert len(result["current_task"]["samples_us"]) == 5
