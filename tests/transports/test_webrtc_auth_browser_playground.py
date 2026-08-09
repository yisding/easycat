"""WebRTC auth, browser event channel, and playground tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import easycat.transports.webrtc as webrtc_mod
from easycat.events import EventBus
from easycat.transports.webrtc import ICEServer, WebRTCTransport, WebRTCTransportConfig

from ._webrtc_fakes import (
    _HAS_WEBRTC_DEPS,
    _FakeAuthorizedOfferRequest,
    _FakeEventsChannel,
    _FakeJsonRequest,
    _FakeOfferRequest,
    _FakeQueryTokenStatsRequest,
    _FakeRTCPeerConnection,
    _FakeWeb,
    _install_fake_webrtc_modules,
    _UsesPytestTcpPortFactory,
)


class _FakeRootRequest:
    def __init__(self, query_string: str = "") -> None:
        self.query_string = query_string


class _FakeRootWeb(_FakeWeb):
    class HTTPFound(Exception):
        def __init__(self, location: str) -> None:
            super().__init__(location)
            self.location = location


class TestWebRTCBrowserEventChannel:
    """The browser-created "events" data channel carries session events."""

    @pytest.mark.asyncio
    async def test_offer_captures_events_data_channel(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeOfferRequest())
        assert response.status == 200

        pc = _FakeRTCPeerConnection.instances[-1]
        unrelated = _FakeEventsChannel()
        unrelated.label = "chat"
        pc._handlers["datachannel"](unrelated)
        assert transport._events_channel is None

        channel = _FakeEventsChannel()
        pc._handlers["datachannel"](channel)
        assert transport._events_channel is channel

    @pytest.mark.asyncio
    async def test_replacement_offer_drops_stale_events_channel(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True

        await transport._handle_offer(_FakeOfferRequest())
        stale_pc = _FakeRTCPeerConnection.instances[-1]
        stale_channel = _FakeEventsChannel()
        stale_pc._handlers["datachannel"](stale_channel)
        assert transport._events_channel is stale_channel

        await transport._handle_offer(_FakeOfferRequest())
        assert transport._events_channel is None

        # Stale-peer datachannel callbacks must not resurrect the slot.
        stale_pc._handlers["datachannel"](_FakeEventsChannel())
        assert transport._events_channel is None

    @pytest.mark.asyncio
    async def test_send_client_event_serializes_to_open_channel(self):
        transport = WebRTCTransport()
        channel = _FakeEventsChannel()
        transport._events_channel = channel

        await transport._send_client_event({"type": "stt_final", "text": "hi", "turn_id": "t1"})

        assert [json.loads(item) for item in channel.sent] == [
            {"type": "stt_final", "text": "hi", "turn_id": "t1"}
        ]

    @pytest.mark.asyncio
    async def test_send_client_event_noop_without_open_channel(self):
        transport = WebRTCTransport()

        await transport._send_client_event({"type": "stt_final", "text": "hi"})

        closed = _FakeEventsChannel(ready_state="closed")
        transport._events_channel = closed
        await transport._send_client_event({"type": "stt_final", "text": "hi"})
        assert closed.sent == []


class TestWebRTCAuthToken:
    """Optional shared-token auth mirroring the WebSocket/docker defaults."""

    @pytest.mark.asyncio
    async def test_offer_rejects_missing_token(self):
        transport = WebRTCTransport(WebRTCTransportConfig(auth_token="sekrit"))
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeOfferRequest())

        assert response.status == 401

    @pytest.mark.asyncio
    async def test_offer_accepts_bearer_token(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport(WebRTCTransportConfig(auth_token="sekrit"))
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeAuthorizedOfferRequest("sekrit"))

        assert response.status == 200

    @pytest.mark.asyncio
    async def test_offer_rejects_wrong_bearer_token(self):
        transport = WebRTCTransport(WebRTCTransportConfig(auth_token="sekrit"))
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeAuthorizedOfferRequest("wrong"))

        assert response.status == 401

    @pytest.mark.asyncio
    async def test_config_rejects_missing_token(self):
        transport = WebRTCTransport(WebRTCTransportConfig(auth_token="sekrit"))
        transport._web = _FakeWeb

        response = await transport._handle_config(_FakeOfferRequest())

        assert response.status == 401

    @pytest.mark.asyncio
    async def test_config_accepts_bearer_token_and_returns_ice_servers(self):
        transport = WebRTCTransport(
            WebRTCTransportConfig(
                auth_token="sekrit",
                ice_servers=[ICEServer(urls="stun:stun.example.com:3478")],
            )
        )
        transport._web = _FakeWeb

        response = await transport._handle_config(_FakeAuthorizedOfferRequest("sekrit"))

        assert response.status == 200
        assert json.loads(response.text) == {
            "iceServers": [{"urls": ["stun:stun.example.com:3478"]}]
        }

    @pytest.mark.asyncio
    async def test_stats_rejects_missing_token(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(auth_token="sekrit", stats_path=str(stats_path))
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(_FakeJsonRequest({"kind": "x"}))

        assert response.status == 401
        assert not stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_accepts_query_token_when_opted_in(self, tmp_path):
        # Query-token auth is now gated behind ``allow_query_token=True`` (the
        # default-off browser/dev opt-in). The bundled WebRTC client sends the
        # ``Authorization`` header and is unaffected.
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(
                auth_token="sekrit", allow_query_token=True, stats_path=str(stats_path)
            )
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeQueryTokenStatsRequest({"kind": "webrtc_client_stats"}, "sekrit")
        )

        assert response.status == 200
        assert stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_rejects_query_token_by_default(self, tmp_path):
        # Default-OFF: a correct ``?token=`` value is rejected because
        # ``allow_query_token`` defaults False. Only the Bearer header authorizes.
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(auth_token="sekrit", stats_path=str(stats_path))
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeQueryTokenStatsRequest({"kind": "webrtc_client_stats"}, "sekrit")
        )

        assert response.status == 401
        assert not stats_path.exists()

    def test_query_token_rejected_by_default_but_accepted_when_opted_in(self):
        rejecting = WebRTCTransport(WebRTCTransportConfig(auth_token="sekrit"))
        assert rejecting._request_authorized(_FakeQueryTokenStatsRequest({}, "sekrit")) is False

        accepting = WebRTCTransport(
            WebRTCTransportConfig(auth_token="sekrit", allow_query_token=True)
        )
        assert accepting._request_authorized(_FakeQueryTokenStatsRequest({}, "sekrit")) is True
        # A wrong query token is still rejected even when opted in.
        assert accepting._request_authorized(_FakeQueryTokenStatsRequest({}, "wrong")) is False

    def test_no_token_means_open_access(self):
        transport = WebRTCTransport()
        assert transport._request_authorized(_FakeOfferRequest()) is True

    @pytest.mark.asyncio
    async def test_root_redirect_drops_token_query_for_bundled_client(self):
        transport = WebRTCTransport()
        transport._web = _FakeRootWeb
        transport._has_bundled_client = True

        with pytest.raises(_FakeRootWeb.HTTPFound) as exc_info:
            await transport._handle_root(_FakeRootRequest("token=sekrit&view=compact"))

        assert exc_info.value.location == "/webrtc_client.html?view=compact"

    @pytest.mark.asyncio
    async def test_root_redirect_without_query_keeps_existing_location(self):
        transport = WebRTCTransport()
        transport._web = _FakeRootWeb
        transport._has_bundled_client = True

        with pytest.raises(_FakeRootWeb.HTTPFound) as exc_info:
            await transport._handle_root(_FakeRootRequest())

        assert exc_info.value.location == "/webrtc_client.html"


def test_bundled_client_renders_playground_ui():
    """Served-page smoke: transcript, interruption, latency, events channel."""
    html_path = Path(webrtc_mod.__file__).with_name("static") / "webrtc_client.html"
    html = html_path.read_text(encoding="utf-8")

    assert 'pc.createDataChannel("events")' in html
    assert 'id="transcript"' in html
    assert 'id="latency"' in html
    assert 'id="interruption"' in html
    assert "handleServerEvent" in html
    for message_type in (
        "stt_partial",
        "stt_final",
        "agent_delta",
        "agent_final",
        "turn_started",
        "interruption",
        "turn_latency",
    ):
        assert f'"{message_type}"' in html
    # Debugger UI link and token forwarding.
    assert 'id="debuggerLink"' in html
    assert 'new URLSearchParams(current.hash.replace(/^#/, ""))' in html
    assert 'current.searchParams.has("token")' in html
    assert 'current.searchParams.get("token")' not in html
    assert "Ignoring ?token= bootstrap" in html
    assert 'history.replaceState(null, "",' in html
    assert "authHeaders(" in html


@pytest.mark.integration_socket
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
class TestWebRTCServedPlaygroundPage(_UsesPytestTcpPortFactory):
    @pytest.mark.asyncio
    async def test_bundled_client_served_with_playground_widgets(self):
        import aiohttp

        port = self._unused_port()
        transport = WebRTCTransport(WebRTCTransportConfig(host="127.0.0.1", port=port))

        await transport.connect()
        try:
            async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
                async with session.get(f"http://127.0.0.1:{port}/webrtc_client.html") as resp:
                    assert resp.status == 200
                    html = await resp.text()
        finally:
            await transport.disconnect()

        assert 'pc.createDataChannel("events")' in html
        assert 'id="transcript"' in html
        assert 'id="latency"' in html
        assert 'id="interruption"' in html
        assert 'id="debuggerLink"' in html

    @pytest.mark.asyncio
    async def test_config_cors_preflight_allows_same_origin(self):
        import aiohttp

        port = self._unused_port()
        origin = f"http://127.0.0.1:{port}"
        transport = WebRTCTransport(WebRTCTransportConfig(host="127.0.0.1", port=port))

        await transport.connect()
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.options(
                    f"{origin}/config",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "GET",
                    },
                ) as resp,
            ):
                assert resp.status == 200
                assert resp.headers["Access-Control-Allow-Origin"] == origin
                assert "GET" in resp.headers["Access-Control-Allow-Methods"]
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wires_browser_event_forwarder_to_session_bus(self):
        from easycat.events import STTFinal

        port = self._unused_port()
        transport = WebRTCTransport(WebRTCTransportConfig(host="127.0.0.1", port=port))
        bus = EventBus()
        transport._event_bus = bus  # Session attaches the bus pre-connect.

        await transport.connect()
        try:
            channel = _FakeEventsChannel()
            transport._events_channel = channel
            await bus.emit(STTFinal(text="hello", turn_id="t1"))
            forwarder = transport._browser_event_forwarder
            assert forwarder is not None
            await asyncio.wait_for(forwarder._send_queue.join(), timeout=0.2)
            payloads = [json.loads(item) for item in channel.sent]
            assert payloads == [{"type": "stt_final", "text": "hello", "turn_id": "t1"}]
        finally:
            await transport.disconnect()

        # Teardown unsubscribes: further events are not forwarded.
        channel.sent.clear()
        await bus.emit(STTFinal(text="late", turn_id="t2"))
        assert channel.sent == []
