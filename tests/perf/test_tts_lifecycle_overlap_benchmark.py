from __future__ import annotations

import asyncio

import pytest

import perf.bench_tts_lifecycle_overlap as benchmark
from perf.bench_tts_lifecycle_overlap import modeled_comparison


def test_tts_lifecycle_overlap_model_replaces_sum_with_maximum() -> None:
    assert modeled_comparison(handler_ms=80.0, provider_ms=120.0) == {
        "serial_ms": 200.0,
        "overlapped_ms": 120.0,
        "saved_ms": 80.0,
    }


@pytest.mark.parametrize(
    ("handler_ms", "provider_ms"),
    [(-1.0, 10.0), (10.0, -1.0)],
)
def test_tts_lifecycle_overlap_model_rejects_negative_delays(
    handler_ms: float,
    provider_ms: float,
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        modeled_comparison(handler_ms=handler_ms, provider_ms=provider_ms)


@pytest.mark.asyncio
async def test_overlap_benchmark_drains_synthesis_when_lifecycle_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_tasks: list[asyncio.Task[object]] = []
    real_create_task = asyncio.create_task

    def _capture_task(coro: object) -> asyncio.Task[object]:
        task = real_create_task(coro)  # type: ignore[arg-type]
        created_tasks.append(task)
        return task

    async def _fail_lifecycle(_self: object) -> None:
        raise RuntimeError("lifecycle failed")

    monkeypatch.setattr(benchmark.asyncio, "create_task", _capture_task)
    monkeypatch.setattr(benchmark.TurnManager, "bot_started_speaking", _fail_lifecycle)

    with pytest.raises(RuntimeError, match="lifecycle failed"):
        await benchmark._measure_once(handler_s=0.0, provider_s=10.0, overlap=True)

    assert len(created_tasks) == 1
    assert created_tasks[0].cancelled()
