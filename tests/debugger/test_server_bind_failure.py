"""Synchronous port-bind failure surfacing for the debugger server.

``serve_session(..., in_thread=True)`` must probe-bind on the calling thread
*before* starting the daemon so a port collision raises synchronously (instead
of an unhandled exception inside a background thread the autolaunch try/except
can never catch). And a session whose bind fails must open no browser tab.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import pytest

pytest.importorskip("aiohttp")

from easycat.debugger import server as _server  # noqa: E402


def _bind_a_free_port() -> tuple[socket.socket, int]:
    """Bind a loopback socket and return ``(socket, port)`` (caller closes)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_serve_session_raises_synchronously_when_port_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy port raises from ``serve_session`` itself, never opens a browser.

    The bind probe runs on the calling thread before any daemon starts, so the
    collision propagates to the caller and no ``easycat-debugger`` thread is
    left running.
    """
    opened: list[str] = []
    monkeypatch.setattr(_server, "_open_browser", lambda url: opened.append(url))

    before = {t.name for t in threading.enumerate()}
    held, port = _bind_a_free_port()
    try:
        with pytest.raises(OSError):
            _server.serve_session(
                object(),
                host="127.0.0.1",
                port=port,
                open_browser=True,
                in_thread=True,
            )
    finally:
        held.close()

    # Bind failed before the thread started, so no tab was popped and no
    # debugger daemon thread leaked.
    assert opened == []
    after = {t.name for t in threading.enumerate()}
    assert "easycat-debugger" not in (after - before)


def test_serve_bundle_raises_synchronously_when_port_busy(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synchronous (non-threaded) serve path also fails before opening a tab."""
    # An empty directory is a valid loopback-only bundle path argument; the
    # probe-bind raises before the bundle is ever loaded or ``run_app`` runs.
    opened: list[str] = []
    monkeypatch.setattr(_server, "_open_browser", lambda url: opened.append(url))
    monkeypatch.setattr(_server, "_bundle_source", lambda _path: object())
    # ``run_app`` must never be reached when the probe-bind fails.
    import aiohttp.web

    monkeypatch.setattr(
        aiohttp.web,
        "run_app",
        lambda *a, **k: pytest.fail("run_app reached despite busy port"),
    )

    held, port = _bind_a_free_port()
    try:
        with pytest.raises(OSError):
            _server.serve_bundle(
                str(tmp_path),
                host="127.0.0.1",
                port=port,
                open_browser=True,
            )
    finally:
        held.close()

    assert opened == []
