"""Strangler-fig parity tests.

Verify that the journal-derived views match the legacy systems for
the same inputs. Journal writes are now unconditional (the legacy
EASYCAT_LEGACY_OBS_DUAL_WRITE flag has been removed).
"""

from __future__ import annotations

import warnings

# Suppress expected deprecation warnings from legacy modules under test.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from easycat._span_manager import SpanManager
    from easycat.event_logging import EventLoggingConfig, EventTraceLogger
    from easycat.metrics import InMemoryMetrics
    from easycat.tracing import SpanStatus, Tracer

from easycat.events import EventBus, STTFinal, TurnStarted
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.records import JournalRecordKind

# Timestamp fields that legitimately diverge between legacy and journal.
TIMESTAMP_ALLOWLIST = {"timestamp", "elapsed_s", "wall_ns", "mono_ns", "duration_ms"}


class TestEventTraceLoggerParity:
    """EventTraceLogger legacy output vs journal EVENT records."""

    def _setup(self):
        journal = InMemoryRingBuffer(capacity=1000)
        event_bus = EventBus()
        etl = EventTraceLogger(event_bus, EventLoggingConfig(enabled=True), journal=journal)
        etl.start()
        return journal, event_bus, etl

    async def test_stt_final_parity(self):
        journal, event_bus, etl = self._setup()

        event = STTFinal(text="hello world")
        await event_bus.emit(event)

        # Legacy side: check the ring buffer.
        legacy_events = list(etl._recent)
        assert len(legacy_events) >= 1
        legacy = legacy_events[-1]

        # Journal side: check journal records.
        journal_events = journal.slice(kind=JournalRecordKind.EVENT)
        assert len(journal_events) >= 1
        j_rec = journal_events[-1]

        # Parity: event name matches.
        assert j_rec.name == "STTFinal"
        # Parity: the journal data dict contains the same keys as legacy
        # (modulo timestamp fields).
        legacy_keys = set(legacy.keys()) - TIMESTAMP_ALLOWLIST
        journal_keys = set(j_rec.data.keys()) - TIMESTAMP_ALLOWLIST
        assert legacy_keys == journal_keys, (
            f"Key mismatch: legacy={legacy_keys - journal_keys}, "
            f"journal={journal_keys - legacy_keys}"
        )

    async def test_multiple_events_same_count(self):
        journal, event_bus, etl = self._setup()

        await event_bus.emit(TurnStarted())
        await event_bus.emit(STTFinal(text="a"))
        await event_bus.emit(STTFinal(text="b"))

        legacy_count = len(etl._recent)
        journal_count = len(journal.slice(kind=JournalRecordKind.EVENT))
        assert legacy_count == journal_count


class TestSpanManagerParity:
    """SpanManager legacy spans vs journal SPAN_START/SPAN_END records."""

    def _setup(self):
        journal = InMemoryRingBuffer(capacity=1000)
        tracer = Tracer()
        sm = SpanManager(tracer=tracer, journal=journal)
        sm.bind_session("test-session")
        return journal, tracer, sm

    def test_span_lifecycle_parity(self):
        journal, tracer, sm = self._setup()

        # Start a turn and a child span via the real API.
        sm.begin_turn()
        sm.start("stt")

        # Finish the child span.
        sm.finish("stt", SpanStatus.OK)
        sm.finish("turn", SpanStatus.OK)

        # Legacy side: tracer exporter has spans.
        exported = tracer.exporter.spans
        legacy_span_names = {s.name for s in exported}
        assert "turn" in legacy_span_names
        assert "stt" in legacy_span_names

        # Journal side: paired start/end records.
        starts = journal.slice(kind=JournalRecordKind.SPAN_START)
        ends = journal.slice(kind=JournalRecordKind.SPAN_END)
        start_names = {r.name for r in starts}
        end_names = {r.name for r in ends}
        assert start_names == legacy_span_names
        assert end_names == legacy_span_names

    def test_error_span_parity(self):
        journal, tracer, sm = self._setup()

        sm.begin_turn()
        sm.start("agent")
        sm.finish_with_error("agent", ValueError("test error"))

        # Legacy: exported with ERROR status.
        agent_spans = tracer.exporter.get_spans_by_name("agent")
        assert len(agent_spans) == 1
        assert agent_spans[0].status == SpanStatus.ERROR

        # Journal: SPAN_END with error info.
        ends = journal.slice(kind=JournalRecordKind.SPAN_END)
        agent_ends = [r for r in ends if r.name == "agent"]
        assert len(agent_ends) == 1
        assert agent_ends[0].error is not None
        assert agent_ends[0].error.type == "ValueError"
        assert agent_ends[0].data["status"] == "error"


class TestInMemoryMetricsParity:
    """InMemoryMetrics legacy data vs journal METRIC records."""

    def _setup(self):
        journal = InMemoryRingBuffer(capacity=1000)
        metrics = InMemoryMetrics(journal=journal)
        metrics.bind_session("test-session")
        return journal, metrics

    def test_latency_parity(self):
        journal, metrics = self._setup()

        metrics.record_latency("stt_first_token", 123.4)
        metrics.record_latency("stt_first_token", 200.1)

        # Legacy: LatencyStats via get_metrics().
        legacy = metrics.get_metrics()
        assert "stt_first_token" in legacy["latencies"]
        assert legacy["latencies"]["stt_first_token"]["count"] == 2

        # Journal: two METRIC records.
        j_metrics = [
            r for r in journal.slice(kind=JournalRecordKind.METRIC) if r.name == "stt_first_token"
        ]
        assert len(j_metrics) == 2
        assert j_metrics[0].data["metric_type"] == "latency"
        assert j_metrics[0].data["value_ms"] == 123.4

    def test_counter_parity(self):
        journal, metrics = self._setup()

        metrics.increment_counter("turns_completed", 1)
        metrics.increment_counter("turns_completed", 1)

        # Legacy.
        legacy = metrics.get_metrics()
        assert legacy["counters"].get("turns_completed") == 2

        # Journal.
        j_counters = [
            r for r in journal.slice(kind=JournalRecordKind.METRIC) if r.name == "turns_completed"
        ]
        assert len(j_counters) == 2
        assert all(r.data["metric_type"] == "counter" for r in j_counters)


class TestJournalWriteAlwaysOn:
    """Journal writes are always on (legacy dual-write flag removed)."""

    def test_journal_writes_unconditionally(self):
        journal = InMemoryRingBuffer(capacity=100)
        metrics = InMemoryMetrics(journal=journal)
        metrics.bind_session("s")

        metrics.record_latency("lat", 10.0)
        metrics.increment_counter("cnt")

        # Legacy still works.
        assert metrics.get_metrics()["latencies"]["lat"]["count"] == 1
        assert metrics.get_metrics()["counters"]["cnt"] == 1

        # Journal always receives writes now.
        assert len(journal.read()) == 2
