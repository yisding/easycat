"""Debugger optional-extra install hint coverage."""

from __future__ import annotations

import builtins
import logging
import sys
import types
from typing import Any

import pytest

from easycat.debugger import server as debugger_server
from easycat.debugger._autolaunch import maybe_launch_debugger_ui


def _block_aiohttp(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fail_aiohttp_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "aiohttp":
            raise ImportError("missing aiohttp")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_aiohttp_import)


def _allow_aiohttp_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))


def _assert_debugger_hint(message: str) -> None:
    assert "uv add 'easycat[debugger]'" in message
    assert "uv sync --extra debugger --group dev" in message
    assert "pip install easycat[debugger]" not in message


def test_debugger_server_missing_aiohttp_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_aiohttp(monkeypatch)

    with pytest.raises(RuntimeError) as exc_info:
        debugger_server._ensure_aiohttp()

    _assert_debugger_hint(str(exc_info.value))


def test_debugger_app_missing_aiohttp_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_aiohttp(monkeypatch)

    with pytest.raises(RuntimeError) as exc_info:
        debugger_server._make_app(source=object())

    _assert_debugger_hint(str(exc_info.value))


def _opt_in_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the guards and fake an interactive opted-in terminal context."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_AUTOLAUNCH", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)


def test_debugger_auto_launch_missing_aiohttp_install_hint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _block_aiohttp(monkeypatch)
    _opt_in_interactive(monkeypatch)

    with caplog.at_level(logging.INFO, logger="easycat.debugger"):
        maybe_launch_debugger_ui(session=object())

    message = "\n".join(record.message for record in caplog.records)
    assert "skipping auto-launch" in message.lower()
    _assert_debugger_hint(message)


def test_debugger_auto_launch_logs_warning_when_serve_session_raises_oserror(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import easycat.debugger as debugger_pkg

    _allow_aiohttp_import(monkeypatch)
    _opt_in_interactive(monkeypatch)
    monkeypatch.delenv("EASYCAT_DEBUGGER_PORT", raising=False)

    def fail_serve_session(*args: Any, **kwargs: Any) -> None:
        raise OSError("port busy")

    monkeypatch.setattr(debugger_pkg, "serve_session", fail_serve_session)

    with caplog.at_level(logging.WARNING, logger="easycat.debugger"):
        maybe_launch_debugger_ui(session=object())

    assert any(
        record.levelno == logging.WARNING
        and "Could not start debugger UI on port 8765: port busy" in record.message
        for record in caplog.records
    )


def test_debugger_auto_launch_logs_exception_when_serve_session_raises_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import easycat.debugger as debugger_pkg

    _allow_aiohttp_import(monkeypatch)
    _opt_in_interactive(monkeypatch)
    monkeypatch.delenv("EASYCAT_DEBUGGER_PORT", raising=False)

    def fail_serve_session(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(debugger_pkg, "serve_session", fail_serve_session)

    with caplog.at_level(logging.ERROR, logger="easycat.debugger"):
        maybe_launch_debugger_ui(session=object())

    matching = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "Debugger UI failed to start; continuing without it." in record.message
    ]
    assert len(matching) == 1
    assert matching[0].exc_info is not None
    assert matching[0].exc_info[0] is RuntimeError
