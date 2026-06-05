"""Debugger optional-extra install hint coverage."""

from __future__ import annotations

import builtins
import logging
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


def _assert_debugger_hint(message: str) -> None:
    assert "uv add 'easycat[debugger]'" in message
    assert "uv sync --extra debugger" in message
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


def test_debugger_auto_launch_missing_aiohttp_install_hint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _block_aiohttp(monkeypatch)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)

    with caplog.at_level(logging.INFO, logger="easycat.debugger"):
        maybe_launch_debugger_ui(session=object())

    message = "\n".join(record.message for record in caplog.records)
    assert "skipping auto-launch" in message.lower()
    _assert_debugger_hint(message)
