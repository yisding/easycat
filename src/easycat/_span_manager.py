"""Tracing span lifecycle manager for Session.

Centralizes span creation, finishing, and cleanup so that Session
doesn't repeat the same ``if self._tracer and self._X_span`` pattern
in every cancellation/error/completion path.
"""

from __future__ import annotations

from easycat.tracing import Span, SpanStatus, TraceContext, Tracer


class SpanManager:
    """Manages named tracing spans for a single session turn.

    Provides a uniform API for starting, finishing, and bulk-cancelling
    spans.  Session delegates all tracing bookkeeping here.
    """

    def __init__(self, tracer: Tracer | None = None) -> None:
        self._tracer = tracer
        self._trace_context: TraceContext | None = None
        self._spans: dict[str, Span] = {}

    # ── Properties ──────────────────────────────────────────────

    @property
    def tracer(self) -> Tracer | None:
        return self._tracer

    @property
    def trace_context(self) -> TraceContext | None:
        return self._trace_context

    @property
    def enabled(self) -> bool:
        return self._tracer is not None

    # ── Turn-level lifecycle ────────────────────────────────────

    def begin_turn(self) -> Span | None:
        """Start a new trace context and root turn span.

        Returns the turn span, or None if tracing is disabled.
        """
        if not self._tracer:
            return None
        self._trace_context = TraceContext()
        span = self._tracer.start_span("turn", self._trace_context)
        self._trace_context.root_span_id = span.span_id
        self._spans["turn"] = span
        return span

    # ── Span operations ─────────────────────────────────────────

    def start(self, name: str) -> Span | None:
        """Start a named span under the current trace context.

        Returns the span, or None if tracing is disabled / no context.
        """
        if not self._tracer or not self._trace_context:
            return None
        span = self._tracer.start_span(name, self._trace_context)
        self._spans[name] = span
        return span

    def finish(self, name: str, status: SpanStatus = SpanStatus.OK) -> None:
        """Finish a named span and export it."""
        span = self._spans.pop(name, None)
        if span and self._tracer:
            self._tracer.finish_span(span, status)

    def finish_with_error(self, name: str, error: BaseException) -> None:
        """Mark a named span as errored, finish, and export it."""
        span = self._spans.pop(name, None)
        if span and self._tracer:
            span.set_error(error)
            self._tracer.finish_span(span, SpanStatus.ERROR)

    def finish_all(self, status: SpanStatus = SpanStatus.CANCELLED) -> None:
        """Finish all active spans with the given status.

        Used during cancellation and shutdown to ensure no spans leak.
        """
        if not self._tracer:
            return
        for span in self._spans.values():
            self._tracer.finish_span(span, status)
        self._spans.clear()

    def get(self, name: str) -> Span | None:
        """Get an active span by name, or None."""
        return self._spans.get(name)

    def has(self, name: str) -> bool:
        """Check whether a named span is currently active."""
        return name in self._spans
