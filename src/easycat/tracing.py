"""Tracing span infrastructure (WS8 Tasks 8.10–8.11).

Provides span creation for EasyCat pipeline stages with support for
trace context propagation. Each span records start time, end time,
status, and metadata. Integrates with the Agents SDK trace context
pass-through pattern.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Span status ────────────────────────────────────────────────────


class SpanStatus(enum.Enum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


# ── Span dataclass ─────────────────────────────────────────────────


@dataclass
class Span:
    """A tracing span representing a unit of work in the pipeline."""

    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str | None = None
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    status: SpanStatus = SpanStatus.OK
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000

    def finish(self, status: SpanStatus = SpanStatus.OK) -> None:
        """Mark the span as completed."""
        self.end_time = time.monotonic()
        self.status = status

    def set_error(self, error: Exception) -> None:
        """Mark the span as errored."""
        self.status = SpanStatus.ERROR
        self.metadata["error"] = str(error)
        self.metadata["error_type"] = type(error).__name__

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "metadata": self.metadata,
        }


# ── Trace context ──────────────────────────────────────────────────


@dataclass
class TraceContext:
    """Context propagated through the pipeline for a single session/turn.

    Links EasyCat spans to each other and optionally to the Agents SDK
    trace context.
    """

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    root_span_id: str | None = None
    agent_trace_id: str | None = None  # from Agents SDK, if available

    def create_span(
        self,
        name: str,
        parent_span_id: str | None = None,
        **metadata: Any,
    ) -> Span:
        """Create a new span under this trace context."""
        span = Span(
            name=name,
            trace_id=self.trace_id,
            parent_span_id=parent_span_id or self.root_span_id,
            metadata=metadata,
        )
        return span


# ── Trace exporter interface ───────────────────────────────────────


class TraceExporter:
    """Base class for trace exporters.

    Subclass and override `export` to send spans to a backend.
    """

    def export(self, span: Span) -> None:
        """Export a completed span."""
        pass


class InMemoryTraceExporter(TraceExporter):
    """In-memory trace exporter for development and testing."""

    def __init__(self) -> None:
        self._spans: list[Span] = []

    def export(self, span: Span) -> None:
        self._spans.append(span)

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    def get_spans_by_trace(self, trace_id: str) -> list[Span]:
        """Return all spans for a given trace ID."""
        return [s for s in self._spans if s.trace_id == trace_id]

    def get_spans_by_name(self, name: str) -> list[Span]:
        """Return all spans with a given name."""
        return [s for s in self._spans if s.name == name]

    def clear(self) -> None:
        self._spans.clear()


# ── Tracer ─────────────────────────────────────────────────────────


class Tracer:
    """Main tracer for creating and managing spans.

    Integrates with TraceContext for propagation and exports completed
    spans via the configured exporter.
    """

    # Standard pipeline stage names
    NOISE_REDUCTION = "noise_reduction"
    VAD = "vad"
    STT = "stt"
    AGENT = "agent"
    TTS = "tts"

    def __init__(self, exporter: TraceExporter | None = None) -> None:
        self._exporter = exporter or InMemoryTraceExporter()

    @property
    def exporter(self) -> TraceExporter:
        return self._exporter

    def start_span(
        self,
        name: str,
        context: TraceContext,
        parent_span_id: str | None = None,
        **metadata: Any,
    ) -> Span:
        """Create and return a new span. Caller must call span.finish()."""
        return context.create_span(name, parent_span_id=parent_span_id, **metadata)

    def finish_span(self, span: Span, status: SpanStatus = SpanStatus.OK) -> None:
        """Finish a span and export it."""
        span.finish(status)
        self._exporter.export(span)

    @asynccontextmanager
    async def trace(
        self,
        name: str,
        context: TraceContext,
        parent_span_id: str | None = None,
        **metadata: Any,
    ):
        """Async context manager for tracing a pipeline stage.

        Usage::

            async with tracer.trace("stt", ctx) as span:
                result = await stt.process()
                span.metadata["transcript_len"] = len(result)
        """
        span = self.start_span(name, context, parent_span_id=parent_span_id, **metadata)
        try:
            yield span
            self.finish_span(span, SpanStatus.OK)
        except asyncio.CancelledError:
            self.finish_span(span, SpanStatus.CANCELLED)
            raise
        except Exception as exc:
            span.set_error(exc)
            self.finish_span(span, SpanStatus.ERROR)
            raise

    @contextmanager
    def trace_sync(
        self,
        name: str,
        context: TraceContext,
        parent_span_id: str | None = None,
        **metadata: Any,
    ):
        """Sync context manager for tracing a pipeline stage."""
        span = self.start_span(name, context, parent_span_id=parent_span_id, **metadata)
        try:
            yield span
            self.finish_span(span, SpanStatus.OK)
        except Exception as exc:
            span.set_error(exc)
            self.finish_span(span, SpanStatus.ERROR)
            raise
