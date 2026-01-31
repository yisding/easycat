"""Tests for metrics collection framework (WS8 Tasks 8.8–8.9)."""

from __future__ import annotations

import asyncio

from easycat.metrics import (
    AGENT_LATENCY,
    ERRORS,
    INTERRUPTIONS,
    RECONNECTS,
    STT_LATENCY,
    TTS_TTFB,
    TURN_E2E,
    InMemoryMetrics,
    LatencyStats,
    MetricsCollector,
    measure_latency,
    measure_latency_sync,
    timed_metric,
)

# ── LatencyStats ───────────────────────────────────────────────────


class TestLatencyStats:
    def test_record_and_stats(self):
        stats = LatencyStats()
        stats.record(10.0)
        stats.record(20.0)
        stats.record(30.0)

        assert stats.count == 3
        assert stats.total_ms == 60.0
        assert stats.avg_ms == 20.0
        assert stats.min_ms == 10.0
        assert stats.max_ms == 30.0

    def test_empty_stats(self):
        stats = LatencyStats()
        assert stats.count == 0
        assert stats.avg_ms == 0.0

    def test_to_dict(self):
        stats = LatencyStats()
        stats.record(50.0)
        d = stats.to_dict()
        assert d["count"] == 1
        assert d["avg_ms"] == 50.0
        assert d["min_ms"] == 50.0
        assert d["max_ms"] == 50.0


# ── InMemoryMetrics ────────────────────────────────────────────────


class TestInMemoryMetrics:
    def test_record_latency(self):
        m = InMemoryMetrics()
        m.record_latency("stt_latency_ms", 150.0)
        m.record_latency("stt_latency_ms", 200.0)

        stats = m.get_latency("stt_latency_ms")
        assert stats.count == 2
        assert stats.avg_ms == 175.0

    def test_increment_counter(self):
        m = InMemoryMetrics()
        m.increment_counter("errors")
        m.increment_counter("errors")
        m.increment_counter("errors", 3)
        assert m.get_counter("errors") == 5

    def test_get_metrics(self):
        m = InMemoryMetrics()
        m.record_latency("stt_latency_ms", 100.0)
        m.increment_counter("interruptions")

        result = m.get_metrics()
        assert "latencies" in result
        assert "counters" in result
        assert "stt_latency_ms" in result["latencies"]
        assert result["counters"]["interruptions"] == 1

    def test_reset(self):
        m = InMemoryMetrics()
        m.record_latency("stt_latency_ms", 100.0)
        m.increment_counter("errors")
        m.reset()

        assert m.get_metrics() == {"latencies": {}, "counters": {}}

    def test_implements_protocol(self):
        m = InMemoryMetrics()
        assert isinstance(m, MetricsCollector)

    def test_all_standard_metrics(self):
        """Record all five latency metrics + count metrics."""
        m = InMemoryMetrics()
        m.record_latency(STT_LATENCY, 150.0)
        m.record_latency(AGENT_LATENCY, 500.0)
        m.record_latency(TTS_TTFB, 80.0)
        m.record_latency(TURN_E2E, 730.0)

        m.increment_counter(INTERRUPTIONS)
        m.increment_counter(RECONNECTS, 2)
        m.increment_counter(ERRORS)

        result = m.get_metrics()
        assert len(result["latencies"]) == 4
        assert result["counters"][INTERRUPTIONS] == 1
        assert result["counters"][RECONNECTS] == 2
        assert result["counters"][ERRORS] == 1


# ── timed_metric decorator (Task 8.9) ─────────────────────────────


class TestTimedMetric:
    async def test_decorator_records_latency(self):
        m = InMemoryMetrics()

        @timed_metric("test_latency", m)
        async def slow_function():
            await asyncio.sleep(0.05)
            return "result"

        result = await slow_function()
        assert result == "result"

        stats = m.get_latency("test_latency")
        assert stats.count == 1
        assert stats.values[0] >= 40  # at least 40ms (allowing some tolerance)

    async def test_decorator_records_on_exception(self):
        m = InMemoryMetrics()

        @timed_metric("error_latency", m)
        async def failing_function():
            await asyncio.sleep(0.02)
            raise ValueError("boom")

        try:
            await failing_function()
        except ValueError:
            pass

        # Latency should still be recorded
        stats = m.get_latency("error_latency")
        assert stats.count == 1


# ── measure_latency context manager (Task 8.9) ────────────────────


class TestMeasureLatency:
    async def test_async_context_manager(self):
        m = InMemoryMetrics()

        async with measure_latency("ctx_latency", m):
            await asyncio.sleep(0.05)

        stats = m.get_latency("ctx_latency")
        assert stats.count == 1
        assert stats.values[0] >= 40

    async def test_async_context_manager_on_exception(self):
        m = InMemoryMetrics()

        try:
            async with measure_latency("error_ctx", m):
                raise RuntimeError("fail")
        except RuntimeError:
            pass

        stats = m.get_latency("error_ctx")
        assert stats.count == 1

    def test_sync_context_manager(self):
        m = InMemoryMetrics()

        with measure_latency_sync("sync_latency", m):
            # Do some work
            _ = sum(range(1000))

        stats = m.get_latency("sync_latency")
        assert stats.count == 1
        assert stats.values[0] >= 0  # should be very fast
