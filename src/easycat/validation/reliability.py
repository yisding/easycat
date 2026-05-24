from __future__ import annotations

import asyncio

from easycat.validation.latency import (
    LatencyMode,
    ReliabilitySample,
    ReliabilitySignals,
)

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows fallback
    _resource = None  # type: ignore[assignment]

import sys


class EventLoopLagSampler:
    def __init__(self, *, interval_s: float = 0.02) -> None:
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._started = False
        self.max_lag_ms = 0.0

    async def start(self) -> None:
        self._running = True
        self._started = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> float | None:
        if not self._started:
            return None
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            await task
        return round(self.max_lag_ms, 3)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + self._interval_s
        while self._running:
            await asyncio.sleep(max(0.0, next_deadline - loop.time()))
            now = loop.time()
            self.max_lag_ms = max(self.max_lag_ms, (now - next_deadline) * 1000.0)
            next_deadline = now + self._interval_s


class MemoryGrowthSampler:
    def __init__(self) -> None:
        self._baseline_kib: int | None = None

    def start(self) -> None:
        self._baseline_kib = _current_rss_kib()

    def stop(self) -> int | None:
        if self._baseline_kib is None:
            return None
        current = _current_rss_kib()
        if current is None:
            return None
        return max(0, current - self._baseline_kib)


def _current_rss_kib() -> int | None:
    if _resource is None:
        return None
    rss = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    # macOS reports ru_maxrss in bytes; Linux reports KiB.
    if sys.platform == "darwin":
        return int(rss / 1024)
    return int(rss)


def capture_reliability_sample(
    *,
    mode: LatencyMode | str,
    sample_id: str,
    condition_id: str,
    event_loop_lag_ms: float | None = None,
    queue_depth: int | None = None,
    dropped_frames: int | None = None,
    journal_degraded: bool | None = None,
    active_sessions: int | None = None,
    memory_growth_kib: int | None = None,
) -> ReliabilitySample:
    mode_str = mode.value if isinstance(mode, LatencyMode) else str(mode)
    if mode_str == LatencyMode.SMOKE.value:
        informational = True
        eligible = False
    else:
        informational = False
        eligible = True

    all_signals = (
        event_loop_lag_ms,
        queue_depth,
        dropped_frames,
        journal_degraded,
        active_sessions,
        memory_growth_kib,
    )
    unavailable_reason: str | None = None
    if all(value is None for value in all_signals):
        unavailable_reason = "no reliability probes returned a value"

    return ReliabilitySample(
        sample_id=sample_id,
        condition_id=condition_id,
        mode=mode_str,
        informational=informational,
        eligible=eligible,
        signals=ReliabilitySignals(
            event_loop_lag_ms=event_loop_lag_ms,
            queue_depth=queue_depth,
            dropped_frames=dropped_frames,
            journal_degraded=journal_degraded,
            active_sessions=active_sessions,
            memory_growth_kib=memory_growth_kib,
            unavailable_reason=unavailable_reason,
        ),
    )
