"""Architecture tests for debugger route ownership and shutdown."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("aiohttp")

from easycat.debugger.server import _DebuggerRoutes, _empty_dev_source, _make_app


def test_make_app_registers_controller_routes_and_shutdown_hook() -> None:
    """The base app exposes exactly the non-development HTTP contract."""
    app = _make_app(_empty_dev_source())

    registered = {(route.method, route.resource.canonical) for route in app.router.routes()}
    get_paths = {
        "/",
        "/api/aec/{turn}",
        "/api/annotations",
        "/api/artifact/{ref}",
        "/api/audio/concat/{turn}",
        "/api/audio/waveform/{turn}",
        "/api/health",
        "/api/issues",
        "/api/manifest",
        "/api/records",
        "/api/refresh",
        "/api/timeline",
        "/api/transcript",
        "/api/turns",
        "/static",
        "/ws",
    }
    expected = {(method, path) for path in get_paths for method in ("GET", "HEAD")}
    expected.update(
        {
            ("POST", "/api/aec/{turn}/vad-whatif"),
            ("POST", "/api/annotate"),
            ("POST", "/api/export"),
            ("POST", "/api/replay"),
        }
    )

    assert registered == expected
    assert len(app.on_shutdown) == 1


@pytest.mark.asyncio
async def test_route_controller_closes_tracked_websockets_on_shutdown() -> None:
    """Application shutdown closes and forgets every tracked WebSocket."""
    from aiohttp import WSMsgType, web

    closed: list[tuple[int, bytes]] = []

    class _Socket:
        async def close(self, *, code: int, message: bytes) -> None:
            closed.append((code, message))

    routes = _DebuggerRoutes(
        _empty_dev_source(),
        web=web,
        ws_msg_type=WSMsgType,
        allow_remote=False,
        registry=None,
    )
    sockets: set[Any] = {_Socket(), _Socket()}
    routes.websockets.update(sockets)

    await routes.shutdown(None)

    assert closed == [(1001, b"server shutdown"), (1001, b"server shutdown")]
    assert routes.websockets == set()


@pytest.mark.asyncio
async def test_route_controller_reports_and_retains_websocket_close_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from aiohttp import WSMsgType, web

    class _FailingSocket:
        async def close(self, *, code: int, message: bytes) -> None:
            raise RuntimeError("close failed")

    routes = _DebuggerRoutes(
        _empty_dev_source(),
        web=web,
        ws_msg_type=WSMsgType,
        allow_remote=False,
        registry=None,
    )
    socket = _FailingSocket()
    routes.websockets.add(socket)

    with caplog.at_level("ERROR", logger="easycat.debugger.server"):
        await routes.shutdown(None)

    assert routes.websockets == {socket}
    assert "Debugger WebSocket close failed" in caplog.text
    assert "close failed" in caplog.text
