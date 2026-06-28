"""Authorization regression tests for the ``/plan`` route handler."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from easycat.server import BearerTokenAuth, VoiceServer, VoiceServerConfig
from easycat.server.routes import register_plan_route


class _FakeWeb(ModuleType):
    @staticmethod
    def json_response(payload: dict[str, Any], *, status: int = 200) -> SimpleNamespace:
        return SimpleNamespace(payload=payload, status=status)


class _FakeRouter:
    def __init__(self) -> None:
        self.handler: Any | None = None

    def add_get(self, path: str, handler: Any) -> None:
        assert path == "/plan"
        self.handler = handler


class _FakeApp:
    def __init__(self) -> None:
        self.router = _FakeRouter()


class _FakeSession:
    async def start(self) -> None:
        pass

    async def stop(self, *, force: bool = False) -> None:
        pass


class _Request:
    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {}
        if authorization is not None:
            self.headers["Authorization"] = authorization
        self.query = {}


def _install_fake_aiohttp(monkeypatch: pytest.MonkeyPatch) -> None:
    aiohttp = ModuleType("aiohttp")
    aiohttp.web = _FakeWeb("aiohttp.web")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", aiohttp.web)


def _plan_handler() -> Any:
    server = VoiceServer(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            auth=BearerTokenAuth("secret-token"),
        ),
        session_factory=lambda _transport: _FakeSession(),
    )
    app = _FakeApp()
    register_plan_route(app, server)
    assert app.router.handler is not None
    return app.router.handler


@pytest.mark.asyncio
async def test_plan_route_rejects_missing_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_aiohttp(monkeypatch)
    response = await _plan_handler()(_Request())
    assert response.status == 401
    assert response.payload == {"error": "Missing or invalid bearer token"}


@pytest.mark.asyncio
async def test_plan_route_allows_valid_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_aiohttp(monkeypatch)
    response = await _plan_handler()(_Request("Bearer secret-token"))
    assert response.status == 200
    assert response.payload["selected"] == {}
