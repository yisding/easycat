"""Architecture tests for debugger route ownership and shutdown."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("aiohttp")

from easycat.debugger.server import _DebuggerRoutes, _empty_dev_source, _make_app


def test_make_app_registers_controller_routes_and_shutdown_hook() -> None:
    app = _make_app(_empty_dev_source())

    registered = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/api/records") in registered
    assert ("POST", "/api/replay") in registered
    assert ("GET", "/ws") in registered
    assert not any(path.startswith("/api/dev/") for _method, path in registered)
    assert len(app.on_shutdown) == 1


@pytest.mark.asyncio
async def test_route_controller_closes_tracked_websockets_on_shutdown() -> None:
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
