"""Health-endpoint tests driving a real aiohttp client against ``VoiceServer``.

These bind on port 0 (OS-assigned) and exercise ``/health/live``, ``/health``,
and ``/health/ready`` over HTTP, including the 503 paths for draining and
at-capacity.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest

from easycat.server import VoiceServer, VoiceServerConfig


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        self.stopped.set()


async def _running_server(config: VoiceServerConfig) -> VoiceServer:
    server = VoiceServer(config, session_factory=lambda _t: _FakeSession())
    await server.start()
    return server


def _base_url(server: VoiceServer) -> str:
    address = server.http_address
    assert address is not None
    host, port = address
    return f"http://{host}:{port}"


@pytest.fixture
async def client() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.mark.integration_socket
async def test_health_live_returns_200(client: aiohttp.ClientSession) -> None:
    server = await _running_server(VoiceServerConfig(host="127.0.0.1", port=0))
    try:
        async with client.get(f"{_base_url(server)}/health/live") as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body == {"status": "ok"}
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_health_returns_stable_json_shape(client: aiohttp.ClientSession) -> None:
    server = await _running_server(VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=64))
    try:
        async with client.get(f"{_base_url(server)}/health") as resp:
            assert resp.status == 200
            body = await resp.json()
        assert set(body) == {
            "status",
            "state",
            "active_sessions",
            "max_sessions",
            "draining",
            "checks",
        }
        assert body["status"] == "ok"
        assert body["state"] == "serving"
        assert body["active_sessions"] == 0
        assert body["max_sessions"] == 64
        assert body["draining"] is False
        assert set(body["checks"]) == {"manifest", "providers", "sessions"}
        # M6b-deferred sub-checks report the static placeholder in M4.
        assert body["checks"]["manifest"] == {"status": "skipped"}
        assert body["checks"]["providers"] == {"status": "skipped"}
        assert body["checks"]["sessions"] == {"status": "ok"}
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_health_ready_returns_200_when_idle(client: aiohttp.ClientSession) -> None:
    server = await _running_server(VoiceServerConfig(host="127.0.0.1", port=0))
    try:
        async with client.get(f"{_base_url(server)}/health/ready") as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_health_ready_returns_503_when_draining(client: aiohttp.ClientSession) -> None:
    server = await _running_server(VoiceServerConfig(host="127.0.0.1", port=0))
    try:
        url = f"{_base_url(server)}/health/ready"
        # Flip the draining flag without tearing the listeners down so the HTTP
        # endpoint still answers (and now reports 503).
        server._draining = True
        async with client.get(url) as resp:
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "not_ready"
            assert "draining" in body["reasons"]
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_health_ready_returns_503_at_capacity(client: aiohttp.ClientSession) -> None:
    server = await _running_server(VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=1))
    try:
        url = f"{_base_url(server)}/health/ready"
        # Occupy the single slot via the minimal counter.
        assert await server._try_acquire_slot() is True
        async with client.get(url) as resp:
            assert resp.status == 503
            body = await resp.json()
            assert "at_capacity" in body["reasons"]
        # Releasing the slot restores readiness.
        await server._release_slot()
        async with client.get(url) as resp:
            assert resp.status == 200
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_health_body_never_leaks_session_ids_or_tokens(
    client: aiohttp.ClientSession,
) -> None:
    server = await _running_server(VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=1))
    try:
        await server._try_acquire_slot()
        async with client.get(f"{_base_url(server)}/health/ready") as resp:
            text = await resp.text()
        # Precondition: we actually exercised the at-capacity not-ready path
        # (so the leak assertions below run against a populated reasons body).
        assert "at_capacity" in text
        # The readiness reasons are content-free tokens only — never an auth
        # token value and never raw socket/host detail.
        assert "token" not in text.lower()
        assert "127.0.0.1" not in text
    finally:
        await server.stop()
