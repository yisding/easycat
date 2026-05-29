"""Tests for the ``easycat`` logging surface.

Covers library hygiene (NullHandler / no ``basicConfig`` on import),
correlation-id enrichment, the opt-in JSON formatter, console-handler
ownership/idempotency, and the degraded-journal WARNING path — all via real
``caplog`` records rather than mocks.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from easycat._log_context import CorrelationFilter, bind_session, bind_turn
from easycat._logging import _HANDLER_TAG, _JsonFormatter, enable_console_logging


@pytest.fixture
def easycat_logger_state():
    """Snapshot and restore the ``easycat`` logger's mutable state.

    ``enable_console_logging`` attaches handlers and flips ``propagate``; without
    restoring it, that state would leak into other tests (and break ``caplog``,
    which relies on propagation reaching the root handler).
    """
    logger = logging.getLogger("easycat")
    handlers = logger.handlers[:]
    level = logger.level
    propagate = logger.propagate
    filters = logger.filters[:]
    try:
        yield logger
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.filters[:] = filters


@pytest.fixture(autouse=True)
def _reset_correlation_context():
    """Ensure each test starts with unbound correlation ids.

    Session/turn tests elsewhere bind ids into the module-level ``ContextVar``
    slots and do not reset them; without this, collection order would leak a
    stale id into the unbound-default assertions here.
    """
    bind_session(None)
    bind_turn(None)
    yield


def test_import_installs_only_nullhandler_and_leaves_root_untouched() -> None:
    """A fresh ``import easycat`` adds one NullHandler and nothing to root."""
    code = (
        "import logging\n"
        "before = list(logging.root.handlers)\n"
        "import easycat\n"
        "lg = logging.getLogger('easycat')\n"
        "null = [h for h in lg.handlers if isinstance(h, logging.NullHandler)]\n"
        "after = list(logging.root.handlers)\n"
        "assert len(lg.handlers) == 1, lg.handlers\n"
        "assert len(null) == 1, lg.handlers\n"
        "assert before == after, (before, after)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_import_does_not_call_basicconfig() -> None:
    """Importing easycat (and EasyConfig) must not configure root logging."""
    code = (
        "import logging\n"
        "import easycat\n"
        "from easycat import EasyConfig\n"
        "assert logging.root.handlers == [], logging.root.handlers\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_degraded_journal_emits_warning_not_error(caplog) -> None:
    from easycat.runtime.journal import InMemoryRingBuffer
    from easycat.runtime.records import JournalRecordKind

    journal = InMemoryRingBuffer(capacity=10)

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    journal._do_append = broken

    with caplog.at_level(logging.WARNING, logger="easycat"):
        seq = journal.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s1",
        )

    assert seq == -1
    assert journal.degraded is True
    assert "Journal entered degraded mode" in caplog.text
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert warnings, caplog.records
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_bound_contextvars_enrich_records() -> None:
    """Records emitted under bound contextvars carry session/turn ids."""
    record = logging.LogRecord(
        name="easycat.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    filt = CorrelationFilter()

    import easycat._log_context as ctx

    session_token = bind_session("sess-42")
    turn_token = bind_turn("turn-7")
    try:
        assert filt.filter(record) is True
        assert record.session_id == "sess-42"
        assert record.turn_id == "turn-7"
    finally:
        ctx._session_id.reset(session_token)
        ctx._turn_id.reset(turn_token)


def test_unbound_contextvars_default_to_dash() -> None:
    record = logging.LogRecord(
        name="easycat.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )
    assert CorrelationFilter().filter(record) is True
    assert record.session_id == "-"
    assert record.turn_id == "-"


def test_json_formatter_round_trips_with_correlation_fields() -> None:
    record = logging.LogRecord(
        name="easycat.session",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="turn %s done",
        args=("abc",),
        exc_info=None,
    )
    # Populate the correlation slots the way the filter would at runtime.
    record.session_id = "sess-1"
    record.turn_id = "turn-1"

    line = _JsonFormatter().format(record)
    payload = json.loads(line)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "easycat.session"
    assert payload["msg"] == "turn abc done"
    assert payload["session_id"] == "sess-1"
    assert payload["turn_id"] == "turn-1"
    assert "ts" in payload
    assert "exc" not in payload


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="easycat.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(_JsonFormatter().format(record))
    assert payload["level"] == "ERROR"
    assert "ValueError: boom" in payload["exc"]


def test_enable_console_logging_attaches_exactly_one_tagged_handler(
    easycat_logger_state,
) -> None:
    logger = easycat_logger_state
    # Start from a clean slate so the assertion is about this call only.
    logger.handlers[:] = []

    enable_console_logging()

    tagged = [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]
    assert len(tagged) == 1
    assert logger.propagate is False
    # The handler carries the correlation filter so formatter slots resolve.
    assert any(isinstance(f, CorrelationFilter) for f in tagged[0].filters)


def test_enable_console_logging_is_idempotent(easycat_logger_state) -> None:
    logger = easycat_logger_state
    logger.handlers[:] = []

    enable_console_logging()
    first = [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]
    enable_console_logging()
    second = [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]

    assert len(first) == 1
    assert len(second) == 1
    assert first[0] is second[0]


def test_enable_console_logging_force_adds_a_second_handler(easycat_logger_state) -> None:
    logger = easycat_logger_state
    logger.handlers[:] = []

    enable_console_logging()
    enable_console_logging(force=True)

    tagged = [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]
    assert len(tagged) == 2
