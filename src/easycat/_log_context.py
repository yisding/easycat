"""Correlation context for the ``easycat`` logger.

The session and turn ids are stashed in :class:`contextvars.ContextVar` slots so
any log record emitted while a session/turn is active can be tagged with them by
:class:`CorrelationFilter` — without threading the ids through every call site.
This is logging-only correlation; these keys are intentionally kept OUT of the
OpenTelemetry attribute allow-list (see :mod:`easycat._observability`).

``ContextVar`` values are captured at task-creation time: a task inherits the
ids bound in the context that created it, and a record is enriched (by the
handler-level :class:`CorrelationFilter`) using the value live in the *emitting*
task's context.  A long-lived task created before an id is bound — e.g. the
audio-router pipeline loop, spawned at session start — will therefore NOT see a
later ``bind_turn`` from another task; such tasks re-bind explicitly each
iteration so their own log records stay correlated.  ``threading.Thread`` workers
do not inherit the bound values either, but EasyCat avoids that boundary, so no
thread resets are needed.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

_session_id: ContextVar[str | None] = ContextVar("easycat_session_id", default=None)
_turn_id: ContextVar[str | None] = ContextVar("easycat_turn_id", default=None)


def bind_session(session_id: str | None) -> Token[str | None]:
    """Bind *session_id* for the current context; returns a reset token."""
    return _session_id.set(session_id)


def bind_turn(turn_id: str | None) -> Token[str | None]:
    """Bind *turn_id* for the current context; returns a reset token."""
    return _turn_id.set(turn_id)


def reset_session(token: Token[str | None]) -> None:
    """Restore the session id binding represented by *token*; best-effort.

    A logging-correlation reset must never break teardown.  When ``stop()``
    runs in a different asyncio task than ``start()`` — e.g.
    ``SessionManager.stop_all()`` gathers each ``stop()`` in a fresh context —
    ``ContextVar.reset(token)`` raises ``ValueError`` ("Token was created in a
    different Context").  Fall back to clearing the binding in that case.
    """
    try:
        _session_id.reset(token)
    except ValueError:
        _session_id.set(None)


def reset_turn(token: Token[str | None]) -> None:
    """Restore the turn id binding represented by *token*; best-effort.

    See :func:`reset_session` — the reset can run in a different context than
    the bind (a cross-task ``stop()``), where ``reset(token)`` raises
    ``ValueError``; fall back to clearing the binding instead of propagating it.
    """
    try:
        _turn_id.reset(token)
    except ValueError:
        _turn_id.set(None)


class CorrelationFilter(logging.Filter):
    """Enrich every record with session/turn ids; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_id.get() or "-"
        record.turn_id = _turn_id.get() or "-"
        return True
