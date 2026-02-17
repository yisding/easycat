"""Centralized tracing span lifecycle management.

Encapsulates the Tracer reference, TraceContext, and named span slots
so that Session doesn't need to repeat null-check-then-finish patterns
at every span site.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from easycat.tracing import Span, SpanStatus, TraceContext, Tracer


class SpanManager:
    """Manages tracing spans for a single session turn.

    Provides a thin facade over the Tracer that:
    - Guards every operation behind an ``enabled`` check
    - Tracks named span slots (turn, stt, agent, tts) so callers
      don't need their own ``if tracer and span:`` conditionals
    - Offers ``finish_all()`` to bulk-close open spans on cancellation
    """

    def __init__(self, tracer: Tracer | None) -> None:
        self._tracer = tracer
        self._context: TraceContext | None = None
        self._spans: dict[str, Span | None] = {
            "turn": None,
            "stt": None,
            "agent": None,
            "tts": None,
        }

    @property
    def enabled(self) -> bool:
        """Whether tracing is active."""
        return self._tracer is not None

    @property
    def context(self) -> TraceContext | None:
        return self._context

    def new_turn(self) -> None:
        """Start a new trace context and root turn span."""
        if not self._tracer:
            return
        self._context = TraceContext()
        span = self._tracer.start_span("turn", self._context)
        self._context.root_span_id = span.span_id
        self._spans["turn"] = span

    def start(self, stage: str) -> Span | None:
        """Start a span for the given pipeline stage.

        Returns the Span (or None if tracing is disabled).
        """
        if not self._tracer or not self._context:
            return None
        span = self._tracer.start_span(stage, self._context)
        self._spans[stage] = span
        return span

    def finish(self, stage: str, status: SpanStatus = SpanStatus.OK) -> None:
        """Finish the span for the given stage (no-op if absent)."""
        span = self._spans.get(stage)
        if span and self._tracer:
            self._tracer.finish_span(span, status)
            self._spans[stage] = None

    def finish_all(self, status: SpanStatus = SpanStatus.CANCELLED) -> None:
        """Finish all open spans with the given status."""
        if not self._tracer:
            return
        for stage in list(self._spans):
            self.finish(stage, status)

    def get(self, stage: str) -> Span | None:
        """Get the current span for a stage (or None)."""
        return self._spans.get(stage)

    @asynccontextmanager
    async def trace(self, stage: str) -> AsyncIterator[Span | None]:
        """Async context manager that starts/finishes a span.

        When tracing is disabled, yields None and executes the body
        unconditionally — no branching needed at the call site.
        """
        if not self._tracer or not self._context:
            yield None
            return
        async with self._tracer.trace(stage, self._context) as span:
            yield span
