#!/usr/bin/env python3
"""Benchmark the AgentRunner timeout guard's scheduler overhead.

The serial baseline uses ``asyncio.wait_for``, which creates a child task for
each awaited agent event. The current path uses ``asyncio.timeout`` through
AgentRunner's private guard and keeps execution in the caller task.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from easycat.integrations.agents._agent_runner import _await_with_timeout


async def _immediate() -> None:
    return None


async def _wait_for_guard(awaitable: Awaitable[None]) -> None:
    await asyncio.wait_for(awaitable, timeout=30.0)


async def _current_task_guard(awaitable: Awaitable[None]) -> None:
    await _await_with_timeout(awaitable, timeout=30.0)


async def _measure(
    guard: Callable[[Awaitable[None]], Awaitable[None]], iterations: int
) -> list[float]:
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        await guard(_immediate())
        samples.append((time.perf_counter_ns() - started) / 1_000.0)
    return samples


async def compare(*, iterations: int = 1_000) -> dict[str, Any]:
    """Compare the former child-task guard with the current-task guard."""
    if iterations < 1:
        raise ValueError("iterations must be positive")

    await _measure(_wait_for_guard, 10)
    await _measure(_current_task_guard, 10)
    wait_for = await _measure(_wait_for_guard, iterations)
    current_task = await _measure(_current_task_guard, iterations)
    wait_for_p50 = statistics.median(wait_for)
    current_task_p50 = statistics.median(current_task)
    return {
        "schema_version": 1,
        "iterations": iterations,
        "warmup_runs_per_mode": 10,
        "unit": "microseconds",
        "wait_for": {"samples_us": wait_for, "p50_us": wait_for_p50},
        "current_task": {"samples_us": current_task, "p50_us": current_task_p50},
        "saved_p50_us": wait_for_p50 - current_task_p50,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(compare(iterations=args.iterations))
    except ValueError as exc:
        parser.error(str(exc))

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
