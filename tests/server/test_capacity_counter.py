"""Minimal-capacity-counter tests over the raw ``/ws`` listener.

These drive a real ``websockets`` client against the co-hosted raw-ws listener
and assert the M4 minimal counter increments on accept, decrements on close,
and rejects over-capacity / draining connections with the documented close
code.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from easycat.server import VoiceServer, VoiceServerConfig


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        self.stopped.set()


async def _running_server(config: VoiceServerConfig) -> tuple[VoiceServer, list[_FakeSession]]:
    sessions: list[_FakeSession] = []

    def session_factory(_transport: object) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server = VoiceServer(config, session_factory=session_factory)
    await server.start()
    return server, sessions


def _ws_url(server: VoiceServer) -> str:
    address = server.websocket_address
    assert address is not None
    host, port = address
    return f"ws://{host}:{port}"


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout=timeout)


@pytest.mark.integration_socket
async def test_counter_increments_on_accept_and_decrements_on_close() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4)
    )
    try:
        async with websockets.connect(_ws_url(server)):
            await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
            await _wait_until(lambda: server._active_sessions == 1)
        # On close the counter is decremented and the session stopped.
        await _wait_until(lambda: server._active_sessions == 0)
        await _wait_until(lambda: sessions[0].stopped.is_set())
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_over_capacity_connection_is_rejected() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=1)
    )
    try:
        async with websockets.connect(_ws_url(server)):
            await _wait_until(lambda: server._active_sessions == 1)

            async with websockets.connect(_ws_url(server)) as extra_client:
                await asyncio.wait_for(extra_client.wait_closed(), timeout=1)
                assert extra_client.close_code == 1013
                assert extra_client.close_reason == "Server is at the configured session limit"

            # The rejected client never created a second session.
            assert len(sessions) == 1
            assert server._active_sessions == 1
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_draining_rejects_new_connections() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4)
    )
    try:
        server._draining = True
        async with websockets.connect(_ws_url(server)) as client:
            await asyncio.wait_for(client.wait_closed(), timeout=1)
            assert client.close_code == 1013
            assert client.close_reason == "Server is draining"
        assert sessions == []
        assert server._active_sessions == 0
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_stop_closes_ws_listener_and_stops_active_sessions() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4)
    )
    async with websockets.connect(_ws_url(server)) as client:
        await _wait_until(lambda: server._active_sessions == 1)
        # ``stop`` closes the raw-ws listener and hard-sweeps the registry.
        await server.stop()
        await asyncio.wait_for(client.wait_closed(), timeout=1)
    assert server.websocket_address is None
    assert sessions[0].stopped.is_set()
