"""WebRTC transport tests.

Tests cover:
  - WebRTCTransportConfig defaults and ICE server configuration
  - Transport protocol conformance (has required methods)
  - Connect/disconnect lifecycle
  - Signaling HTTP endpoints (health, config, offer)
  - Inbound/outbound audio flow (with mocked aiortc)
  - Outbound audio source frame generation and remainder handling
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

import easycat.transports.webrtc as webrtc_mod
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import EventBus, TransportDegraded
from easycat.transports.webrtc import (
    _DEGRADED_INBOUND_CONSUME_ERROR,
    _DEGRADED_NEGOTIATION_FAILED,
    _DEGRADED_OUTBOUND_QUEUE_FULL,
    ICEServer,
    WebRTCTransport,
    WebRTCTransportConfig,
    _OutboundAudioSource,
)

from .conftest import find_free_port, make_chunk

# Whether aiortc + aiohttp are available (needed for integration tests).
_HAS_AIORTC = importlib.util.find_spec("aiortc") is not None
_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
_HAS_WEBRTC_DEPS = _HAS_AIORTC and _HAS_AIOHTTP


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "",
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.text = text
        self.content_type = content_type
        self.headers = headers or {}


class _FakeWeb:
    Response = _FakeResponse


class _FakeOfferRequest:
    async def json(self) -> dict[str, str]:
        return {"sdp": "v=0\r\n", "type": "offer"}


class _FakeSessionDescription:
    def __init__(self, *, sdp: str, type: str) -> None:  # noqa: A002
        self.sdp = sdp
        self.type = type


class _FakeRTCConfiguration:
    def __init__(self, *, iceServers: list[object]) -> None:  # noqa: N803
        self.iceServers = iceServers


class _FakeRTCIceServer:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeMediaStreamTrack:
    def __init__(self) -> None:
        pass


class _FakeMediaStreamError(Exception):
    pass


class _FakeRTCPeerConnection:
    instances: list[_FakeRTCPeerConnection] = []

    def __init__(self, config: _FakeRTCConfiguration) -> None:
        self.config = config
        self.connectionState = "new"
        self.iceGatheringState = "complete"
        self.localDescription: _FakeSessionDescription | None = None
        self.remoteDescription: _FakeSessionDescription | None = None
        self.closed = False
        self.tracks: list[object] = []
        self._handlers: dict[str, object] = {}
        self.instances.append(self)

    def addTrack(self, track: object) -> None:  # noqa: N802
        self.tracks.append(track)

    def on(self, event: str):
        def decorator(callback):
            self._handlers[event] = callback
            return callback

        return decorator

    async def setRemoteDescription(self, offer: _FakeSessionDescription) -> None:  # noqa: N802
        self.remoteDescription = offer

    async def createAnswer(self) -> _FakeSessionDescription:  # noqa: N802
        return _FakeSessionDescription(sdp="fake-answer", type="answer")

    async def setLocalDescription(self, answer: _FakeSessionDescription) -> None:  # noqa: N802
        self.localDescription = answer

    async def close(self) -> None:
        self.closed = True
        self.connectionState = "closed"
        callback = self._handlers.get("connectionstatechange")
        if callback is not None:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        await asyncio.sleep(0)


class _FakeAiortc:
    MediaStreamError = _FakeMediaStreamError
    MediaStreamTrack = _FakeMediaStreamTrack
    RTCConfiguration = _FakeRTCConfiguration
    RTCIceServer = _FakeRTCIceServer
    RTCPeerConnection = _FakeRTCPeerConnection
    RTCSessionDescription = _FakeSessionDescription


def _install_fake_webrtc_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRTCPeerConnection.instances.clear()

    def fake_require_module(name: str, **_: object) -> object:
        if name == "aiortc":
            return _FakeAiortc
        raise AssertionError(f"unexpected module request: {name}")

    monkeypatch.setattr(webrtc_mod, "require_module", fake_require_module)


# ── Config tests ──────────────────────────────────────────────────


class TestWebRTCTransportConfig:
    def test_defaults(self):
        config = WebRTCTransportConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8080
        assert config.audio_format == PCM16_MONO_16K
        assert config.max_pending_chunks == 200
        assert config.static_dir == WebRTCTransportConfig._USE_BUNDLED
        assert len(config.ice_servers) == 1
        assert "stun:" in config.ice_servers[0].urls[0]

    def test_custom_ice_servers(self):
        servers = [
            ICEServer(urls="stun:stun.example.com:3478"),
            ICEServer(
                urls=["turn:turn.example.com:3478", "turns:turn.example.com:5349"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(ice_servers=servers)
        assert len(config.ice_servers) == 2
        assert config.ice_servers[1].username == "user"
        assert config.ice_servers[1].credential == "pass"

    def test_ice_server_single_url_normalized_to_list(self):
        srv = ICEServer(urls="stun:stun.l.google.com:19302")
        assert srv.urls == ["stun:stun.l.google.com:19302"]
        assert srv.username is None
        assert srv.credential is None

    def test_ice_server_multiple_urls(self):
        srv = ICEServer(urls=["turn:a.example.com:3478", "turn:b.example.com:3478"])
        assert isinstance(srv.urls, list)
        assert len(srv.urls) == 2


# ── Protocol conformance tests ───────────────────────────────────


class TestWebRTCTransportConformance:
    def test_has_protocol_methods(self):
        t = WebRTCTransport()
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    def test_is_transport_protocol(self):
        from easycat.providers import Transport

        t = WebRTCTransport()
        assert isinstance(t, Transport)

    def test_initial_state(self):
        t = WebRTCTransport()
        assert not t.is_connected
        assert not t.has_client

    def test_echo_cancellation_default_is_on(self):
        # Deliberate flip from the prior implicit ``False`` default: WebRTC is a
        # browser-mic transport and matches WebSocket's EasyCat-side AEC default.
        from easycat.runtime.capabilities import default_echo_cancellation_enabled

        assert default_echo_cancellation_enabled(WebRTCTransport()) is True


class TestWebRTCIngressQueueOwnership:
    @pytest.mark.asyncio
    async def test_repeated_offer_keeps_active_receive_audio_on_same_queue(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        # The signaling server is live (an /offer can only reach the handler
        # once connect() has started it); offers received after teardown begins
        # are rejected with 503 instead.
        transport._connected = True
        original_queue = transport._in_queue

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        first_response = await transport._handle_offer(_FakeOfferRequest())
        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert first_response.status == 200
        assert second_response.status == 200
        assert transport._in_queue is original_queue
        await asyncio.sleep(0)
        assert not pending.done()

        new_chunk = make_chunk(8)
        transport._enqueue_chunk(new_chunk, context="test")
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_repeated_offer_drains_stale_audio_without_replacing_queue(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see test above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200

        original_queue = transport._in_queue
        stale_chunk = make_chunk(8)
        transport._enqueue_chunk(stale_chunk, context="test")
        transport._enqueue_sentinel()

        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert second_response.status == 200
        assert transport._in_queue is original_queue
        assert transport._in_queue.empty()

        new_chunk = make_chunk(10)
        transport._enqueue_chunk(new_chunk, context="test")
        audio_iter = transport.receive_audio()
        received = await asyncio.wait_for(anext(audio_iter), timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_disconnect_does_not_hold_offer_lock_during_http_cleanup(self):
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        offer_task: asyncio.Task[object] | None = None

        class _OfferDuringStopSite:
            async def stop(self) -> None:
                nonlocal offer_task
                offer_task = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
                await asyncio.sleep(0)

        class _CleanupWaitsForHandlersRunner:
            async def cleanup(self) -> None:
                assert offer_task is not None
                response = await asyncio.wait_for(offer_task, timeout=1.0)
                assert response.status == 503

        transport._site = _OfferDuringStopSite()
        transport._runner = _CleanupWaitsForHandlersRunner()

        await asyncio.wait_for(transport.disconnect(), timeout=1.0)

        assert transport._site is None
        assert transport._runner is None
        assert offer_task is not None
        assert offer_task.done()

    @pytest.mark.asyncio
    async def test_replacing_connected_peer_clears_wait_for_client(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see test above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200
        first_pc = _FakeRTCPeerConnection.instances[0]
        first_pc.connectionState = "connected"
        first_connected = first_pc._handlers["connectionstatechange"]()
        if asyncio.iscoroutine(first_connected):
            await first_connected
        assert transport.has_client
        assert transport._client_connected.is_set()

        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert second_response.status == 200
        assert first_pc.closed
        assert not transport.has_client
        assert not transport._client_connected.is_set()

        second_pc = _FakeRTCPeerConnection.instances[1]
        second_pc.connectionState = "connected"
        second_connected = second_pc._handlers["connectionstatechange"]()
        if asyncio.iscoroutine(second_connected):
            await second_connected
        assert transport.has_client
        assert transport._client_connected.is_set()


# ── Lifecycle tests (require aiohttp) ────────────────────────────


@pytest.mark.integration_socket
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
class TestWebRTCTransportLifecycle:
    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        assert transport.is_connected

        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        transport = WebRTCTransport()
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        await transport.connect()  # Should not raise.
        assert transport.is_connected

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_redirects_to_bundled_client_when_present(self, tmp_path):
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/", allow_redirects=False) as resp:
                assert resp.status == 302
                assert resp.headers["Location"] == "/webrtc_client.html"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_returns_endpoint_hint_without_static_client(self):
        import aiohttp

        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "easycat-webrtc-signaling"
                assert "/offer" in data["endpoints"]
                assert "Access-Control-Allow-Origin" in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_failed_connect_does_not_leave_stale_bundled_client_state(
        self,
        tmp_path,
        monkeypatch,
    ):
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)

        async def broken_start(_self):
            raise RuntimeError("port busy")

        monkeypatch.setattr(aiohttp.web.TCPSite, "start", broken_start)

        with pytest.raises(RuntimeError, match="port busy"):
            await transport.connect()

        monkeypatch.undo()

        assert transport._has_bundled_client is False
        assert transport._app is None
        assert transport._runner is None
        assert transport._site is None

        # Retry on same instance without static files should not keep stale
        # redirect behavior from the failed attempt.
        transport._config.static_dir = None
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "easycat-webrtc-signaling"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        import aiohttp

        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                # Verify CORS headers are present.
                assert "Access-Control-Allow-Origin" in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_without_valid_sdp_returns_error(self):
        import aiohttp

        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            # Send invalid JSON.
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                data="not json",
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 400

            # Send valid JSON but invalid schema.
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json={"type": "answer", "sdp": "dummy"},
            ) as resp:
                assert resp.status == 400

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_endpoint_omits_turn_credentials(self):
        import aiohttp

        port = find_free_port()
        servers = [
            ICEServer(urls="stun:stun.example.com:3478"),
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, ice_servers=servers)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/config") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert "iceServers" in data
                assert len(data["iceServers"]) == 2
                # Public config should include URLs but must not leak TURN credentials.
                turn = data["iceServers"][1]
                assert turn["urls"] == ["turn:turn.example.com:3478"]
                assert "username" not in turn
                assert "credential" not in turn

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_uses_full_ice_credentials_for_server_peer(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        servers = [
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            )
        ]
        transport = WebRTCTransport(WebRTCTransportConfig(ice_servers=servers))
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeOfferRequest())

        assert response.status == 200
        pc = _FakeRTCPeerConnection.instances[0]
        assert pc.config.iceServers[0].kwargs == {
            "urls": ["turn:turn.example.com:3478"],
            "username": "user",
            "credential": "pass",
        }

    @pytest.mark.asyncio
    async def test_cors_preflight(self):
        import aiohttp

        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.options(f"http://127.0.0.1:{port}/offer") as resp:
                assert resp.status == 200
                assert "Access-Control-Allow-Origin" in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_receive_audio_ends_on_disconnect(self):
        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                chunks.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await transport.disconnect()
        await asyncio.wait_for(task, timeout=2.0)
        # Should have exited cleanly.

    @pytest.mark.asyncio
    async def test_send_audio_no_peer(self):
        """send_audio reports False when no peer is connected."""
        port = find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunk = make_chunk()
        delivered = await transport.send_audio(chunk)
        assert delivered is False

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_reports_drop_after_peer_disconnect(self):
        """After the peer connection drops, send_audio must return False so
        the session stops emitting AudioOut for audio no one will hear."""
        transport = WebRTCTransport()
        # Pretend a peer connected: populate the fields that gate send_audio.
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()

        chunk = make_chunk()
        # With a live track, send_audio accepts the chunk.
        delivered_while_live = await transport.send_audio(chunk)
        assert delivered_while_live is True

        # Simulate the connectionstatechange handler's "disconnected" branch.
        transport._outbound_track = None

        delivered_after_drop = await transport.send_audio(chunk)
        assert delivered_after_drop is False


# ── Outbound audio source tests ──────────────────────────────────


class TestOutboundAudioSource:
    def test_enqueue_and_drain(self):
        source = _OutboundAudioSource()
        data = bytes(960 * 2)  # 20ms at 48kHz mono s16
        source.enqueue(data, original_chunk=AudioChunk(data=data, format=PCM16_MONO_16K))
        assert not source._queue.empty()

    def test_enqueue_overflow(self):
        source = _OutboundAudioSource()
        source._queue = asyncio.Queue(maxsize=2)
        chunk = AudioChunk(data=bytes(100), format=PCM16_MONO_16K)
        # Fill queue.
        assert source.enqueue(bytes(100), original_chunk=chunk) is True
        assert source.enqueue(bytes(100), original_chunk=chunk) is True
        # Overflow — should not raise, and should report dropped frame.
        assert source.enqueue(bytes(100), original_chunk=chunk) is False

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_produces_silence_when_empty(self):
        source = _OutboundAudioSource()
        frame = await source._recv()
        assert frame.sample_rate == 48000
        assert frame.samples == 960
        # Frame data should be all zeros (silence).
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_returns_enqueued_data(self):
        source = _OutboundAudioSource()
        # Enqueue one frame of non-silent data.
        test_data = bytes(range(256)) * (960 * 2 // 256 + 1)
        test_data = test_data[: 960 * 2]
        source.enqueue(test_data, original_chunk=AudioChunk(data=test_data, format=PCM16_MONO_16K))

        frame = await source._recv()
        actual = bytes(frame.planes[0])
        assert actual == test_data

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_preserves_audio_order_with_remainder(self):
        """Verify that audio chunks larger than one frame don't reorder."""
        source = _OutboundAudioSource()
        frame_bytes = 960 * 2  # one 20ms frame at 48kHz mono s16

        # Create chunk A (1.5 frames) and chunk B (1 frame).
        chunk_a = bytes([0xAA]) * (frame_bytes + frame_bytes // 2)
        chunk_b = bytes([0xBB]) * frame_bytes
        source.enqueue(chunk_a, original_chunk=AudioChunk(data=chunk_a, format=PCM16_MONO_16K))
        source.enqueue(chunk_b, original_chunk=AudioChunk(data=chunk_b, format=PCM16_MONO_16K))

        # Frame 1: first frame of A.
        frame1 = await source._recv()
        data1 = bytes(frame1.planes[0])
        assert data1 == bytes([0xAA]) * frame_bytes

        # Frame 2: remainder of A (half frame) + start of B (half frame).
        frame2 = await source._recv()
        data2 = bytes(frame2.planes[0])
        expected = bytes([0xAA]) * (frame_bytes // 2) + bytes([0xBB]) * (frame_bytes // 2)
        assert data2 == expected

        # Frame 3: remainder of B (half frame) + silence padding.
        frame3 = await source._recv()
        data3 = bytes(frame3.planes[0])
        expected3 = bytes([0xBB]) * (frame_bytes // 2) + bytes(frame_bytes // 2)
        assert data3 == expected3

    def test_clear_discards_queued_data(self):
        source = _OutboundAudioSource()
        chunk = AudioChunk(data=bytes(200), format=PCM16_MONO_16K)
        source.enqueue(bytes(100), original_chunk=chunk)
        source.enqueue(bytes(200), original_chunk=chunk)
        source._pending.append(source._queue.get_nowait())

        source.clear()

        assert source._queue.empty()
        assert not source._pending

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_clear_then_recv_produces_silence(self):
        source = _OutboundAudioSource()
        test_data = bytes([0xFF]) * 960 * 2
        source.enqueue(
            test_data,
            original_chunk=AudioChunk(data=test_data, format=PCM16_MONO_16K),
        )
        source.clear()

        frame = await source._recv()
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)  # silence


# ── Consume-audio sentinel tests ─────────────────────────────────


class TestConsumeAudioSentinel:
    """Verify that _consume_audio enqueues a sentinel when the track ends."""

    @pytest.mark.asyncio
    async def test_track_recv_raises_stops_receive_audio(self):
        """When track.recv() raises, _consume_audio's finally block enqueues
        a sentinel so that receive_audio() terminates instead of blocking."""
        transport = WebRTCTransport(WebRTCTransportConfig())
        transport._init_audio_queue(200)
        transport._connected = True

        # Fake track whose recv() signals end-of-stream immediately.
        class _FakeTrack:
            async def recv(self):
                raise StopAsyncIteration

        # Run _consume_audio — it should enqueue a sentinel via the finally block.
        await transport._consume_audio(_FakeTrack())

        # receive_audio() should now terminate promptly.
        chunks: list[AudioChunk] = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_sentinel_delivered_when_queue_is_full(self):
        """Even when the inbound queue is full, the sentinel must be delivered
        so that receive_audio() does not block forever."""
        transport = WebRTCTransport(WebRTCTransportConfig(max_pending_chunks=2))
        transport._init_audio_queue(2)
        transport._connected = True

        # Fill the queue completely.
        for _ in range(2):
            transport._enqueue_chunk(make_chunk(), context="test")

        # Fake track that ends immediately.
        class _FakeTrack:
            async def recv(self):
                raise StopAsyncIteration

        await transport._consume_audio(_FakeTrack())

        # receive_audio() must still terminate (sentinel was force-enqueued).
        chunks: list[AudioChunk] = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        # One chunk was dropped to make room for the sentinel; at most 1 chunk.
        assert len(chunks) <= 2


# ── Journal integration: TransportDegraded emission ───────────────


class TestWebRTCDegradedEvents:
    """SDP negotiation failure and inbound-track crash must surface a
    ``TransportDegraded`` so they land in the journal, not just the log."""

    @pytest.mark.asyncio
    async def test_negotiation_failure_emits_fatal(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)

        async def _boom(self) -> None:  # noqa: ANN001
            raise RuntimeError("sdp boom")

        monkeypatch.setattr(_FakeRTCPeerConnection, "createAnswer", _boom)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see ingress tests)
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        resp = await transport._handle_offer(_FakeOfferRequest())

        assert resp.status == 400
        for _ in range(5):
            await asyncio.sleep(0)
        assert [e.reason for e in received] == [_DEGRADED_NEGOTIATION_FAILED]
        assert received[0].provider == "webrtc"
        assert received[0].fatal is True

    @pytest.mark.asyncio
    async def test_inbound_consume_error_emits_degraded(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        class _BadTrack:
            async def recv(self):
                raise RuntimeError("decode boom")

        await transport._consume_audio(_BadTrack(), peer_generation=transport._peer_generation)

        for _ in range(5):
            await asyncio.sleep(0)
        evt = next(e for e in received if e.reason == _DEGRADED_INBOUND_CONSUME_ERROR)
        assert evt.provider == "webrtc"
        assert evt.fatal is False

    @pytest.mark.asyncio
    async def test_outbound_queue_full_emits_degraded(self):
        """A dropped outbound TTS frame must surface a ``TransportDegraded`` so
        backpressure is visible in the journal, not just a logger.debug line."""
        transport = WebRTCTransport()
        # Pretend a peer connected so send_audio reaches the enqueue path.
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        # Force the outbound source to always reject the frame as if full.
        transport._outbound.enqueue = lambda *a, **k: False  # type: ignore[method-assign]

        delivered = await transport.send_audio(make_chunk())
        assert delivered is False

        for _ in range(5):
            await asyncio.sleep(0)
        evt = next(e for e in received if e.reason == _DEGRADED_OUTBOUND_QUEUE_FULL)
        assert evt.provider == "webrtc"
        assert evt.fatal is False
