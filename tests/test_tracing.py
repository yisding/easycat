"""Tests for tracing span infrastructure."""

from __future__ import annotations

import asyncio

import pytest

from easycat.tracing import (
    InMemoryTraceExporter,
    Span,
    SpanStatus,
    TraceContext,
    Tracer,
)

# ── Span ───────────────────────────────────────────────────────────


class TestSpan:
    def test_creation(self):
        span = Span(name="stt", trace_id="abc123")
        assert span.name == "stt"
        assert span.trace_id == "abc123"
        assert span.end_time is None
        assert span.status == SpanStatus.OK
        assert span.duration_ms is None

    def test_finish(self):
        span = Span(name="stt", trace_id="abc123")
        span.finish()
        assert span.end_time is not None
        assert span.duration_ms is not None
        assert span.duration_ms >= 0

    def test_finish_with_status(self):
        span = Span(name="stt", trace_id="abc123")
        span.finish(SpanStatus.ERROR)
        assert span.status == SpanStatus.ERROR

    def test_set_error(self):
        span = Span(name="stt", trace_id="abc123")
        span.set_error(ValueError("bad input"))
        assert span.status == SpanStatus.ERROR
        assert span.metadata["error"] == "bad input"
        assert span.metadata["error_type"] == "ValueError"

    def test_to_dict(self):
        span = Span(name="stt", trace_id="abc123", parent_span_id="parent1")
        span.metadata["key"] = "value"
        span.finish()

        d = span.to_dict()
        assert d["name"] == "stt"
        assert d["trace_id"] == "abc123"
        assert d["parent_span_id"] == "parent1"
        assert d["status"] == "ok"
        assert d["duration_ms"] >= 0
        assert d["metadata"]["key"] == "value"

    def test_span_id_unique(self):
        s1 = Span(name="a", trace_id="t1")
        s2 = Span(name="b", trace_id="t1")
        assert s1.span_id != s2.span_id


# ── TraceContext ───────────────────────────────────────────────────


class TestTraceContext:
    def test_create_span(self):
        ctx = TraceContext(trace_id="trace1")
        span = ctx.create_span("noise_reduction")
        assert span.trace_id == "trace1"
        assert span.name == "noise_reduction"

    def test_create_span_with_parent(self):
        ctx = TraceContext(trace_id="trace1", root_span_id="root1")
        span = ctx.create_span("stt")
        assert span.parent_span_id == "root1"

    def test_create_span_explicit_parent(self):
        ctx = TraceContext(trace_id="trace1", root_span_id="root1")
        span = ctx.create_span("stt", parent_span_id="custom_parent")
        assert span.parent_span_id == "custom_parent"

    def test_agent_trace_id(self):
        ctx = TraceContext(trace_id="trace1", agent_trace_id="agent_abc")
        assert ctx.agent_trace_id == "agent_abc"

    def test_unique_trace_ids(self):
        ctx1 = TraceContext()
        ctx2 = TraceContext()
        assert ctx1.trace_id != ctx2.trace_id


# ── InMemoryTraceExporter ─────────────────────────────────────────


class TestInMemoryTraceExporter:
    def test_export_and_retrieve(self):
        exporter = InMemoryTraceExporter()
        span = Span(name="stt", trace_id="t1")
        span.finish()
        exporter.export(span)

        assert len(exporter.spans) == 1
        assert exporter.spans[0].name == "stt"

    def test_get_spans_by_trace(self):
        exporter = InMemoryTraceExporter()
        s1 = Span(name="stt", trace_id="t1")
        s2 = Span(name="tts", trace_id="t1")
        s3 = Span(name="stt", trace_id="t2")

        for s in [s1, s2, s3]:
            s.finish()
            exporter.export(s)

        t1_spans = exporter.get_spans_by_trace("t1")
        assert len(t1_spans) == 2

    def test_get_spans_by_name(self):
        exporter = InMemoryTraceExporter()
        s1 = Span(name="stt", trace_id="t1")
        s2 = Span(name="stt", trace_id="t2")
        s3 = Span(name="tts", trace_id="t1")

        for s in [s1, s2, s3]:
            s.finish()
            exporter.export(s)

        stt_spans = exporter.get_spans_by_name("stt")
        assert len(stt_spans) == 2

    def test_clear(self):
        exporter = InMemoryTraceExporter()
        exporter.export(Span(name="x", trace_id="t1"))
        exporter.clear()
        assert len(exporter.spans) == 0


# ── Tracer ─────────────────────────────────────────────────────────


class TestTracer:
    def test_standard_stage_names(self):
        assert Tracer.NOISE_REDUCTION == "noise_reduction"
        assert Tracer.VAD == "vad"
        assert Tracer.STT == "stt"
        assert Tracer.AGENT == "agent"
        assert Tracer.TTS == "tts"

    def test_start_and_finish_span(self):
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="t1")

        span = tracer.start_span("stt", ctx)
        assert span.trace_id == "t1"
        tracer.finish_span(span)

        assert len(exporter.spans) == 1
        assert exporter.spans[0].status == SpanStatus.OK
        assert exporter.spans[0].duration_ms is not None

    async def test_trace_async_context_manager(self):
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="t1")

        async with tracer.trace("stt", ctx) as span:
            await asyncio.sleep(0.02)
            span.metadata["transcript_len"] = 42

        assert len(exporter.spans) == 1
        exported = exporter.spans[0]
        assert exported.status == SpanStatus.OK
        assert exported.duration_ms >= 15  # at least 15ms
        assert exported.metadata["transcript_len"] == 42

    async def test_trace_records_error(self):
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="t1")

        with pytest.raises(ValueError):
            async with tracer.trace("agent", ctx):
                raise ValueError("bad")

        assert len(exporter.spans) == 1
        assert exporter.spans[0].status == SpanStatus.ERROR
        assert "bad" in exporter.spans[0].metadata["error"]

    async def test_trace_records_cancellation(self):
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="t1")

        with pytest.raises(asyncio.CancelledError):
            async with tracer.trace("tts", ctx):
                raise asyncio.CancelledError()

        assert len(exporter.spans) == 1
        assert exporter.spans[0].status == SpanStatus.CANCELLED

    def test_trace_sync_context_manager(self):
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="t1")

        with tracer.trace_sync("vad", ctx) as span:
            span.metadata["frames"] = 160

        assert len(exporter.spans) == 1
        assert exporter.spans[0].metadata["frames"] == 160

    def test_trace_sync_records_error(self):
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="t1")

        with pytest.raises(RuntimeError):
            with tracer.trace_sync("noise_reduction", ctx):
                raise RuntimeError("crash")

        assert exporter.spans[0].status == SpanStatus.ERROR


# ── Trace context propagation (Task 8.11) ─────────────────────────


class TestTraceContextPropagation:
    async def test_full_pipeline_trace(self):
        """Run a full turn and verify all spans are linked under one trace."""
        exporter = InMemoryTraceExporter()
        tracer = Tracer(exporter=exporter)
        ctx = TraceContext(trace_id="session_turn_1")

        # Create root span for the session turn
        root_span = tracer.start_span("turn", ctx)
        ctx.root_span_id = root_span.span_id

        # Simulate the pipeline stages
        async with tracer.trace(Tracer.NOISE_REDUCTION, ctx) as _:
            await asyncio.sleep(0.001)

        async with tracer.trace(Tracer.VAD, ctx) as _:
            await asyncio.sleep(0.001)

        async with tracer.trace(Tracer.STT, ctx) as span:
            await asyncio.sleep(0.01)
            span.metadata["transcript"] = "Hello"

        async with tracer.trace(Tracer.AGENT, ctx) as span:
            await asyncio.sleep(0.01)
            span.metadata["response_len"] = 20

        async with tracer.trace(Tracer.TTS, ctx) as span:
            await asyncio.sleep(0.01)
            span.metadata["chunks"] = 5

        tracer.finish_span(root_span)

        # All 6 spans (root + 5 stages) should be under the same trace
        trace_spans = exporter.get_spans_by_trace("session_turn_1")
        assert len(trace_spans) == 6

        # All child spans should reference the root span as parent
        child_spans = [s for s in trace_spans if s.name != "turn"]
        for child in child_spans:
            assert child.parent_span_id == root_span.span_id

        # Check pipeline stage names
        names = {s.name for s in trace_spans}
        assert names == {"turn", "noise_reduction", "vad", "stt", "agent", "tts"}

    async def test_agent_trace_id_linkage(self):
        """If Agents SDK provides a trace ID, it should be stored in context."""
        ctx = TraceContext(
            trace_id="easycat_trace",
            agent_trace_id="agents_sdk_trace_abc123",
        )
        assert ctx.agent_trace_id == "agents_sdk_trace_abc123"

        # Spans created under this context carry the EasyCat trace ID
        span = ctx.create_span("agent")
        assert span.trace_id == "easycat_trace"
