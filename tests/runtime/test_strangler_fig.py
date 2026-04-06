"""Tests for strangler-fig dual-write adapters.

Verifies that EventTraceLogger, SpanManager, and InMemoryMetrics write
journal records when a journal is provided.
"""

from __future__ import annotations

from easycat._span_manager import SpanManager
from easycat.event_logging import EventLoggingConfig, EventTraceLogger
from easycat.events import EventBus, STTFinal
from easycat.metrics import InMemoryMetrics
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.records import JournalRecordKind
from easycat.tracing import Tracer


class TestEventTraceLoggerAdapter:
    async def test_events_appear_as_journal_records(self):
        journal = InMemoryRingBuffer(capacity=100)
        bus = EventBus()
        logger = EventTraceLogger(
            bus,
            EventLoggingConfig(enabled=True, include_partials=True),
            journal=journal,
        )
        logger.start()

        await bus.emit(STTFinal(text="hello world", session_id="s1", turn_id="t1"))

        logger.stop()

        records = journal.slice(kind=JournalRecordKind.EVENT)
        assert len(records) == 1
        rec = records[0]
        assert rec.name == "STTFinal"
        assert rec.session_id == "s1"
        assert rec.turn_id == "t1"
        assert "text" in rec.data or "text_chars" in rec.data

    async def test_no_journal_no_crash(self):
        """EventTraceLogger works fine without a journal."""
        bus = EventBus()
        logger = EventTraceLogger(bus, EventLoggingConfig(enabled=True))
        logger.start()
        await bus.emit(STTFinal(text="hello", session_id="s1"))
        logger.stop()
        # No exception means success


class TestSpanManagerAdapter:
    def test_span_lifecycle_produces_journal_records(self):
        journal = InMemoryRingBuffer(capacity=100)
        tracer = Tracer()
        sm = SpanManager(tracer=tracer, journal=journal)
        sm.bind_session("s1")

        sm.begin_turn()
        sm.start("stt")
        sm.finish("stt")
        sm.finish_all()

        starts = journal.slice(kind=JournalRecordKind.SPAN_START)
        ends = journal.slice(kind=JournalRecordKind.SPAN_END)

        # turn start + stt start = 2 starts
        assert len(starts) == 2
        # stt end + turn end (from finish_all) = 2 ends
        assert len(ends) == 2

        stt_start = [r for r in starts if r.name == "stt"]
        assert len(stt_start) == 1
        assert stt_start[0].session_id == "s1"
        assert "span_id" in stt_start[0].data

        stt_end = [r for r in ends if r.name == "stt"]
        assert len(stt_end) == 1
        assert stt_end[0].data["status"] == "ok"

    def test_error_span_records_error_info(self):
        journal = InMemoryRingBuffer(capacity=100)
        tracer = Tracer()
        sm = SpanManager(tracer=tracer, journal=journal)
        sm.bind_session("s1")

        sm.begin_turn()
        sm.start("agent")
        sm.finish_with_error("agent", ValueError("timeout"))

        ends = journal.slice(kind=JournalRecordKind.SPAN_END)
        agent_ends = [r for r in ends if r.name == "agent"]
        assert len(agent_ends) == 1
        assert agent_ends[0].error is not None
        assert agent_ends[0].error.type == "ValueError"
        assert agent_ends[0].data["status"] == "error"

    def test_no_journal_no_crash(self):
        """SpanManager works fine without a journal."""
        tracer = Tracer()
        sm = SpanManager(tracer=tracer)
        sm.begin_turn()
        sm.start("stt")
        sm.finish("stt")
        sm.finish_all()
        # No exception means success

    def test_no_tracer_no_crash(self):
        """SpanManager with journal but no tracer — no records written."""
        journal = InMemoryRingBuffer(capacity=100)
        sm = SpanManager(tracer=None, journal=journal)
        sm.begin_turn()
        sm.start("stt")
        sm.finish("stt")
        # No tracer means span operations are no-ops
        assert len(journal.read()) == 0


class TestInMemoryMetricsAdapter:
    def test_latency_produces_journal_record(self):
        journal = InMemoryRingBuffer(capacity=100)
        m = InMemoryMetrics(journal=journal)
        m.bind_session("s1")

        m.record_latency("stt_latency_ms", 150.0)

        records = journal.slice(kind=JournalRecordKind.METRIC)
        assert len(records) == 1
        rec = records[0]
        assert rec.name == "stt_latency_ms"
        assert rec.session_id == "s1"
        assert rec.data["metric_type"] == "latency"
        assert rec.data["value_ms"] == 150.0

    def test_counter_produces_journal_record(self):
        journal = InMemoryRingBuffer(capacity=100)
        m = InMemoryMetrics(journal=journal)
        m.bind_session("s1")

        m.increment_counter("errors", 1)

        records = journal.slice(kind=JournalRecordKind.METRIC)
        assert len(records) == 1
        rec = records[0]
        assert rec.name == "errors"
        assert rec.data["metric_type"] == "counter"
        assert rec.data["amount"] == 1

    def test_legacy_behavior_preserved(self):
        """InMemoryMetrics still works as before for legacy consumers."""
        journal = InMemoryRingBuffer(capacity=100)
        m = InMemoryMetrics(journal=journal)

        m.record_latency("stt_latency_ms", 100.0)
        m.record_latency("stt_latency_ms", 200.0)
        m.increment_counter("errors", 3)

        stats = m.get_latency("stt_latency_ms")
        assert stats.count == 2
        assert stats.avg_ms == 150.0
        assert m.get_counter("errors") == 3

    def test_no_journal_no_crash(self):
        """InMemoryMetrics works fine without a journal."""
        m = InMemoryMetrics()
        m.record_latency("stt_latency_ms", 100.0)
        m.increment_counter("errors")
        assert m.get_counter("errors") == 1
