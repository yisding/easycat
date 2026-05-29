"""Correlation context for the ``easycat`` logger.

The session and turn ids are stashed in :class:`contextvars.ContextVar` slots so
any log record emitted while a session/turn is active can be tagged with them by
:class:`CorrelationFilter` — without threading the ids through every call site.
This is logging-only correlation; these keys are intentionally kept OUT of the
OpenTelemetry attribute allow-list (see :mod:`easycat._observability`).

``ContextVar`` propagation follows ``asyncio`` task boundaries, which is the only
concurrency boundary EasyCat crosses.  ``threading.Thread`` workers do not inherit
the bound values, but EasyCat avoids that boundary, so no thread resets are needed.
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


class CorrelationFilter(logging.Filter):
    """Enrich every record with session/turn ids; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_id.get() or "-"
        record.turn_id = _turn_id.get() or "-"
        return True
