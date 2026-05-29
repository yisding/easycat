"""Transport conformance and unit tests.

Tests cover:
  - LocalTransport (basic lifecycle without sounddevice hardware)
  - WebSocketTransport (full send/receive with a test client)
  - TwilioTransport (mocked Twilio Media Streams messages)
  - Audio conversion helpers (mulaw <-> PCM16 round-trip)
  - TwiML generation helpers
  - Transport protocol conformance
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import struct

import pytest
import websockets

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import DTMF, EventBus, PlaybackMarkAck
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import (
    TwilioConnectionTransport,
    TwilioTransport,
    TwilioTransportConfig,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    twiml_connect_stream,
    twiml_stream,
)
from easycat.transports.webrtc import WebRTCTransport
from easycat.transports.websocket import WebSocketTransport, WebSocketTransportConfig

from .conftest import find_free_port, make_chunk

# ── Helpers ───────────────────────────────────────────────────────

# Aliases for backward compatibility within this file.
_make_chunk = make_chunk
_find_free_port = find_free_port


def _make_sine_pcm16(freq: int = 440, duration_ms: int = 20, sample_rate: int = 16000) -> bytes:
    """Generate a short PCM16 sine wave for conversion tests."""
    import math

    n_samples = (sample_rate * duration_ms) // 1000
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * freq * t))
        samples.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{n_samples}h", *samples)


def _sounddevice_available() -> bool:
    if importlib.util.find_spec("sounddevice") is None:
        return False
    try:
        importlib.import_module("sounddevice")
    except (ImportError, OSError):
        return False
    return True


# ── LocalTransport tests ─────────────────────────────────────────


class TestLocalTransport:
    """Tests for LocalTransport (without requiring audio hardware)."""

    @pytest.mark.asyncio
    async def test_connect_disconnect_without_sounddevice(self):
        """LocalTransport requires sounddevice to connect."""
        transport = LocalTransport()
        if not _sounddevice_available():
            with pytest.raises(ImportError):
                await transport.connect()
            assert not transport.is_connected
        else:
            try:
                await transport.connect()
            except OSError:
                pytest.skip("No audio device available (CI/container environment)")
            assert transport.is_connected
            await transport.disconnect()
            assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        transport = LocalTransport()
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_send_audio_when_not_connected(self):
        """send_audio reports False when the device is not connected."""
        transport = LocalTransport()
        chunk = _make_chunk()
        delivered = await transport.send_audio(chunk)
        assert delivered is False

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_output_queue_full(self):
        """Dropped frames surface as a False return so AudioOut isn't emitted."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        # Tight queue so even a single split chunk overflows.
        config = LocalTransportConfig(max_pending_out_chunks=1)
        transport = LocalTransport(config)
        await transport.connect()
        try:
            # A 4800-byte chunk splits into ~8 frames; after the first one
            # the output queue is full and the remainder is dropped.
            big_chunk = _make_chunk(4800, sample_rate=16000)
            delivered = await transport.send_audio(big_chunk)
            assert delivered is False
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_defaults(self):
        config = LocalTransportConfig()
        assert config.audio_format == PCM16_MONO_24K
        assert config.frame_duration_ms == 20
        assert config.input_device is None
        assert config.output_device is None

    @pytest.mark.asyncio
    async def test_receive_audio_returns_on_disconnect(self):
        """receive_audio iterator ends when transport disconnects."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        transport = LocalTransport()
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
    async def test_send_audio_splits_oversized_chunks(self):
        """Chunks larger than one frame are split into frame-sized pieces."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        transport = LocalTransport()
        await transport.connect()

        # Default: 16kHz, 20ms frames → 320 samples → 640 bytes per frame.
        # Send a 4800-byte chunk (typical TTS size) — should produce 8 pieces.
        big_chunk = _make_chunk(4800, sample_rate=16000)
        await transport.send_audio(big_chunk)

        pieces: list[bytes] = []
        while not transport._out_queue.empty():
            pieces.append(transport._out_queue.get_nowait().chunk.data)

        # 4800 / 640 = 7.5 → 8 pieces (last one is a 320-byte remainder).
        assert len(pieces) == 8
        assert all(len(p) == 640 for p in pieces[:7])
        assert len(pieces[7]) == 320  # 4800 - 7*640 = 320

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_drops_whole_chunk_when_out_queue_lacks_capacity(self):
        transport = LocalTransport(
            LocalTransportConfig(
                audio_format=PCM16_MONO_24K,
                frame_duration_ms=20,
                max_pending_out_chunks=1,
            )
        )
        transport._connected = True
        transport._out_queue.put_nowait(None)

        chunk = _make_chunk(1920, sample_rate=24000)  # needs two 20ms output frames
        delivered = await transport.send_audio(chunk)

        assert delivered is False
        assert transport._out_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_mic_queue_full_emits_inbound_queue_full(self):
        """Mic-queue overflow surfaces a TransportDegraded like other transports."""
        from easycat.events import TransportDegraded
        from easycat.transports._base import _DEGRADED_INBOUND_QUEUE_FULL

        transport = LocalTransport(LocalTransportConfig(max_pending_in_chunks=1))
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        transport._enqueue_chunk(_make_chunk(), context="mic")  # fills the 1 slot
        transport._enqueue_chunk(_make_chunk(), context="mic")  # dropped
        for _ in range(5):
            await asyncio.sleep(0)

        assert [e.reason for e in received] == [_DEGRADED_INBOUND_QUEUE_FULL]
        assert received[0].provider == "local"
        assert "mic" in received[0].detail

    @pytest.mark.asyncio
    async def test_schedule_audio_delivery_tracks_emit_task(self):
        """The audio-delivery emit task is retained so it isn't GC'd mid-flight."""
        from easycat.events import TransportAudioDelivered
        from easycat.transports.local import _QueuedOutputChunk

        transport = LocalTransport()
        bus = EventBus()
        received: list[TransportAudioDelivered] = []
        bus.subscribe(TransportAudioDelivered, lambda e: received.append(e))
        transport._event_bus = bus
        transport._loop = asyncio.get_running_loop()

        queued = _QueuedOutputChunk(chunk=_make_chunk(), turn_id="t1")
        transport._schedule_audio_delivery(queued)
        # call_soon_threadsafe callback runs first, then the emit task.
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0].turn_id == "t1"
        assert transport._emit_tasks == set()  # drained after completion


# ── WebSocketTransport tests ─────────────────────────────────────


@pytest.mark.integration_socket
class TestWebSocketTransport:
    """Tests for WebSocketTransport with a real test client."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = _find_free_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)

        await transport.connect()
        assert transport.is_connected
        assert not transport.has_client

        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_send_receive_audio(self):
        """Client sends audio, server yields it via receive_audio."""
        port = _find_free_port()
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
        port = _find_free_port()
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
    async def test_control_message_config(self):
        """Client can send a config control message to negotiate format."""
        port = _find_free_port()
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

        for sample_rate in (True, False, 0, -16000, 384001, 16000.0, "16000", None):
            transport._handle_control_message(
                json.dumps({"type": "config", "sample_rate": sample_rate})
            )
            assert transport._audio_format.sample_rate == 16000

        transport._handle_control_message(json.dumps({"type": "config", "sample_rate": 44100}))
        assert transport._audio_format.sample_rate == 44100

    @pytest.mark.asyncio
    async def test_client_disconnect_signals_end(self):
        """When client disconnects, receive_audio iterator should end."""
        port = _find_free_port()
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
        port = _find_free_port()
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
        port = _find_free_port()
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
        port = _find_free_port()
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


# ── TwilioTransport tests ────────────────────────────────────────


def _twilio_connected_msg() -> str:
    return json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})


def _twilio_start_msg(stream_sid: str = "MZ123", call_sid: str = "CA456") -> str:
    return json.dumps(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "accountSid": "AC789",
                "callSid": call_sid,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "channels": 1,
                },
            },
        }
    )


def _twilio_media_msg(mulaw_data: bytes, stream_sid: str = "MZ123") -> str:
    payload = base64.b64encode(mulaw_data).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": "2",
            "streamSid": stream_sid,
            "media": {"track": "inbound", "chunk": "1", "timestamp": "0", "payload": payload},
        }
    )


def _twilio_media_msg_with_track(
    mulaw_data: bytes,
    *,
    stream_sid: str = "MZ123",
    track: str = "inbound",
) -> str:
    payload = base64.b64encode(mulaw_data).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": "2",
            "streamSid": stream_sid,
            "media": {"track": track, "chunk": "1", "timestamp": "0", "payload": payload},
        }
    )


def _twilio_dtmf_msg(digit: str, stream_sid: str = "MZ123") -> str:
    return json.dumps(
        {
            "event": "dtmf",
            "streamSid": stream_sid,
            "dtmf": {"digit": digit, "track": "inbound_track"},
        }
    )


def _twilio_stop_msg(stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "stop", "streamSid": stream_sid})


def _twilio_mark_msg(name: str, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


class _DummyTwilioWebSocket:
    async def send(self, _message: str) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.integration_socket
class TestTwilioTransport:
    """Tests for TwilioTransport with mocked Twilio messages."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)

        await transport.connect()
        assert transport.is_connected
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_receive_audio_from_twilio(self):
        """Twilio media messages produce PCM16 audio chunks."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        received: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                received.append(chunk)
                if len(received) >= 1:
                    break

        collect_task = asyncio.create_task(collect())

        # Simulate Twilio client.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())

            # Create some mulaw audio (160 samples = 20ms at 8kHz).
            pcm_silence = bytes(320)  # 160 samples * 2 bytes
            mulaw_data = pcm16_to_mulaw(pcm_silence, source_rate=8000)
            await ws.send(_twilio_media_msg(mulaw_data))

            await asyncio.wait_for(collect_task, timeout=2.0)

        await transport.disconnect()
        assert len(received) == 1
        assert received[0].format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_media_frame_guard_filters_prestart_wrong_stream_and_outbound_tracks(self):
        """Server transport only accepts inbound media for the active streamSid."""
        transport = TwilioTransport(TwilioTransportConfig())
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_media_msg_with_track(mulaw_data))
        assert transport._in_queue.empty()

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="WRONG", track="inbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="outbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(
                mulaw_data,
                stream_sid="STREAM1",
                track="outbound_track",
            )
        )
        assert transport._in_queue.empty()

        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="inbound")
        )
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_connection_media_frame_guard_filters_prestart_wrong_stream_and_outbound_tracks(
        self,
    ):
        """Connection transport uses the same Twilio inbound media guard."""
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket())
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_media_msg_with_track(mulaw_data))
        assert transport._in_queue.empty()

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="WRONG", track="inbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="outbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(
                mulaw_data,
                stream_sid="STREAM1",
                track="outbound_track",
            )
        )
        assert transport._in_queue.empty()

        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="inbound")
        )
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_send_audio_to_twilio(self):
        """Audio sent via send_audio is received by Twilio as a base64 media message."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg("STREAM1"))
            await asyncio.sleep(0.1)

            assert transport.stream_sid == "STREAM1"

            # Send PCM16 audio chunk.
            chunk = _make_chunk(640, sample_rate=16000)
            await transport.send_audio(chunk)

            # Receive the media message from server.
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "media"
            assert msg["streamSid"] == "STREAM1"
            # Verify the payload is valid base64 mulaw.
            payload = base64.b64decode(msg["media"]["payload"])
            assert len(payload) > 0

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_playback_mark_to_twilio(self):
        """Playback marks are sent as Twilio mark messages."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg("STREAM1"))
            await asyncio.sleep(0.1)

            mark_name = await transport.send_playback_mark("unit_mark")
            assert mark_name == "unit_mark"

            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "mark"
            assert msg["streamSid"] == "STREAM1"
            assert msg["mark"]["name"] == "unit_mark"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_dtmf_emitted_to_event_bus(self):
        """DTMF messages from Twilio are emitted as DTMF events."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        event_bus = EventBus()
        transport = TwilioTransport(config, event_bus=event_bus)

        digits_received: list[str] = []
        event_bus.subscribe(DTMF, lambda e: digits_received.append(e.digit))

        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_dtmf_msg("5"))
            await ws.send(_twilio_dtmf_msg("#"))
            await asyncio.sleep(0.1)

        await transport.disconnect()
        assert digits_received == ["5", "#"]

    @pytest.mark.asyncio
    async def test_mark_ack_emitted_to_event_bus(self):
        """Twilio mark messages are emitted as PlaybackMarkAck events."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        event_bus = EventBus()
        transport = TwilioTransport(config, event_bus=event_bus)

        marks_received: list[str] = []
        event_bus.subscribe(PlaybackMarkAck, lambda e: marks_received.append(e.mark_name))

        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_mark_msg("mark_1"))
            await ws.send(_twilio_mark_msg("mark_2"))
            await asyncio.sleep(0.1)

        await transport.disconnect()
        assert marks_received == ["mark_1", "mark_2"]

    @pytest.mark.asyncio
    async def test_control_events_ignore_wrong_stream_sid(self):
        """stop/mark/dtmf are scoped to the active Twilio streamSid."""
        event_bus = EventBus()
        transport = TwilioTransport(event_bus=event_bus)
        digits_received: list[str] = []
        marks_received: list[str] = []
        event_bus.subscribe(DTMF, lambda e: digits_received.append(e.digit))
        event_bus.subscribe(PlaybackMarkAck, lambda e: marks_received.append(e.mark_name))

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_dtmf_msg("5", stream_sid="WRONG"))
        await transport._handle_message(_twilio_mark_msg("mark_1", stream_sid="WRONG"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="WRONG"))

        assert digits_received == []
        assert marks_received == []
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

        await transport._handle_message(_twilio_dtmf_msg("6", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_mark_msg("mark_2", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM1"))

        assert digits_received == ["6"]
        assert marks_received == ["mark_2"]
        assert transport.stream_sid is None
        assert transport.call_sid is None

    @pytest.mark.asyncio
    async def test_stream_metadata(self):
        """stream_sid and call_sid are set from the start message."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg("MY_STREAM", "MY_CALL"))
            await asyncio.sleep(0.1)

            assert transport.stream_sid == "MY_STREAM"
            assert transport.call_sid == "MY_CALL"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_stop_message(self):
        """Twilio stop message ends the receive_audio iterator."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                chunks.append(chunk)

        collect_task = asyncio.create_task(collect())

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_stop_msg())

        # Client disconnected — collect should end.
        await asyncio.wait_for(collect_task, timeout=2.0)

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_wait_for_client_waits_for_new_twilio_connection_after_disconnect(self):
        """wait_for_client should clear after Twilio socket disconnects."""
        port = _find_free_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await asyncio.sleep(0.05)
        assert not transport.has_client

        with pytest.raises(asyncio.TimeoutError):
            await transport.wait_for_client(timeout=0.1)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
            await ws2.send(_twilio_connected_msg())
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await transport.disconnect()


# ── Audio conversion tests ────────────────────────────────────────


class TestAudioConversion:
    """Tests for mulaw <-> PCM16 conversion helpers."""

    def test_mulaw_to_pcm16_silence(self):
        """Silent mulaw converts to (near) silent PCM16."""
        # mulaw silence is 0xFF.
        mulaw_silence = bytes([0xFF] * 160)
        pcm = mulaw_to_pcm16(mulaw_silence, target_rate=8000)
        # Should produce PCM16 samples.
        assert len(pcm) == 320  # 160 samples * 2 bytes

    def test_pcm16_to_mulaw_roundtrip(self):
        """PCM16 -> mulaw -> PCM16 round-trip preserves signal shape."""
        pcm_original = _make_sine_pcm16(freq=440, duration_ms=20, sample_rate=8000)
        mulaw = pcm16_to_mulaw(pcm_original, source_rate=8000)
        pcm_back = mulaw_to_pcm16(mulaw, target_rate=8000)

        # Lengths should match.
        assert len(pcm_back) == len(pcm_original)

        # Decode both and check correlation (mulaw is lossy, so values won't match exactly).
        n = len(pcm_original) // 2
        orig_samples = struct.unpack(f"<{n}h", pcm_original)
        back_samples = struct.unpack(f"<{n}h", pcm_back)

        # Correlation check: most samples should be within ~200 of original.
        diffs = [abs(a - b) for a, b in zip(orig_samples, back_samples)]
        avg_diff = sum(diffs) / len(diffs)
        assert avg_diff < 500, f"Average sample difference too high: {avg_diff}"

    def test_pcm16_to_mulaw_with_resampling(self):
        """PCM16 at 16kHz -> mulaw 8kHz produces the expected number of samples."""
        pcm_16k = _make_sine_pcm16(freq=440, duration_ms=20, sample_rate=16000)
        mulaw = pcm16_to_mulaw(pcm_16k, source_rate=16000)
        # 20ms at 8kHz = 160 samples; mulaw is 1 byte per sample.
        assert len(mulaw) == 160

    def test_mulaw_to_pcm16_with_upsampling(self):
        """mulaw 8kHz -> PCM16 16kHz produces the expected number of samples."""
        mulaw_data = bytes([0xFF] * 160)  # 20ms at 8kHz
        pcm = mulaw_to_pcm16(mulaw_data, target_rate=16000)
        # 20ms at 16kHz = 320 samples * 2 bytes = 640 bytes.
        assert len(pcm) == 640


# ── TwiML helper tests ───────────────────────────────────────────


class TestTwiML:
    """Tests for TwiML generation helpers."""

    def test_twiml_connect_stream(self):
        xml = twiml_connect_stream("wss://example.com/stream")
        assert '<?xml version="1.0"' in xml
        assert "<Connect>" in xml
        assert '<Stream url="wss://example.com/stream"' in xml
        assert 'track="both"' in xml
        assert "</Response>" in xml
        assert "<Parameter" not in xml

    def test_twiml_connect_stream_with_callback(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            status_callback_url="https://example.com/status",
        )
        assert 'statusCallback="https://example.com/status"' in xml

    def test_twiml_connect_stream_custom_track(self):
        xml = twiml_connect_stream("wss://example.com/stream", track="inbound")
        assert 'track="inbound"' in xml

    def test_twiml_connect_stream_disable_caller_id(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            forward_caller_id=False,
        )
        assert "<Parameter" not in xml
        assert "<Stream" in xml and "/>" in xml

    def test_twiml_connect_stream_custom_parameters(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            parameters={"crm_account_id": "ACC-42"},
        )
        assert '<Parameter name="crm_account_id"' in xml
        assert 'value="ACC-42"' in xml
        assert '<Parameter name="From"' not in xml

    def test_twiml_connect_stream_explicit_caller_id_parameters(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            parameters={
                "Direction": "inbound",
                "From": "+15551234567",
                "To": "+15557654321",
            },
            forward_caller_id=True,
        )
        assert '<Parameter name="Direction" value="inbound"/>' in xml
        assert '<Parameter name="From" value="+15551234567"/>' in xml
        assert '<Parameter name="To" value="+15557654321"/>' in xml
        assert "{{From}}" not in xml

    def test_twiml_connect_stream_forward_caller_id_requires_values(self):
        with pytest.raises(ValueError, match="explicit caller-ID values"):
            twiml_connect_stream("wss://example.com/stream", forward_caller_id=True)
        with pytest.raises(ValueError, match="explicit caller-ID values"):
            twiml_connect_stream(
                "wss://example.com/stream",
                parameters={"From": "{{From}}"},
                forward_caller_id=True,
            )

    def test_twiml_connect_stream_escapes_parameter_values_once(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            parameters={"company": "AT&T <Gold>"},
            forward_caller_id=False,
        )
        assert '<Parameter name="company" value="AT&amp;T &lt;Gold&gt;"/>' in xml
        assert "amp;amp" not in xml

    def test_twiml_stream(self):
        xml = twiml_stream("wss://example.com/stream")
        assert "<Start>" in xml
        assert '<Stream url="wss://example.com/stream"' in xml
        assert 'track="inbound_track"' in xml
        assert "<Pause" in xml


# ── Transport conformance tests ───────────────────────────────────


class TestTransportConformance:
    """Verify all transports satisfy the Transport protocol shape."""

    def _assert_has_protocol_methods(self, t: object) -> None:
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    def test_local_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(LocalTransport())

    def test_websocket_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(WebSocketTransport())

    def test_twilio_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(TwilioTransport())

    def test_webrtc_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(WebRTCTransport())

    def test_local_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(LocalTransport(), Transport)

    def test_websocket_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(WebSocketTransport(), Transport)

    def test_twilio_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(TwilioTransport(), Transport)

    def test_webrtc_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(WebRTCTransport(), Transport)
