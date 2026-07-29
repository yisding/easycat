"""WebSocket server transport tests."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from easycat.audio_format import AudioChunk
from easycat.events import EventBus
from easycat.transports.websocket import WebSocketTransport, WebSocketTransportConfig

from ._webrtc_fakes import _UsesPytestTcpPortFactory
from .conftest import make_chunk

_make_chunk = make_chunk


def test_websocket_transport_config_defaults_to_loopback():
    config = WebSocketTransportConfig()

    assert config.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_server_websocket_transports_disable_compression(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    class FakeServer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def fake_serve(*_args: object, **kwargs: object) -> FakeServer:
        calls.append(kwargs)
        return FakeServer()

    monkeypatch.setattr("easycat.transports._base.websockets.serve", fake_serve)

    transport = WebSocketTransport(WebSocketTransportConfig())
    await transport.connect()
    try:
        assert calls == [{"compression": None}]
    finally:
        await transport.disconnect()


@pytest.mark.integration_socket
class TestWebSocketTransport(_UsesPytestTcpPortFactory):
    """Tests for WebSocketTransport with a real test client."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)

        await transport.connect()
        assert transport.is_connected
        assert not transport.has_client

        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_default_host_accepts_loopback_client(self):
        port = self._unused_port()
        config = WebSocketTransportConfig(port=port)
        transport = WebSocketTransport(config)

        await transport.connect()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                ready = await ws.recv()
                assert json.loads(ready)["type"] == "ready"
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_receive_audio(self):
        """Client sends audio, server yields it via receive_audio."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        received_chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                received_chunks.append(chunk)
                if len(received_chunks) >= 3:
                    break

        collect_task = asyncio.create_task(collect())

        # Connect a test client and send binary frames.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # Should receive ready message.
            ready = await ws.recv()
            assert json.loads(ready)["type"] == "ready"

            # Send 3 audio frames.
            for _ in range(3):
                await ws.send(bytes(320))

            await asyncio.wait_for(collect_task, timeout=2.0)

        await transport.disconnect()
        assert len(received_chunks) == 3
        assert all(len(c.data) == 320 for c in received_chunks)

    @pytest.mark.asyncio
    async def test_server_sends_audio_to_client(self):
        """Server sends audio chunk, client receives binary frame."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # Consume ready message.
            await ws.recv()
            await asyncio.sleep(0.05)

            # Send audio from server to client.
            chunk = _make_chunk(640)
            await transport.send_audio(chunk)
            fmt_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)  # audio_format
            assert json.loads(fmt_msg)["type"] == "audio_format"
            data = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert isinstance(data, bytes)
            assert len(data) == 640

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_clear_audio_sends_client_playback_reset(self):
        port = self._unused_port()
        transport = WebSocketTransport(WebSocketTransportConfig(host="127.0.0.1", port=port))
        await transport.connect()

        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # ready
                await asyncio.wait_for(transport.wait_for_client(), timeout=2.0)

                await transport.clear_audio()

                message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                assert json.loads(message) == {"type": "clear"}
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_server_forwards_session_events_as_json_text_frames(self):
        """Session events reach the browser as JSON control messages."""
        from easycat.events import STTFinal

        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        bus = EventBus()
        transport._event_bus = bus  # Session attaches the bus pre-connect.
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await asyncio.sleep(0.05)

            await bus.emit(STTFinal(text="hello there", turn_id="t1"))
            message = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert json.loads(message) == {
                "type": "stt_final",
                "text": "hello there",
                "turn_id": "t1",
            }

        await transport.disconnect()

        # Teardown unsubscribes the forwarder; later emits must not raise.
        await bus.emit(STTFinal(text="late", turn_id="t2"))

    @pytest.mark.asyncio
    async def test_control_message_config(self):
        """Client can send a config control message to negotiate format."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(json.dumps({"type": "config", "sample_rate": 24000}))
            await asyncio.sleep(0.1)
            assert transport._audio_format.sample_rate == 24000

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_invalid_sample_rate_config_is_ignored(self):
        """Invalid config messages must not poison the negotiated audio format."""
        transport = WebSocketTransport()

        for sample_rate in (True, False, 0, 1, 7999, -16000, 384001, 16000.0, "16000", None):
            transport._handle_control_message(
                json.dumps({"type": "config", "sample_rate": sample_rate})
            )
            assert transport._audio_format.sample_rate == 16000

        transport._handle_control_message(json.dumps({"type": "config", "sample_rate": 44100}))
        assert transport._audio_format.sample_rate == 44100

    @pytest.mark.asyncio
    async def test_client_disconnect_signals_end(self):
        """When client disconnects, receive_audio iterator should end."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        received: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                received.append(chunk)

        collect_task = asyncio.create_task(collect())

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()
            await ws.send(bytes(320))
            await asyncio.sleep(0.05)

        # Client disconnected; collect should finish.
        await asyncio.wait_for(collect_task, timeout=2.0)
        assert len(received) == 1

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_audio_format_resets_after_client_disconnect(self):
        """Negotiated audio format resets to default when client disconnects."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        # First client negotiates 24kHz.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(json.dumps({"type": "config", "sample_rate": 24000}))
            await asyncio.sleep(0.1)
            assert transport._audio_format.sample_rate == 24000

        # Client disconnected — format should reset to 16kHz default.
        await asyncio.sleep(0.1)
        assert transport._audio_format.sample_rate == 16000

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_rejects_second_client(self):
        """Only one client at a time is allowed."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws1:
            await ws1.recv()  # ready

            # Second client should be rejected.
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
                try:
                    await asyncio.wait_for(ws2.recv(), timeout=1.0)
                except websockets.exceptions.ConnectionClosed:
                    pass  # Expected — server closes with 4000.

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_wait_for_client_waits_for_new_connection_after_disconnect(self):
        """wait_for_client should not stay set after a client disconnects."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await asyncio.sleep(0.05)
        assert not transport.has_client

        with pytest.raises(asyncio.TimeoutError):
            await transport.wait_for_client(timeout=0.1)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
            await ws2.recv()  # ready
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await transport.disconnect()
