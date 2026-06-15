"""M8 read-only endpoint tests: ``GET /metrics``, ``/manifest``, ``/capabilities``.

These bind a real aiohttp listener on an ephemeral port and assert each endpoint
returns 200 with the documented key set for BOTH a factory-only server and a
``from_manifest`` server, that ``/manifest`` and ``/plan`` never echo a resolved
token, and that ``/capabilities`` from a manifest exposes the seven roles'
declared capability strings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from easycat.server import VoiceServer, VoiceServerConfig


class _FakeSession:
    async def start(self) -> None:  # noqa: D401 - test stub
        pass

    async def stop(self, *, force: bool = False) -> None:  # noqa: D401 - test stub
        pass


def _write_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        "\n".join(
            [
                "[project]",
                'name = "metrics-endpoint-test"',
                "",
                "[server]",
                'host = "127.0.0.1"',
                "port = 0",
                'auth = "bearer-env:EASYCAT_SERVE_TOKEN"',
                "",
                "[voice.default]",
                'transport = "webrtc"',
                'stt = "openai/realtime"',
                'tts = "openai"',
                'vad = "silero"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
async def client() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def _base_url(server: VoiceServer) -> str:
    address = server.http_address
    assert address is not None
    host, port = address
    return f"http://{host}:{port}"


def _factory_only_server() -> VoiceServer:
    return VoiceServer(
        VoiceServerConfig(host="127.0.0.1", port=0),
        session_factory=lambda _t: _FakeSession(),
    )


def _manifest_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VoiceServer:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path))
    server.config.port = 0
    return server


# ── /metrics ─────────────────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_metrics_endpoint_shape_factory_only(client: aiohttp.ClientSession) -> None:
    server = _factory_only_server()
    await server.start()
    try:
        # The middleware counts a request AFTER the handler returns, so the very
        # first /metrics read reports 0 for itself. Hit /health first so the
        # snapshot reflects at least one completed request.
        async with client.get(f"{_base_url(server)}/health/live") as resp:
            assert resp.status == 200
        async with client.get(f"{_base_url(server)}/metrics") as resp:
            assert resp.status == 200
            body = await resp.json()
        assert set(body) == {
            "active_sessions",
            "max_sessions",
            "draining",
            "requests_total",
            "sessions_rejected_total",
        }
        assert body["active_sessions"] == 0
        assert body["draining"] is False
        # The /health/live request was counted by the middleware.
        assert body["requests_total"] >= 1
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_metrics_endpoint_shape_from_manifest(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _manifest_server(tmp_path, monkeypatch)
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/metrics") as resp:
            assert resp.status == 200
            body = await resp.json()
        assert set(body) == {
            "active_sessions",
            "max_sessions",
            "draining",
            "requests_total",
            "sessions_rejected_total",
        }
        # No resolved token can appear in a metrics snapshot.
        async with client.get(f"{_base_url(server)}/metrics") as resp:
            assert "tok" not in await resp.text()
    finally:
        await server.stop()


# ── /manifest ────────────────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_manifest_endpoint_absent_for_factory_only(client: aiohttp.ClientSession) -> None:
    server = _factory_only_server()
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/manifest") as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body == {"loaded": False, "manifest": None}
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_manifest_endpoint_from_manifest_has_no_token(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _manifest_server(tmp_path, monkeypatch)
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/manifest") as resp:
            assert resp.status == 200
            body = await resp.json()
            text = await resp.text()
        assert body["loaded"] is True
        manifest = body["manifest"]
        # The redacted dump exposes only the bearer-env:NAME reference.
        assert manifest["server"]["auth_ref"] == "bearer-env:EASYCAT_SERVE_TOKEN"
        # The resolved token must never appear in the dump.
        assert "tok" not in text
    finally:
        await server.stop()


# ── /plan never leaks the token (M8 re-assertion over the socket) ─────


@pytest.mark.integration_socket
async def test_plan_endpoint_has_no_token(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _manifest_server(tmp_path, monkeypatch)
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/plan") as resp:
            assert resp.status == 200
            assert "tok" not in await resp.text()
    finally:
        await server.stop()


# ── /capabilities ────────────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_capabilities_endpoint_empty_for_factory_only(
    client: aiohttp.ClientSession,
) -> None:
    server = _factory_only_server()
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/capabilities") as resp:
            assert resp.status == 200
            body = await resp.json()
        assert set(body) == {"profile", "roles", "all_capabilities"}
        assert body["roles"] == {}
        assert body["all_capabilities"] == []
        assert body["profile"] == "default"
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_capabilities_endpoint_from_manifest_lists_seven_roles(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _manifest_server(tmp_path, monkeypatch)
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/capabilities") as resp:
            assert resp.status == 200
            body = await resp.json()
            text = await resp.text()
        assert set(body) == {"profile", "roles", "all_capabilities"}
        assert set(body["roles"]) == {
            "stt",
            "tts",
            "vad",
            "transport",
            "agent",
            "noise_reducer",
            "echo_canceller",
        }
        # The webrtc transport declares its capability strings; the union surfaces
        # them. (stt/tts are catalog roles with no static capabilities.)
        assert "browser" in body["all_capabilities"]
        assert "duplex_audio" in body["all_capabilities"]
        assert "browser" in body["roles"]["transport"]
        # The planner reads only metadata, never secret values.
        assert "tok" not in text
    finally:
        await server.stop()
