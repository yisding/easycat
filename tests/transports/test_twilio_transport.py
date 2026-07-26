"""Twilio media stream transport, TwiML, and audio conversion tests."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct

import pytest
import websockets

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk
from easycat.events import DTMF, CallEnded, EventBus, PlaybackMarkAck, TransportDegraded
from easycat.transports.twilio_media import (
    _DEGRADED_TWILIO_SEQUENCE_GAP,
    _DEGRADED_TWILIO_TIMESTAMP_GAP,
    TWILIO_STREAM_TOKEN_PARAMETER,
    StreamTokenContext,
    TwilioConnectionTransport,
    TwilioStreamTokenStore,
    TwilioTransport,
    TwilioTransportConfig,
    _TwilioProtocolMixin,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    twiml_connect_stream,
    twiml_stream,
)

from ._webrtc_fakes import _UsesPytestTcpPortFactory
from .conftest import make_chunk

_make_chunk = make_chunk


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


def _twilio_connected_msg() -> str:
    return json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})


def _twilio_start_msg(
    stream_sid: str = "MZ123",
    call_sid: str = "CA456",
    *,
    custom_parameters: dict[str, str] | None = None,
) -> str:
    start = {
        "streamSid": stream_sid,
        "accountSid": "AC789",
        "callSid": call_sid,
        "tracks": ["inbound"],
        "mediaFormat": {
            "encoding": "audio/x-mulaw",
            "sampleRate": 8000,
            "channels": 1,
        },
    }
    if custom_parameters is not None:
        start["customParameters"] = custom_parameters
    return json.dumps(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": start,
        }
    )


def _twilio_media_msg(
    mulaw_data: bytes,
    stream_sid: str = "MZ123",
    *,
    sequence_number: str = "2",
    timestamp: str = "0",
) -> str:
    payload = base64.b64encode(mulaw_data).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": sequence_number,
            "streamSid": stream_sid,
            "media": {
                "track": "inbound",
                "chunk": "1",
                "timestamp": timestamp,
                "payload": payload,
            },
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


async def _drain_transport_diagnostics(
    transport: TwilioTransport | TwilioConnectionTransport,
) -> None:
    await transport._drain_emit_tasks()


class _DummyTwilioWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed_with: tuple[object, ...] | None = None

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args


class TestTwilioStreamTokenStore:
    def test_consumes_token_once(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue()

        assert store.consume(token)
        assert not store.consume(token)
        assert not store.consume(f"{token}x")
        assert not store.consume("nonce.123.é")

    def test_rejects_expired_tokens(self) -> None:
        current = 1000.0

        def now() -> float:
            return current

        store = TwilioStreamTokenStore("secret", ttl_s=1, now=now)
        token = store.issue()
        current = 1002.0

        assert not store.consume(token)


class TestTwilioStreamTokenValidation:
    @pytest.mark.asyncio
    async def test_server_transport_consumes_token_and_hides_parameter(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue()
        config = TwilioTransportConfig(stream_token_validator=store.consume)
        transport = TwilioTransport(config)

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={
                    TWILIO_STREAM_TOKEN_PARAMETER: token,
                    "crm_account_id": "ACC-42",
                },
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert transport.call_identity.custom_fields == {"crm_account_id": "ACC-42"}

        replay = TwilioTransport(config)
        await replay._handle_message(
            _twilio_start_msg(
                "STREAM2",
                "CALL2",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: token},
            )
        )
        assert replay.stream_sid is None
        assert replay.call_sid is None

    @pytest.mark.asyncio
    async def test_connection_transport_rejects_missing_token(self) -> None:
        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=lambda _token: True),
        )

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ws.closed_with == (4003, "Missing or invalid stream token")

    @pytest.mark.asyncio
    async def test_connection_transport_accepts_valid_token(self) -> None:
        store = TwilioStreamTokenStore("secret")
        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=store.consume),
        )

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: store.issue()},
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert ws.closed_with is None

    @pytest.mark.asyncio
    async def test_context_validator_receives_start_fields_and_merges_claims(self) -> None:
        seen: list[StreamTokenContext] = []

        def validator(context: StreamTokenContext) -> dict[str, object]:
            seen.append(context)
            return {"tenant_id": "verified-tenant", "attempt": 3}

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={
                    TWILIO_STREAM_TOKEN_PARAMETER: "token-1",
                    "tenant_id": "spoofed-tenant",
                    "crm_account_id": "ACC-42",
                },
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert len(seen) == 1
        assert seen[0].token == "token-1"
        assert seen[0].stream_sid == "STREAM1"
        assert seen[0].call_sid == "CALL1"
        assert seen[0].parameters[TWILIO_STREAM_TOKEN_PARAMETER] == "token-1"
        assert transport.call_identity.custom_fields == {
            "tenant_id": "verified-tenant",
            "crm_account_id": "ACC-42",
            "attempt": "3",
        }

    @pytest.mark.asyncio
    async def test_async_context_validator_is_awaited(self) -> None:
        async def validator(ctx: StreamTokenContext) -> bool:
            await asyncio.sleep(0)
            return ctx.call_sid == "CALL1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"


class TestTwilioDtmfParsingInTransports:
    @pytest.mark.asyncio
    async def test_server_transport_uses_shared_dtmf_parser(self) -> None:
        event_bus = EventBus()
        digits: list[str] = []
        event_bus.subscribe(DTMF, lambda event: digits.append(event.digit))
        transport = TwilioTransport(event_bus=event_bus)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_dtmf_msg("a", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_dtmf_msg("X", stream_sid="STREAM1"))
        await transport._handle_message(
            json.dumps(
                {
                    "event": "dtmf",
                    "streamSid": "STREAM1",
                    "dtmf": {"digit": "12"},
                }
            )
        )

        assert digits == ["A"]

    @pytest.mark.asyncio
    async def test_connection_transport_uses_shared_dtmf_parser(self) -> None:
        event_bus = EventBus()
        digits: list[str] = []
        event_bus.subscribe(DTMF, lambda event: digits.append(event.digit))
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_dtmf_msg("b", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_dtmf_msg("", stream_sid="STREAM1"))
        await transport._handle_message(
            json.dumps({"event": "dtmf", "streamSid": "STREAM1", "dtmf": "5"})
        )

        assert digits == ["B"]


class TestTwilioStreamGapDiagnostics:
    @pytest.mark.asyncio
    async def test_server_transport_emits_sequence_and_timestamp_gap_diagnostics(self) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="3",
                timestamp="0",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="4",
                timestamp="60",
            )
        )
        await _drain_transport_diagnostics(transport)

        assert [event.reason for event in degraded] == [
            _DEGRADED_TWILIO_SEQUENCE_GAP,
            _DEGRADED_TWILIO_TIMESTAMP_GAP,
        ]
        assert degraded[0].provider == "telephony"
        assert "expected sequenceNumber 2, got 3" in degraded[0].detail
        assert "expected media timestamp 20ms, got 60ms" in degraded[1].detail

    @pytest.mark.asyncio
    async def test_connection_transport_emits_sequence_gap_for_active_controls(self) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            json.dumps(
                {
                    "event": "dtmf",
                    "sequenceNumber": "4",
                    "streamSid": "STREAM1",
                    "dtmf": {"digit": "5"},
                }
            )
        )
        await _drain_transport_diagnostics(transport)

        assert [event.reason for event in degraded] == [_DEGRADED_TWILIO_SEQUENCE_GAP]
        assert "expected sequenceNumber 2, got 4" in degraded[0].detail

    @pytest.mark.asyncio
    async def test_malformed_media_metadata_breaks_gap_tracking_continuity(self) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="2",
                timestamp="0",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="",
                timestamp="",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="4",
                timestamp="40",
            )
        )
        await _drain_transport_diagnostics(transport)

        assert degraded == []

    @pytest.mark.asyncio
    async def test_boolean_media_metadata_breaks_gap_tracking_continuity(self) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="2",
                timestamp="0",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number=True,  # type: ignore[arg-type]
                timestamp=True,  # type: ignore[arg-type]
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="4",
                timestamp="40",
            )
        )
        await _drain_transport_diagnostics(transport)

        assert degraded == []


class _BlockingTwilioWebSocket:
    """Async-iterable fake ws that blocks in ``__anext__`` until released.

    Mirrors ``_RaceServerWS`` in ``test_degraded_events`` but is async-iterable
    so it can suspend inside ``TwilioTransport._handle_connection``'s inline
    ``async for raw in ws`` receive loop.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_sends = False
        self.closed_with: tuple[object, ...] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __aiter__(self) -> _BlockingTwilioWebSocket:
        return self

    async def __anext__(self) -> str:
        self.entered.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def send(self, message: str) -> None:
        if self.fail_sends:
            close_frame = websockets.frames.Close(1006, "abnormal")
            raise websockets.exceptions.ConnectionClosed(close_frame, None)
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args


class TestTwilioStreamLifecycleRaces:
    @pytest.mark.asyncio
    async def test_server_transport_stale_finally_does_not_tear_down_replacement(self) -> None:
        bus = EventBus()
        ended: list[str] = []
        bus.subscribe(CallEnded, lambda event: ended.append(event.call_sid))
        transport = TwilioTransport(event_bus=bus)

        old_ws = _BlockingTwilioWebSocket()
        new_ws = _BlockingTwilioWebSocket()

        old_task = asyncio.create_task(transport._handle_connection(old_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(old_ws.entered.wait(), timeout=1.0)

        # Establish an active stream so ``send_audio`` actually attempts a send.
        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))

        # ``send_audio`` notices the closed socket, emits the dead call's
        # ``CallEnded`` while it still owns the slot, then clears it, letting a
        # replacement connection be accepted before the stale ``finally`` runs.
        old_ws.fail_sends = True
        assert await transport.send_audio(make_chunk()) is False
        assert transport._ws is None
        assert ended == ["CALL1"]

        new_task = asyncio.create_task(transport._handle_connection(new_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(new_ws.entered.wait(), timeout=1.0)
        assert transport._ws is new_ws
        await transport._handle_message(_twilio_start_msg("STREAM2", "CALL2"))

        # Now let the stale old handler's ``finally`` run.
        old_ws.release.set()
        await asyncio.wait_for(old_task, timeout=1.0)
        await transport._drain_emit_tasks()

        # The replacement must survive: the stale finally must not null ``_ws``,
        # wipe the new stream, emit a spurious CallEnded for the replacement
        # call, or poison the queue.
        assert transport._ws is new_ws
        assert transport.stream_sid == "STREAM2"
        assert transport.call_sid == "CALL2"
        assert ended == ["CALL1"]
        assert transport._in_queue.empty()

        new_ws.release.set()
        await asyncio.wait_for(new_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_server_transport_ignores_stale_stop_and_media_after_new_start(self) -> None:
        event_bus = EventBus()
        ended: list[str] = []
        event_bus.subscribe(CallEnded, lambda event: ended.append(event.call_sid))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_start_msg("STREAM2", "CALL2"))
        await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM1"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM1"))

        assert transport.stream_sid == "STREAM2"
        assert transport.call_sid == "CALL2"
        assert transport._in_queue.empty()
        assert ended == []

        await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM2"))
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM2"))

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ended == ["CALL2"]

    @pytest.mark.asyncio
    async def test_connection_transport_ignores_stale_stop_and_media_after_new_start(self) -> None:
        event_bus = EventBus()
        ended: list[str] = []
        event_bus.subscribe(CallEnded, lambda event: ended.append(event.call_sid))
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_start_msg("STREAM2", "CALL2"))
        await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM1"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM1"))

        assert transport.stream_sid == "STREAM2"
        assert transport.call_sid == "CALL2"
        assert transport._in_queue.empty()
        assert ended == []

        await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM2"))
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM2"))

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ended == ["CALL2"]


class TestTwilioProtocolConsolidation:
    """Lock the shared Twilio protocol mixin and the drift it fixed."""

    def test_both_transports_share_protocol_mixin(self) -> None:
        # Structural lock: prevents the two classes from re-forking their own
        # verbatim copy of the Media Streams wire protocol.
        assert issubclass(TwilioTransport, _TwilioProtocolMixin)
        assert issubclass(TwilioConnectionTransport, _TwilioProtocolMixin)

    def test_router_registers_every_twilio_inbound_event(self) -> None:
        assert set(_TwilioProtocolMixin._MESSAGE_HANDLERS) == {
            "connected",
            "start",
            "media",
            "dtmf",
            "stop",
            "mark",
        }

    @pytest.mark.parametrize("raw", ["{", "[]", json.dumps({"event": []})])
    @pytest.mark.asyncio
    async def test_router_ignores_malformed_messages(self, raw: str) -> None:
        transport = TwilioTransport(event_bus=EventBus())

        await transport._handle_message(raw)

        assert transport.stream_sid is None
        assert transport.call_sid is None

    @pytest.mark.asyncio
    async def test_router_ignores_mark_with_non_string_name(self) -> None:
        event_bus = EventBus()
        marks: list[str] = []
        event_bus.subscribe(PlaybackMarkAck, lambda event: marks.append(event.mark_name))
        transport = TwilioTransport(event_bus=event_bus)
        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))

        await transport._handle_message(
            json.dumps(
                {
                    "event": "mark",
                    "streamSid": "STREAM1",
                    "mark": {"name": ["invalid"]},
                }
            )
        )

        assert marks == []

    @pytest.mark.asyncio
    async def test_connection_transport_logs_connected_and_unknown_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The connection variant previously dropped the ``connected`` branch and
        # the unknown-event log; the shared copy restores both.
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=EventBus())
        with caplog.at_level(logging.DEBUG, logger="easycat.transports.twilio_media"):
            await transport._handle_message(_twilio_connected_msg())
            await transport._handle_message(json.dumps({"event": "bogus"}))
        assert "Twilio connected event" in caplog.text
        assert "Unknown Twilio event" in caplog.text

    @pytest.mark.asyncio
    async def test_server_transport_logs_connected_and_unknown_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = TwilioTransport(event_bus=EventBus())
        with caplog.at_level(logging.DEBUG, logger="easycat.transports.twilio_media"):
            await transport._handle_message(_twilio_connected_msg())
            await transport._handle_message(json.dumps({"event": "bogus"}))
        assert "Twilio connected event" in caplog.text
        assert "Unknown Twilio event" in caplog.text


@pytest.mark.integration_socket
class TestTwilioTransport(_UsesPytestTcpPortFactory):
    """Tests for TwilioTransport with mocked Twilio messages."""

    def test_audio_contract_declares_distinct_tts_preference(self):
        transport = TwilioTransport(TwilioTransportConfig())

        assert transport.audio_format == PCM16_MONO_16K
        assert transport.preferred_tts_output_format == PCM16_MONO_8K

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)

        await transport.connect()
        assert transport.is_connected
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_receive_audio_from_twilio(self):
        """Twilio media messages produce PCM16 audio chunks."""
        port = self._unused_port()
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
        port = self._unused_port()
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
        port = self._unused_port()
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
    async def test_send_playback_mark_without_stream_raises(self):
        """Playback marks without an active stream must not report success."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        with pytest.raises(RuntimeError, match="active stream"):
            await transport.send_playback_mark("unit_mark")

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_dtmf_emitted_to_event_bus(self):
        """DTMF messages from Twilio are emitted as DTMF events."""
        port = self._unused_port()
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
        port = self._unused_port()
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
        port = self._unused_port()
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
        port = self._unused_port()
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
        port = self._unused_port()
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


class TestTwilioSendAudioConnectionClosed:
    """Twilio transports clear live-client state when send_audio hits a drop.

    No real socket needed: a fake ws whose ``send`` raises
    ``ConnectionClosed`` drives the error path directly.
    """

    class _ClosedWS:
        async def send(self, _message):
            raise websockets.exceptions.ConnectionClosed(None, None)

        async def close(self):
            return None

    @pytest.mark.asyncio
    async def test_server_variant_clears_state_on_connection_closed(self):
        config = TwilioTransportConfig(host="127.0.0.1", port=0)
        transport = TwilioTransport(config)
        transport._ws = self._ClosedWS()
        transport._stream_sid = "STREAM1"
        transport._client_connected.set()

        delivered = await transport.send_audio(_make_chunk(640, sample_rate=16000))

        assert delivered is False
        # Stale live-client state is cleared so has_client/is_connected stop
        # reporting a live peer after the socket drops.
        assert transport._ws is None
        assert transport.stream_sid is None
        assert not transport.has_client
        assert not transport._client_connected.is_set()

    @pytest.mark.asyncio
    async def test_server_variant_mark_raises_and_clears_state_on_connection_closed(self):
        config = TwilioTransportConfig(host="127.0.0.1", port=0)
        transport = TwilioTransport(config)
        transport._ws = self._ClosedWS()
        transport._stream_sid = "STREAM1"
        transport._client_connected.set()

        with pytest.raises(RuntimeError, match="disconnected"):
            await transport.send_playback_mark("unit_mark")

        assert transport._ws is None
        assert transport.stream_sid is None
        assert not transport.has_client
        assert not transport._client_connected.is_set()

    @pytest.mark.asyncio
    async def test_connection_variant_clears_state_on_connection_closed(self):
        transport = TwilioConnectionTransport(self._ClosedWS())
        transport._stream_sid = "STREAM1"
        transport._connected = True
        transport._client_connected.set()

        delivered = await transport.send_audio(_make_chunk(640, sample_rate=16000))

        assert delivered is False
        assert transport.stream_sid is None
        assert not transport.is_connected
        assert not transport._client_connected.is_set()

    @pytest.mark.asyncio
    async def test_connection_variant_mark_raises_and_clears_state_on_connection_closed(self):
        transport = TwilioConnectionTransport(self._ClosedWS())
        transport._stream_sid = "STREAM1"
        transport._connected = True
        transport._client_connected.set()

        with pytest.raises(RuntimeError, match="disconnected"):
            await transport.send_playback_mark("unit_mark")

        assert transport.stream_sid is None
        assert not transport.is_connected
        assert not transport._client_connected.is_set()


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

    def test_twiml_connect_stream_with_stream_token(self):
        xml = twiml_connect_stream("wss://example.com/stream", stream_token="token-1")
        assert f'<Parameter name="{TWILIO_STREAM_TOKEN_PARAMETER}" value="token-1"/>' in xml

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

    def test_twiml_stream_with_parameters_and_stream_token(self):
        xml = twiml_stream(
            "wss://example.com/stream",
            parameters={"crm_account_id": "ACC-42"},
            stream_token="token-1",
        )
        assert '<Parameter name="crm_account_id" value="ACC-42"/>' in xml
        assert f'<Parameter name="{TWILIO_STREAM_TOKEN_PARAMETER}" value="token-1"/>' in xml
        assert "</Stream>" in xml
