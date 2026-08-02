"""Console logging ownership for the ``easycat`` logger.

Library code stays silent by default (see the ``NullHandler`` installed in
:mod:`easycat`).  Process owners — the ``easycat`` CLI, :func:`easycat.run`,
and ``debug="light"`` wiring — opt in to console output through
:func:`enable_console_logging`, which attaches exactly ONE tagged handler to
the ``easycat`` logger (never the root logger) so applications that already
own logging are never clobbered.
"""

from __future__ import annotations

import json
import logging
import os
import sys

_HANDLER_TAG = "_easycat_console"

_LOG_FORMAT = "%(asctime)s [%(session_id)s/%(turn_id)s] %(name)s %(levelname)s %(message)s"
# RichHandler renders time/level/logger in its own columns, so the Rich format
# only needs to prepend the correlation ids to the message column.
_RICH_LOG_FORMAT = "[%(session_id)s/%(turn_id)s] %(message)s"
_LOG_FORMAT_VALUES = frozenset({"json", "text", "human"})


def enable_console_logging(level: int | str | None = None, *, force: bool = False) -> None:
    """Attach ONE console handler to the ``easycat`` logger (never the root logger).

    The level is resolved via :func:`easycat.config._resolve_easycat_log_level`
    when *level* is ``None`` so ``EASYCAT_LOG_LEVEL`` keeps a single meaning
    across every entry point.  ``propagate`` is disabled so records do not also
    surface through a root handler the host configured, which would otherwise
    double-log.  Idempotent: a second call is a no-op unless *force* is set.
    """
    from easycat.config import _resolve_easycat_log_level

    logger = logging.getLogger("easycat")
    if level is None:
        level = _resolve_easycat_log_level(default=logging.INFO)
    else:
        level = _coerce_level(level)
    logger.setLevel(level)

    if not force and any(getattr(h, _HANDLER_TAG, False) for h in logger.handlers):
        return

    handler = _make_handler()
    setattr(handler, _HANDLER_TAG, True)
    # The correlation filter always populates ``session_id``/``turn_id`` so the
    # formatter slots resolve even when nothing is bound.
    from easycat._log_context import CorrelationFilter

    handler.addFilter(CorrelationFilter())
    logger.addHandler(handler)
    logger.propagate = False


def set_easycat_log_level(level: int | str) -> None:
    """Set the ``easycat`` logger level without touching handlers or root.

    Thin wrapper over ``logging.getLogger("easycat").setLevel`` that accepts a
    level name (case-insensitive) or an integer.  Does not call ``basicConfig``
    and never attaches a handler.
    """
    logging.getLogger("easycat").setLevel(_coerce_level(level))


def _coerce_level(level: int | str) -> int:
    """Coerce a level name (case-insensitive) or int to a logging level int.

    ``logging.getLevelName`` returns a string like ``"Level FOO"`` for an
    unknown name, which ``setLevel`` then rejects with a cryptic
    ``Unknown level: 'Level FOO'``. Validate here so a bad value raises a clear
    error that names the original input and the valid options.
    """
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.upper())
    if not isinstance(resolved, int):
        raise ValueError(  # noqa: TRY004 domain-specific validation error
            f"Unknown logging level: {level!r}. Use one of "
            "DEBUG, INFO, WARNING, ERROR, CRITICAL (or an int)."
        )
    return resolved


class _JsonFormatter(logging.Formatter):
    """Render records as a single-line JSON object.

    Opt-in via ``EASYCAT_LOG_FORMAT=json``.  The field set (``ts``, ``level``,
    ``logger``, ``msg``, ``session_id``, ``turn_id``, and ``exc`` when an
    exception is attached) is a semi-public UNSTABLE schema.  Every line round-
    trips through :func:`json.loads`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "session_id": getattr(record, "session_id", None),
            "turn_id": getattr(record, "turn_id", None),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _make_handler() -> logging.Handler:
    """Build the console handler.

    ``EASYCAT_LOG_FORMAT=json`` selects the structured formatter,
    ``text`` selects plain non-Rich text, and ``human`` selects the
    Rich-capable interactive renderer.  When the format is not explicit,
    ``EASYCAT_ENV=prod`` selects JSON for log pipelines while ``dev``/unset
    keeps the human renderer.  The color decision is the shared policy in
    :mod:`easycat._console`.
    """
    log_format = _resolve_log_format()
    if log_format == "json":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        return handler
    if log_format == "text":
        return _plain_text_handler()

    from easycat._console import color_enabled

    if color_enabled():
        from rich.console import Console
        from rich.logging import RichHandler

        rich_handler = RichHandler(
            console=Console(stderr=True, force_terminal=True),
            show_path=False,
            rich_tracebacks=True,
        )
        # Without a formatter, RichHandler renders only %(message)s, dropping the
        # session/turn ids the CorrelationFilter populates. Prepend them to the
        # message column so the interactive color path stays correlated too.
        rich_handler.setFormatter(logging.Formatter(_RICH_LOG_FORMAT))
        return rich_handler
    return _plain_text_handler()


def _plain_text_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    return handler


def _wants_json_logs() -> bool:
    return _resolve_log_format() == "json"


def _resolve_log_format() -> str:
    log_format = os.getenv("EASYCAT_LOG_FORMAT", "").strip().lower()
    if log_format:
        if log_format not in _LOG_FORMAT_VALUES:
            raise ValueError(
                f"Unknown EASYCAT_LOG_FORMAT: {log_format!r}. Use 'json', 'text', or 'human'."
            )
        return log_format
    if os.getenv("EASYCAT_ENV", "").strip().lower() in {"prod", "production"}:
        return "json"
    return "human"
