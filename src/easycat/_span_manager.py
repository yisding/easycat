"""Tracing span lifecycle manager for Session.

Centralizes span creation, finishing, and cleanup so that Session
doesn't repeat the same ``if self._tracer and self._X_span`` pattern
in every cancellation/error/completion path.
"""
# ruff: noqa: E402

from __future__ import annotations

import warnings

warnings.warn(
    "easycat._span_manager is deprecated. Use session.journal for observability. "
    "See docs/migration-debug-first-runtime.md for migration details.",
    DeprecationWarning,
    stacklevel=2,
)

from typing import TYPE_CHECKING

from easycat.tracing import Span, SpanStatus, TraceContext, Tracer

if TYPE_CHECKING:
    from easycat.runtime.journal import ExecutionJournal


class SpanManager:
    """Manages named tracing spans for a single session turn.

    Provides a uniform API for starting, finishing, and bulk-cancelling
    spans.  Session delegates all tracing bookkeeping here.
    """

    def __init__(
        self,
        tracer: Tracer | None = None,
        *,
        journal: ExecutionJournal | None = None,
    ) -> None:
        self._tracer = tracer
        self._journal = journal
        self._trace_context: TraceContext | None = None
        self._spans: dict[str, Span] = {}
        self._session_id: str = ""

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
        self._journal_span_start("turn", span)
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
        self._journal_span_start(name, span)
        return span

    def finish(self, name: str, status: SpanStatus = SpanStatus.OK) -> None:
        """Finish a named span and export it."""
        span = self._spans.pop(name, None)
        if span and self._tracer:
            self._tracer.finish_span(span, status)
            self._journal_span_end(name, span, status)

    def finish_with_error(self, name: str, error: BaseException) -> None:
        """Mark a named span as errored, finish, and export it."""
        span = self._spans.pop(name, None)
        if span and self._tracer:
            span.set_error(error)
            self._tracer.finish_span(span, SpanStatus.ERROR)
            self._journal_span_end(name, span, SpanStatus.ERROR, error=error)

    def finish_all(self, status: SpanStatus = SpanStatus.CANCELLED) -> None:
        """Finish all active spans with the given status.

        Used during cancellation and shutdown to ensure no spans leak.
        """
        if not self._tracer:
            return
        for name, span in self._spans.items():
            self._tracer.finish_span(span, status)
            self._journal_span_end(name, span, status)
        self._spans.clear()

    def get(self, name: str) -> Span | None:
        """Get an active span by name, or None."""
        return self._spans.get(name)

    def has(self, name: str) -> bool:
        """Check whether a named span is currently active."""
        return name in self._spans

    # ── Journal helpers ─────────────────────────────────────────

    def bind_session(self, session_id: str) -> None:
        """Set the session_id used for journal records."""
        self._session_id = session_id

    def _journal_span_start(self, name: str, span: Span) -> None:
        if self._journal is None:
            return
        from easycat.runtime.records import JournalRecordKind

        self._journal.append(
            kind=JournalRecordKind.SPAN_START,
            name=name,
            session_id=self._session_id,
            data={
                "span_id": span.span_id,
                "trace_id": span.trace_id,
            },
        )

    def _journal_span_end(
        self,
        name: str,
        span: Span,
        status: SpanStatus,
        *,
        error: BaseException | None = None,
    ) -> None:
        if self._journal is None:
            return
        from easycat.runtime.records import ErrorInfo, JournalRecordKind

        data: dict[str, object] = {
            "span_id": span.span_id,
            "status": status.value,
            "duration_ms": span.duration_ms,
        }
        err_info = None
        if error is not None:
            err_info = ErrorInfo(
                type=type(error).__name__,
                message=str(error),
            )
        self._journal.append(
            kind=JournalRecordKind.SPAN_END,
            name=name,
            session_id=self._session_id,
            data=data,
            error=err_info,
        )
