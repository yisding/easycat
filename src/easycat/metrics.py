"""Metrics collection framework.

Provides a MetricsCollector interface and in-memory implementation,
plus helper decorators and context managers for instrumenting pipeline stages.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from easycat.runtime.journal import ExecutionJournal


def _dual_write_enabled() -> bool:
    return os.environ.get("EASYCAT_LEGACY_OBS_DUAL_WRITE", "1") != "0"


logger = logging.getLogger(__name__)


# ── MetricsCollector interface ─────────────────────────────────────


@runtime_checkable
class MetricsCollector(Protocol):
    """Interface for recording latency and count metrics.

    Implementations can export to Prometheus, StatsD, or any backend.
    """

    def record_latency(self, name: str, value_ms: float) -> None:
        """Record a latency measurement in milliseconds."""
        ...

    def increment_counter(self, name: str, amount: int = 1) -> None:
        """Increment a named counter."""
        ...

    def get_metrics(self) -> dict[str, Any]:
        """Return all collected metrics for retrieval/export."""
        ...


# ── In-memory implementation ──────────────────────────────────────


@dataclass
class LatencyStats:
    """Aggregated latency statistics for a metric."""

    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    values: list[float] = field(default_factory=list)

    def record(self, value_ms: float) -> None:
        self.count += 1
        self.total_ms += value_ms
        self.min_ms = min(self.min_ms, value_ms)
        self.max_ms = max(self.max_ms, value_ms)
        self.values.append(value_ms)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_ms": self.total_ms,
            "avg_ms": self.avg_ms,
            "min_ms": self.min_ms if self.count > 0 else 0.0,
            "max_ms": self.max_ms,
        }


class InMemoryMetrics:
    """In-memory metrics collector for development and testing.

    Stores latency distributions and counters in memory.
    """

    def __init__(self, *, journal: ExecutionJournal | None = None) -> None:
        self._latencies: dict[str, LatencyStats] = defaultdict(LatencyStats)
        self._counters: dict[str, int] = defaultdict(int)
        self._journal = journal
        self._session_id: str = ""

    def bind_session(self, session_id: str) -> None:
        """Set the session_id used for journal records."""
        self._session_id = session_id

    def record_latency(self, name: str, value_ms: float) -> None:
        self._latencies[name].record(value_ms)
        if self._journal is not None and _dual_write_enabled():
            from easycat.runtime.records import JournalRecordKind

            self._journal.append(
                kind=JournalRecordKind.METRIC,
                name=name,
                session_id=self._session_id,
                data={"metric_type": "latency", "value_ms": value_ms},
            )

    def increment_counter(self, name: str, amount: int = 1) -> None:
        self._counters[name] += amount
        if self._journal is not None and _dual_write_enabled():
            from easycat.runtime.records import JournalRecordKind

            self._journal.append(
                kind=JournalRecordKind.METRIC,
                name=name,
                session_id=self._session_id,
                data={"metric_type": "counter", "amount": amount},
            )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "latencies": {k: v.to_dict() for k, v in self._latencies.items()},
            "counters": dict(self._counters),
        }

    def get_latency(self, name: str) -> LatencyStats:
        """Get the LatencyStats for a specific metric."""
        return self._latencies[name]

    def get_counter(self, name: str) -> int:
        """Get the current value of a counter."""
        return self._counters[name]

    def reset(self) -> None:
        """Clear all collected metrics."""
        self._latencies.clear()
        self._counters.clear()


# ── Standard metric names ──────────────────────────────────────────

STT_LATENCY = "stt_latency_ms"
AGENT_LATENCY = "agent_latency_ms"
TTS_TTFB = "tts_ttfb_ms"
TURN_E2E = "turn_end_to_end_ms"
INTERRUPTIONS = "interruptions"
RECONNECTS = "reconnects"
ERRORS = "errors"


# ── Helpers: decorator and context managers ────────────────────────


def timed_metric(metric_name: str, collector: MetricsCollector) -> Callable[..., Any]:
    """Decorator that records the execution time of an async function.

    Usage::

        @timed_metric("stt_latency_ms", metrics)
        async def process_stt(...):
            ...
    """

    def decorator(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                collector.record_latency(metric_name, elapsed_ms)

        return wrapper

    return decorator


@asynccontextmanager
async def measure_latency(metric_name: str, collector: MetricsCollector):
    """Async context manager that records the elapsed time.

    Usage::

        async with measure_latency("stt_latency_ms", metrics):
            await do_stt()
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        collector.record_latency(metric_name, elapsed_ms)


@contextmanager
def measure_latency_sync(metric_name: str, collector: MetricsCollector):
    """Sync context manager that records the elapsed time.

    Usage::

        with measure_latency_sync("vad_latency_ms", metrics):
            process_chunk()
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        collector.record_latency(metric_name, elapsed_ms)
