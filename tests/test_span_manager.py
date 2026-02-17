"""Tests for SpanManager — centralized tracing span lifecycle."""

import pytest

from easycat.span_manager import SpanManager
from easycat.tracing import InMemoryTraceExporter, SpanStatus, Tracer

# ── Disabled tracer (no-op behaviour) ──────────────────────────────


class TestSpanManagerDisabled:
    """When tracer is None, every operation is a safe no-op."""

    def test_enabled_is_false(self):
        sm = SpanManager(None)
        assert not sm.enabled

    def test_new_turn_is_noop(self):
        sm = SpanManager(None)
        sm.new_turn()
        assert sm.context is None

    def test_start_returns_none(self):
        sm = SpanManager(None)
        assert sm.start("stt") is None

    def test_finish_is_noop(self):
        sm = SpanManager(None)
        sm.finish("stt")  # should not raise

    def test_finish_all_is_noop(self):
        sm = SpanManager(None)
        sm.finish_all()  # should not raise

    def test_get_returns_none(self):
        sm = SpanManager(None)
        assert sm.get("turn") is None

    @pytest.mark.asyncio
    async def test_trace_yields_none(self):
        sm = SpanManager(None)
        async with sm.trace("stt") as span:
            assert span is None


# ── Enabled tracer ─────────────────────────────────────────────────


def _make_manager() -> tuple[SpanManager, InMemoryTraceExporter]:
    exporter = InMemoryTraceExporter()
    tracer = Tracer(exporter)
    return SpanManager(tracer), exporter


class TestSpanManagerEnabled:
    def test_enabled_is_true(self):
        sm, _ = _make_manager()
        assert sm.enabled

    def test_new_turn_creates_context_and_turn_span(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        assert sm.context is not None
        assert sm.context.root_span_id is not None
        turn_span = sm.get("turn")
        assert turn_span is not None
        assert turn_span.name == "turn"
        assert turn_span.span_id == sm.context.root_span_id

    def test_start_creates_span(self):
        sm, _ = _make_manager()
        sm.new_turn()
        span = sm.start("stt")
        assert span is not None
        assert span.name == "stt"
        assert sm.get("stt") is span

    def test_start_without_context_returns_none(self):
        sm, _ = _make_manager()
        # No new_turn called — no context
        assert sm.start("stt") is None

    def test_finish_exports_span(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        sm.start("stt")
        sm.finish("stt")
        assert sm.get("stt") is None
        stt_spans = exporter.get_spans_by_name("stt")
        assert len(stt_spans) == 1
        assert stt_spans[0].status == SpanStatus.OK

    def test_finish_with_error_status(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        sm.start("agent")
        sm.finish("agent", SpanStatus.ERROR)
        agent_spans = exporter.get_spans_by_name("agent")
        assert agent_spans[0].status == SpanStatus.ERROR

    def test_finish_nonexistent_stage_is_noop(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        sm.finish("stt")  # never started — should not raise
        assert len(exporter.spans) == 0  # only turn span exists but not finished

    def test_finish_all_closes_open_spans(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        sm.start("stt")
        sm.start("agent")
        # turn + stt + agent are open
        sm.finish_all(SpanStatus.CANCELLED)
        assert sm.get("turn") is None
        assert sm.get("stt") is None
        assert sm.get("agent") is None
        # All three should be exported
        assert len(exporter.spans) == 3
        for span in exporter.spans:
            assert span.status == SpanStatus.CANCELLED

    def test_finish_all_skips_already_finished(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        sm.start("stt")
        sm.finish("stt")  # finish stt normally
        sm.finish_all(SpanStatus.CANCELLED)
        # stt exported once with OK, turn exported once with CANCELLED
        stt_spans = exporter.get_spans_by_name("stt")
        assert len(stt_spans) == 1
        assert stt_spans[0].status == SpanStatus.OK
        turn_spans = exporter.get_spans_by_name("turn")
        assert len(turn_spans) == 1
        assert turn_spans[0].status == SpanStatus.CANCELLED

    def test_multiple_turns(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        first_trace_id = sm.context.trace_id
        sm.finish_all()
        sm.new_turn()
        second_trace_id = sm.context.trace_id
        sm.finish_all()
        # Two different trace IDs
        assert first_trace_id != second_trace_id
        # All spans exported
        assert len(exporter.spans) == 2  # two turn spans

    @pytest.mark.asyncio
    async def test_trace_context_manager(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        async with sm.trace("stt") as span:
            assert span is not None
            assert span.name == "stt"
        # Span should be exported after exiting context
        stt_spans = exporter.get_spans_by_name("stt")
        assert len(stt_spans) == 1
        assert stt_spans[0].status == SpanStatus.OK

    @pytest.mark.asyncio
    async def test_trace_context_manager_propagates_errors(self):
        sm, exporter = _make_manager()
        sm.new_turn()
        with pytest.raises(ValueError, match="test error"):
            async with sm.trace("agent"):
                raise ValueError("test error")
        agent_spans = exporter.get_spans_by_name("agent")
        assert len(agent_spans) == 1
        assert agent_spans[0].status == SpanStatus.ERROR
