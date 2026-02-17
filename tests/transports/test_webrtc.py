"""WebRTC transport tests.

Tests cover:
  - WebRTCTransportConfig defaults and ICE server configuration
  - Transport protocol conformance (has required methods)
  - Connect/disconnect lifecycle
  - Signaling HTTP endpoints (health, config, offer)
  - Inbound/outbound audio flow (with mocked aiortc)
  - Outbound audio track frame generation and remainder handling
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.transports.webrtc import (
    ICEServer,
    WebRTCTransport,
    WebRTCTransportConfig,
    _OutboundAudioTrack,
)

# Whether aiortc + aiohttp are available (needed for integration tests).
_HAS_AIORTC = importlib.util.find_spec("aiortc") is not None
_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
_HAS_WEBRTC_DEPS = _HAS_AIORTC and _HAS_AIOHTTP


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_chunk(n_bytes: int = 320, sample_rate: int = 16000) -> AudioChunk:
    """Create a test audio chunk of silence."""
    fmt = AudioFormat(sample_rate=sample_rate, channels=1, sample_width=2)
    return AudioChunk(data=bytes(n_bytes), format=fmt)


# ── Config tests ──────────────────────────────────────────────────


class TestWebRTCTransportConfig:
    def test_defaults(self):
        config = WebRTCTransportConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8080
        assert config.audio_format == PCM16_MONO_16K
        assert config.max_pending_chunks == 200
        assert config.static_dir is None
        assert len(config.ice_servers) == 1
        assert "stun:" in config.ice_servers[0].urls

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

    def test_ice_server_single_url(self):
        srv = ICEServer(urls="stun:stun.l.google.com:19302")
        assert srv.urls == "stun:stun.l.google.com:19302"
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

    def test_is_transport_protocol(self):
        from easycat.providers import Transport

        t = WebRTCTransport()
        assert isinstance(t, Transport)

    def test_initial_state(self):
        t = WebRTCTransport()
        assert not t.is_connected
        assert not t.has_client


# ── Lifecycle tests (require aiohttp) ────────────────────────────


@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
class TestWebRTCTransportLifecycle:
    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = _find_free_port()
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
        port = _find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        await transport.connect()  # Should not raise.
        assert transport.is_connected

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        import aiohttp

        port = _find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_without_valid_sdp_returns_error(self):
        import aiohttp

        port = _find_free_port()
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

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_endpoint(self):
        import aiohttp

        port = _find_free_port()
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
                # TURN server should include credentials.
                turn = data["iceServers"][1]
                assert turn["username"] == "user"
                assert turn["credential"] == "pass"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_preflight(self):
        import aiohttp

        port = _find_free_port()
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
        port = _find_free_port()
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
        """send_audio is a no-op when no peer is connected."""
        port = _find_free_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunk = _make_chunk()
        await transport.send_audio(chunk)  # Should not raise.

        await transport.disconnect()


# ── Outbound audio track tests ───────────────────────────────────


class TestOutboundAudioTrack:
    def test_enqueue_and_drain(self):
        track = _OutboundAudioTrack()
        data = bytes(960 * 2)  # 20ms at 48kHz mono s16
        track.enqueue(data)
        assert not track._queue.empty()

    def test_enqueue_overflow(self):
        track = _OutboundAudioTrack()
        track._queue = asyncio.Queue(maxsize=2)
        # Fill queue.
        track.enqueue(bytes(100))
        track.enqueue(bytes(100))
        # Overflow — should not raise.
        track.enqueue(bytes(100))

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_produces_silence_when_empty(self):
        track = _OutboundAudioTrack()
        frame = await track._recv()
        assert frame.sample_rate == 48000
        assert frame.samples == 960
        # Frame data should be all zeros (silence).
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_returns_enqueued_data(self):
        track = _OutboundAudioTrack()
        # Enqueue one frame of non-silent data.
        test_data = bytes(range(256)) * (960 * 2 // 256 + 1)
        test_data = test_data[: 960 * 2]
        track.enqueue(test_data)

        frame = await track._recv()
        actual = bytes(frame.planes[0])
        assert actual == test_data

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_preserves_audio_order_with_remainder(self):
        """Verify that audio chunks larger than one frame don't reorder."""
        track = _OutboundAudioTrack()
        frame_bytes = 960 * 2  # one 20ms frame at 48kHz mono s16

        # Create chunk A (1.5 frames) and chunk B (1 frame).
        chunk_a = bytes([0xAA]) * (frame_bytes + frame_bytes // 2)
        chunk_b = bytes([0xBB]) * frame_bytes
        track.enqueue(chunk_a)
        track.enqueue(chunk_b)

        # Frame 1: first frame of A.
        frame1 = await track._recv()
        data1 = bytes(frame1.planes[0])
        assert data1 == bytes([0xAA]) * frame_bytes

        # Frame 2: remainder of A (half frame) + start of B (half frame).
        frame2 = await track._recv()
        data2 = bytes(frame2.planes[0])
        expected = bytes([0xAA]) * (frame_bytes // 2) + bytes([0xBB]) * (frame_bytes // 2)
        assert data2 == expected

        # Frame 3: remainder of B (half frame) + silence padding.
        frame3 = await track._recv()
        data3 = bytes(frame3.planes[0])
        expected3 = bytes([0xBB]) * (frame_bytes // 2) + bytes(frame_bytes // 2)
        assert data3 == expected3
