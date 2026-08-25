"""Telnyx media-streams transport tests (offline, scripted sockets)."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    DTMF,
    CallEnded,
    EventBus,
    PlaybackMarkAck,
    TransportDegraded,
)
from easycat.telephony._stream_tokens import StreamTokenStore
from easycat.transports._g711 import _mulaw_encode
from easycat.transports.telnyx_media import (
    _DEGRADED_TELNYX_ERROR,
    _DEGRADED_TELNYX_MEDIA_FORMAT,
    _DEGRADED_TELNYX_SEQUENCE_GAP,
    TelnyxConnectionTransport,
    TelnyxTransport,
    TelnyxTransportConfig,
)

# ── Fakes ─────────────────────────────────────────────────────────


class _DummyTelnyxWebSocket:
    def __init__(self, *, path: str | None = None) -> None:
        self.sent: list[str] = []
        self.closed_with: tuple[object, ...] | None = None
        self.request = SimpleNamespace(path=path) if path else None

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args


class _ScriptedTelnyxWebSocket:
    def __init__(self, *messages: str, path: str | None = None) -> None:
        self._messages = list(messages)
        self.sent: list[str] = []
        self.closed_with: tuple[object, ...] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if path is not None:
            self.request = SimpleNamespace(path=path)

    def __aiter__(self) -> _ScriptedTelnyxWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        self.entered.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args
        self.release.set()


def make_telnyx_bus() -> tuple[EventBus, dict[type[Any], list[Any]]]:
    bus = EventBus()
    collected: dict[type[Any], list[Any]] = {}

    def collect(event: Any) -> None:
        collected.setdefault(type(event), []).append(event)

    bus.subscribe_all(collect)
    return bus, collected


async def drain(transport: TelnyxTransport | TelnyxConnectionTransport) -> None:
    await transport._drain_emit_tasks()


def _start_msg(
    *,
    encoding: str = "L16",
    sample_rate: int = 16000,
    stream_id: str = "ST1",
    call_control_id: str = "CC1",
    client_state: str | None = None,
) -> str:
    start: dict[str, Any] = {
        "stream_id": stream_id,
        "call_control_id": call_control_id,
        "media_format": {
            "encoding": encoding,
            "sample_rate": sample_rate,
            "channels": 1,
        },
        "from": "+15550001111",
        "to": "+15550002222",
    }
    if client_state is not None:
        start["client_state"] = client_state
    return json.dumps({"event": "start", "start": start})


def _media_msg(
    payload: bytes,
    *,
    sequence_number: str = "1",
    track: str = "inbound",
) -> str:
    return json.dumps(
        {
            "event": "media",
            "media": {
                "payload": base64.b64encode(payload).decode("ascii"),
                "track": track,
                "sequence_number": sequence_number,
            },
        }
    )


def _pcm_silence(duration_ms: int, rate: int = 16000) -> bytes:
    count = rate * duration_ms // 1000
    return b"\x00\x00" * count


# ── Config ────────────────────────────────────────────────────────


class TestTelnyxTransportConfig:
    def test_defaults_are_l16_at_16k(self) -> None:
        config = TelnyxTransportConfig()

        assert config.codec == "L16"
        assert config.sampling_rate == 16000
        assert config.send_silence_when_idle is True
        assert config.preferred_tts_output_format == PCM16_MONO_16K

    def test_pcmu_derives_8k_tts_format(self) -> None:
        config = TelnyxTransportConfig(codec="PCMU", sampling_rate=8000)

        assert config.preferred_tts_output_format.sample_rate == 8000

    @pytest.mark.parametrize(
        ("codec", "rate"),
        [("OPUS", 48000), ("PCMU", 16000), ("L16", 12345)],
    )
    def test_rejects_invalid_codec_combinations(self, codec: str, rate: int) -> None:
        with pytest.raises(ValueError):
            TelnyxTransportConfig(codec=codec, sampling_rate=rate)  # type: ignore[arg-type]

    def test_rejects_bad_token_timeout(self) -> None:
        with pytest.raises(ValueError, match="positive finite"):
            TelnyxTransportConfig(stream_token_validation_timeout_s=0)


# ── Start-frame negotiation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_start_negotiates_l16_passthrough() -> None:
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = _DummyTelnyxWebSocket()

    await transport._handle_message(_start_msg(encoding="L16", sample_rate=16000))

    assert transport.stream_id == "ST1"
    assert transport.call_control_id == "CC1"
    assert transport.call_identity is not None
    assert transport.call_identity.caller_number == "+15550001111"


@pytest.mark.asyncio
async def test_start_negotiates_pcmu_fallback() -> None:
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = _DummyTelnyxWebSocket()

    await transport._handle_message(_start_msg(encoding="PCMU", sample_rate=8000))

    assert transport._negotiated_encoding == "PCMU"
    assert transport._negotiated_sample_rate == 8000


@pytest.mark.asyncio
async def test_unsupported_encoding_degrades_fatal_and_closes() -> None:
    bus, collected = make_telnyx_bus()
    ws = _DummyTelnyxWebSocket()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = ws

    await transport._handle_message(_start_msg(encoding="OPUS"))
    await drain(transport)

    assert ws.closed_with == (4003, "Unsupported media format")
    degraded = collected.get(TransportDegraded, [])
    assert any(event.reason == _DEGRADED_TELNYX_MEDIA_FORMAT and event.fatal for event in degraded)


@pytest.mark.asyncio
async def test_start_without_stream_id_is_rejected() -> None:
    ws = _DummyTelnyxWebSocket()
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = ws

    message = json.dumps({"event": "start", "start": {"call_control_id": "CC1"}})
    await transport._handle_message(message)

    assert ws.closed_with == (4003, "Missing stream_id")


# ── Media decoding ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l16_media_enqueues_internal_pcm16() -> None:
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(_start_msg())

    pcm = _pcm_silence(20)
    await transport._handle_message(_media_msg(pcm))

    chunk = transport._in_queue.get_nowait()
    assert isinstance(chunk, AudioChunk)
    assert chunk.format.sample_rate == 16000


@pytest.mark.asyncio
async def test_pcmu_start_media_is_decoded_and_resampled() -> None:
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(_start_msg(encoding="PCMU"))

    mulaw = _mulaw_encode(_pcm_silence(20, rate=8000))
    await transport._handle_message(_media_msg(mulaw, sequence_number="2"))

    chunk = transport._in_queue.get_nowait()
    assert len(chunk.data) >= len(mulaw) * 2


@pytest.mark.asyncio
async def test_outbound_track_frames_are_dropped() -> None:
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(_start_msg())

    await transport._handle_message(_media_msg(_pcm_silence(20), track="outbound"))

    assert transport._in_queue.empty()


# ── Sequence diagnostics ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sequence_gap_emits_degraded() -> None:
    bus, collected = make_telnyx_bus()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(_start_msg())

    await transport._handle_message(_media_msg(_pcm_silence(20), sequence_number="2"))
    await transport._handle_message(_media_msg(_pcm_silence(20), sequence_number="9"))
    await drain(transport)

    degraded = [e for e in collected.get(TransportDegraded, []) if not e.fatal]
    assert any(e.reason == _DEGRADED_TELNYX_SEQUENCE_GAP for e in degraded)


@pytest.mark.asyncio
async def test_top_level_sequence_number_drives_gap_detection() -> None:
    """Telnyx carries sequence_number beside ``event``, not inside the payload."""
    bus, collected = make_telnyx_bus()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(
        json.dumps(
            {
                "event": "start",
                "sequence_number": "1",
                "stream_id": "ST1",
                "start": {
                    "call_control_id": "CC1",
                    "media_format": {"encoding": "L16", "sample_rate": 16000, "channels": 1},
                },
            }
        )
    )
    # The start frame carried stream_id at the top level only, so the stream
    # must still be admitted rather than closed with 4003.
    assert transport._stream_id == "ST1"

    for sequence in ("2", "9"):
        await transport._handle_message(
            json.dumps(
                {
                    "event": "media",
                    "sequence_number": sequence,
                    "stream_id": "ST1",
                    "media": {
                        "track": "inbound",
                        "payload": base64.b64encode(_pcm_silence(20)).decode("ascii"),
                    },
                }
            )
        )
    await drain(transport)

    degraded = [e for e in collected.get(TransportDegraded, []) if not e.fatal]
    assert any(e.reason == _DEGRADED_TELNYX_SEQUENCE_GAP for e in degraded)


# ── DTMF ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dtmf_digit_emits_neutral_event() -> None:
    bus, collected = make_telnyx_bus()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(_start_msg())

    await transport._handle_message(json.dumps({"event": "dtmf", "dtmf": {"digit": "5"}}))

    digits = [event.digit for event in collected.get(DTMF, [])]
    assert digits == ["5"]


# ── Marks and clear semantics ─────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_ack_and_expire_on_clear() -> None:
    bus, collected = make_telnyx_bus()
    ws = _DummyTelnyxWebSocket()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = ws
    transport._stream_id = "ST1"

    await transport.send_mark("m1")
    assert ws.sent[-1] == json.dumps({"event": "mark", "mark": {"name": "m1"}})
    assert "m1" in transport._pending_marks

    # A late ack clears the pending ledger.
    await transport._handle_message(json.dumps({"event": "mark", "mark": {"name": "m1"}}))
    assert "m1" not in transport._pending_marks

    # clear_audio expires outstanding marks locally.
    await transport.send_mark("m2")
    await transport.clear_audio()
    await drain(transport)

    assert ws.sent[-1] == json.dumps({"event": "clear", "stream_id": "ST1"})
    acked = [event.mark_name for event in collected.get(PlaybackMarkAck, [])]
    assert "m1" in acked
    assert "m2" in acked
    assert transport._pending_marks == {}


@pytest.mark.asyncio
async def test_negotiating_pcmu_resizes_outbound_frame_bounds() -> None:
    """A start-frame codec switch must resize send coalescing to the wire codec."""
    ws = _DummyTelnyxWebSocket()
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = ws
    transport._stream_id = "ST1"

    await transport._handle_message(_start_msg(encoding="PCMU", sample_rate=8000))

    for _ in range(10):
        sent = await transport.send_audio(
            AudioChunk(data=_pcm_silence(120, rate=8000), format=PCM16_MONO_16K)
        )
        assert sent is True

    payloads = [
        json.loads(message)["media"]["payload"] for message in ws.sent if '"media"' in message
    ]
    assert payloads
    # PCMU @ 8 kHz is 8 bytes/ms; every wire frame stays within ~100 ms (800 B)
    # even though the transport was constructed with L16-sized bounds.
    assert all(len(base64.b64decode(payload)) <= 800 for payload in payloads)


# ── Error events ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_error_is_non_fatal() -> None:
    bus, collected = make_telnyx_bus()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = _DummyTelnyxWebSocket()
    transport._stream_id = "ST1"

    await transport._handle_message(
        json.dumps({"event": "error", "error": {"code": "100005", "message": "slow down"}})
    )
    await drain(transport)

    degraded = collected.get(TransportDegraded, [])
    assert len(degraded) == 1
    assert degraded[0].fatal is False


@pytest.mark.asyncio
async def test_malformed_error_is_fatal() -> None:
    bus, collected = make_telnyx_bus()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = _DummyTelnyxWebSocket()
    transport._stream_id = "ST1"

    await transport._handle_message(
        json.dumps({"event": "error", "error": {"code": "100003", "message": "bad json"}})
    )
    await drain(transport)

    degraded = collected.get(TransportDegraded, [])
    assert len(degraded) == 1
    assert degraded[0].reason == _DEGRADED_TELNYX_ERROR
    assert degraded[0].fatal is True


# ── Stop / call lifecycle ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_emits_call_ended_once() -> None:
    bus, collected = make_telnyx_bus()
    transport = TelnyxTransport(TelnyxTransportConfig(), event_bus=bus)
    transport._ws = _DummyTelnyxWebSocket()
    await transport._handle_message(_start_msg())

    stop = json.dumps({"event": "stop", "stop": {"stream_id": "ST1"}})
    await transport._handle_message(stop)
    await transport._handle_message(stop)
    await drain(transport)

    ended = collected.get(CallEnded, [])
    assert len(ended) == 1
    assert ended[0].call_sid == "CC1"


# ── Outbound coalescing ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_audio_coalesces_into_bounded_frames() -> None:
    ws = _DummyTelnyxWebSocket()
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = ws
    transport._stream_id = "ST1"

    for _ in range(10):
        sent = await transport.send_audio(AudioChunk(data=_pcm_silence(30), format=PCM16_MONO_16K))
        assert sent is True

    payloads = [
        json.loads(message)["media"]["payload"] for message in ws.sent if '"media"' in message
    ]
    assert payloads
    # Every wire frame stays within the ~100 ms cap (3200 bytes at L16@16k).
    assert all(len(base64.b64decode(payload)) <= 3200 for payload in payloads)


@pytest.mark.asyncio
async def test_send_mark_flushes_coalesced_audio_first() -> None:
    ws = _DummyTelnyxWebSocket()
    transport = TelnyxTransport(TelnyxTransportConfig())
    transport._ws = ws
    transport._stream_id = "ST1"

    await transport.send_audio(AudioChunk(data=_pcm_silence(30), format=PCM16_MONO_16K))
    await transport.send_mark("flush")

    kinds = [json.loads(message)["event"] for message in ws.sent]
    assert kinds.index("media") < kinds.index("mark")


# ── Server admission ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_transport_requires_validator_on_public_bind() -> None:
    transport = TelnyxTransport(TelnyxTransportConfig(host="0.0.0.0", unsafe_allow_no_auth=False))

    with pytest.raises(ValueError, match="stream_token_validator"):
        await transport.connect()


# ── Connection transport: token-bound wait_for_start ──────────────


@pytest.mark.asyncio
async def test_wait_for_start_consumes_valid_handshake_token() -> None:
    store = StreamTokenStore("secret")
    token = store.issue()
    ws = _ScriptedTelnyxWebSocket(_start_msg(), path=f"/?EasyCatStreamToken={token}")
    config = TelnyxTransportConfig(stream_token_validator=store.consume_start)
    transport = TelnyxConnectionTransport(ws, config=config)

    accepted = await transport.wait_for_start(timeout_s=2)

    assert accepted is True
    assert transport._pending_start_message is not None

    await transport.disconnect()


@pytest.mark.asyncio
async def test_wait_for_start_rejects_invalid_or_missing_token() -> None:
    store = StreamTokenStore("secret")
    ws = _ScriptedTelnyxWebSocket(_start_msg(), path="/?EasyCatStreamToken=bogus")
    config = TelnyxTransportConfig(stream_token_validator=store.consume_start)
    transport = TelnyxConnectionTransport(ws, config=config)

    accepted = await transport.wait_for_start(timeout_s=2)

    assert accepted is False
    assert ws.closed_with == (4003, "Missing or invalid stream token")

    await transport.disconnect()


@pytest.mark.asyncio
async def test_connect_replays_pending_start_and_streams_audio() -> None:
    store = StreamTokenStore("secret")
    token = store.issue()
    media_frame = _media_msg(_pcm_silence(20), sequence_number="2")
    ws = _ScriptedTelnyxWebSocket(
        _start_msg(client_state=None),
        media_frame,
        path=f"/?EasyCatStreamToken={token}",
    )
    config = TelnyxTransportConfig(stream_token_validator=store.consume_start)
    transport = TelnyxConnectionTransport(ws, config=config)

    assert await transport.wait_for_start(timeout_s=2) is True
    await transport.connect()

    assert transport.is_connected
    assert transport.call_control_id == "CC1"

    await transport.send_audio(AudioChunk(data=_pcm_silence(120), format=PCM16_MONO_16K))

    await transport.disconnect()
    assert not transport.is_connected
