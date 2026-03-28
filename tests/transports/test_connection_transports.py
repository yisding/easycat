"""Tests for per-connection transport implementations.

Covers WebSocketConnectionTransport and TwilioConnectionTransport using
a real websockets server/client pair (no mocks).
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import websockets

from easycat.audio_format import AudioChunk
from easycat.events import DTMF, EventBus, PlaybackMarkAck
from easycat.transports.twilio_media import (
    TwilioConnectionTransport,
    pcm16_to_mulaw,
)
from easycat.transports.websocket import WebSocketConnectionTransport

from .conftest import find_free_port, make_chunk

# ── Helpers ────────────────────────────────────────────────────────


def _twilio_start_msg(stream_sid: str = "MZ123", call_sid: str = "CA456") -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": stream_sid,
            "start": {"streamSid": stream_sid, "callSid": call_sid},
        }
    )


def _twilio_media_msg(mulaw_data: bytes, stream_sid: str = "MZ123") -> str:
    payload = base64.b64encode(mulaw_data).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        }
    )


def _twilio_mark_msg(name: str, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


def _twilio_dtmf_msg(digit: str) -> str:
    return json.dumps({"event": "dtmf", "dtmf": {"digit": digit}})


def _twilio_stop_msg() -> str:
    return json.dumps({"event": "stop"})


# ── WebSocketConnectionTransport tests ────────────────────────────


class TestWebSocketConnectionTransport:
    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = find_free_port()
        transport_holder: list[WebSocketConnectionTransport] = []

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            transport_holder.append(t)
            await t.connect()
            assert t.is_connected
            # Keep alive until client disconnects.
            await ws.wait_closed()

        server = await websockets.serve(handler, "127.0.0.1", port)
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            ready = await ws.recv()
            assert json.loads(ready)["type"] == "ready"
            # Transport should be connected.
            await asyncio.sleep(0.05)
            assert transport_holder[0].is_connected

        # Client disconnected — give receive loop time to notice.
        await asyncio.sleep(0.1)
        transport = transport_holder[0]
        await transport.disconnect()
        assert not transport.is_connected

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_send_receive_audio(self):
        port = find_free_port()
        received: list[AudioChunk] = []

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            await t.connect()
            async for chunk in t.receive_audio():
                received.append(chunk)
                if len(received) >= 3:
                    break
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            for _ in range(3):
                await ws.send(bytes(320))
            await asyncio.sleep(0.1)

        server.close()
        await server.wait_closed()
        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_server_sends_audio_to_client(self):
        port = find_free_port()

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            await t.connect()
            chunk = make_chunk(640)
            await t.send_audio(chunk)
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            data = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert isinstance(data, bytes)
            assert len(data) == 640

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_control_message_config(self):
        port = find_free_port()
        transport_holder: list[WebSocketConnectionTransport] = []

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            transport_holder.append(t)
            await t.connect()
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(json.dumps({"type": "config", "sample_rate": 24000}))
            await asyncio.sleep(0.1)
            assert transport_holder[0]._audio_format.sample_rate == 24000

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_client_disconnect_ends_receive(self):
        port = find_free_port()
        received: list[AudioChunk] = []
        collect_done = asyncio.Event()

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            await t.connect()
            async for chunk in t.receive_audio():
                received.append(chunk)
            collect_done.set()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(bytes(320))
            await asyncio.sleep(0.05)

        # Client disconnected; handler should finish.
        await asyncio.wait_for(collect_done.wait(), timeout=2.0)
        assert len(received) == 1

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        port = find_free_port()

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            await t.connect()
            await t.disconnect()
            await t.disconnect()  # Should not raise.

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await asyncio.sleep(0.1)

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        port = find_free_port()
        ready_count = 0

        async def handler(ws):
            t = WebSocketConnectionTransport(ws)
            await t.connect()
            await t.connect()  # Should be a no-op.
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            ready = await ws.recv()
            assert json.loads(ready)["type"] == "ready"
            ready_count += 1
            # Second connect is a no-op, so no second ready message.
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.2)
                ready_count += 1
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        server.close()
        await server.wait_closed()
        assert ready_count == 1


# ── TwilioConnectionTransport tests ──────────────────────────────


class TestTwilioConnectionTransport:
    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = find_free_port()
        transport_holder: list[TwilioConnectionTransport] = []

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            transport_holder.append(t)
            await t.connect()
            assert t.is_connected
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as _ws:
            await asyncio.sleep(0.05)
            assert transport_holder[0].is_connected

        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_receive_audio(self):
        port = find_free_port()
        received: list[AudioChunk] = []

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            async for chunk in t.receive_audio():
                received.append(chunk)
                if len(received) >= 1:
                    break
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg())
            pcm_silence = bytes(320)
            mulaw_data = pcm16_to_mulaw(pcm_silence, source_rate=8000)
            await ws.send(_twilio_media_msg(mulaw_data))
            await asyncio.sleep(0.2)

        server.close()
        await server.wait_closed()
        assert len(received) == 1
        assert received[0].format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_send_audio(self):
        port = find_free_port()
        send_ready = asyncio.Event()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            # Wait for the start message to be processed by the receive loop.
            await asyncio.sleep(0.1)
            chunk = make_chunk(640, sample_rate=16000)
            await t.send_audio(chunk)
            send_ready.set()
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg("STREAM1"))
            await send_ready.wait()
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "media"
            assert msg["streamSid"] == "STREAM1"
            payload = base64.b64decode(msg["media"]["payload"])
            assert len(payload) > 0

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_send_mark(self):
        port = find_free_port()
        send_ready = asyncio.Event()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            # Wait for the receive loop to process the start message.
            await asyncio.sleep(0.1)
            name = await t.send_mark("test_mark")
            assert name == "test_mark"
            send_ready.set()
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg("STREAM1"))
            await send_ready.wait()
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "mark"
            assert msg["mark"]["name"] == "test_mark"

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_send_mark_no_stream_returns_empty(self):
        """send_mark without an active stream returns gracefully."""
        port = find_free_port()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            name = await t.send_mark("x")
            assert name == ""  # No stream_sid → returns empty string.
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as _ws:
            await asyncio.sleep(0.1)

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_send_playback_mark(self):
        port = find_free_port()
        send_ready = asyncio.Event()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            await asyncio.sleep(0.1)
            name = await t.send_playback_mark("pb_mark")
            assert name == "pb_mark"
            send_ready.set()
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg("STREAM1"))
            await send_ready.wait()
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "mark"
            assert msg["mark"]["name"] == "pb_mark"

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_dtmf_emitted(self):
        port = find_free_port()
        digits: list[str] = []

        async def handler(ws):
            event_bus = EventBus()
            event_bus.subscribe(DTMF, lambda e: digits.append(e.digit))
            t = TwilioConnectionTransport(ws, event_bus=event_bus)
            await t.connect()
            await ws.wait_closed()
            await asyncio.sleep(0.05)
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_dtmf_msg("5"))
            await ws.send(_twilio_dtmf_msg("#"))
            await asyncio.sleep(0.1)

        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()
        assert digits == ["5", "#"]

    @pytest.mark.asyncio
    async def test_mark_ack_emitted(self):
        port = find_free_port()
        marks: list[str] = []

        async def handler(ws):
            event_bus = EventBus()
            event_bus.subscribe(PlaybackMarkAck, lambda e: marks.append(e.mark_name))
            t = TwilioConnectionTransport(ws, event_bus=event_bus)
            await t.connect()
            await ws.wait_closed()
            await asyncio.sleep(0.05)
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_mark_msg("m1"))
            await ws.send(_twilio_mark_msg("m2"))
            await asyncio.sleep(0.1)

        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()
        assert marks == ["m1", "m2"]

    @pytest.mark.asyncio
    async def test_stop_message_ends_receive(self):
        port = find_free_port()
        chunks: list[AudioChunk] = []
        collect_done = asyncio.Event()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            async for chunk in t.receive_audio():
                chunks.append(chunk)
            collect_done.set()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_stop_msg())
            await asyncio.sleep(0.1)

        await asyncio.wait_for(collect_done.wait(), timeout=2.0)
        assert len(chunks) == 0

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        port = find_free_port()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            await t.disconnect()
            await t.disconnect()  # Should not raise.

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as _ws:
            await asyncio.sleep(0.1)

        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_clear_audio(self):
        port = find_free_port()
        send_ready = asyncio.Event()

        async def handler(ws):
            t = TwilioConnectionTransport(ws)
            await t.connect()
            await asyncio.sleep(0.1)
            await t.clear_audio()  # Should send clear event.
            send_ready.set()
            await ws.wait_closed()
            await t.disconnect()

        server = await websockets.serve(handler, "127.0.0.1", port)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_start_msg("STREAM1"))
            await send_ready.wait()
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "clear"
            assert msg["streamSid"] == "STREAM1"

        server.close()
        await server.wait_closed()
