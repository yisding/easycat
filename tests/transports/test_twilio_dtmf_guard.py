"""Protected-playback (in-band DTMF) clear guard on both Twilio transports.

The guard defers a barge-in ``clear`` while EasyCat's own DTMF tones play so
the key sequence is never cut on the wire, holds untagged outbound audio
behind the deferred clear, and releases (clear first, then held audio) when
Twilio acknowledges the guard mark. Every scenario runs against both
:class:`TwilioTransport` (server) and :class:`TwilioConnectionTransport`
(accepted socket), which share the guard through ``_TwilioProtocolMixin``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from typing import Any

import pytest
import websockets

from easycat._dtmf_tones import render_dtmf_pcm16
from easycat.audio_format import PCM16_MONO_8K, AudioChunk
from easycat.events import DTMF, EventBus, PlaybackMarkAck
from easycat.runtime.capabilities import (
    PROTECTED_PLAYBACK_CHUNK_ATTR,
    ProtectedPlayback,
    protected_playback,
)
from easycat.transports.twilio_media import (
    TwilioConnectionTransport,
    TwilioTransport,
    TwilioTransportConfig,
    mulaw_to_pcm16,
)
from tests.telephony._dtmf_decoder import decode_dtmf

TwilioAny = TwilioTransport | TwilioConnectionTransport

_FRAME_BYTES = 320  # 20 ms of 8 kHz PCM16
_KINDS = ["server", "connection"]


class _RecordingTwilioWebSocket:
    """Fake accepted socket that records every outbound JSON frame."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_sends = False
        self.closed_with: tuple[object, ...] | None = None

    async def send(self, message: str) -> None:
        if self.fail_sends:
            close_frame = websockets.frames.Close(1006, "abnormal")
            raise websockets.exceptions.ConnectionClosedError(close_frame, None)
        self.sent.append(json.loads(message))

    async def close(self, *args: object) -> None:
        self.closed_with = args

    def events(self) -> list[tuple[str, str]]:
        """Return ``(event, detail)`` pairs; media detail is its streamSid."""
        out: list[tuple[str, str]] = []
        for frame in self.sent:
            event = frame["event"]
            if event == "mark":
                out.append(("mark", frame["mark"]["name"]))
            else:
                out.append((event, frame["streamSid"]))
        return out

    def kinds(self) -> list[str]:
        """Collapse consecutive media frames so ordering assertions stay short."""
        out: list[str] = []
        for event, detail in self.events():
            label = f"mark:{detail}" if event == "mark" else event
            if label == "media" and out and out[-1] == "media":
                continue
            out.append(label)
        return out

    def media_pcm(self) -> bytes:
        payloads = [
            base64.b64decode(frame["media"]["payload"])
            for frame in self.sent
            if frame["event"] == "media"
        ]
        return mulaw_to_pcm16(b"".join(payloads), target_rate=8000)


def _start_msg(stream_sid: str = "MZ1", call_sid: str = "CA1") -> str:
    return json.dumps(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "callSid": call_sid,
                "tracks": ["inbound"],
            },
        }
    )


def _mark_msg(name: str, stream_sid: str = "MZ1") -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


def _stop_msg(stream_sid: str = "MZ1") -> str:
    return json.dumps({"event": "stop", "streamSid": stream_sid})


def _dtmf_msg(digit: str, stream_sid: str = "MZ1") -> str:
    return json.dumps(
        {
            "event": "dtmf",
            "streamSid": stream_sid,
            "dtmf": {"digit": digit, "track": "inbound_track"},
        }
    )


async def _started(
    kind: str,
    *,
    config: TwilioTransportConfig | None = None,
    event_bus: EventBus | None = None,
) -> tuple[TwilioAny, _RecordingTwilioWebSocket]:
    ws = _RecordingTwilioWebSocket()
    transport: TwilioAny
    if kind == "server":
        transport = TwilioTransport(config, event_bus=event_bus)
        transport._ws = ws  # type: ignore[assignment]
        transport._client_connected.set()
    else:
        transport = TwilioConnectionTransport(
            ws,  # type: ignore[arg-type]
            event_bus=event_bus,
            config=config,
        )
        transport._connected = True
    await transport._handle_message(_start_msg())
    assert transport.stream_sid == "MZ1"
    return transport, ws


def _tone_frames(digits: str, token: object | None) -> list[AudioChunk]:
    pcm = render_dtmf_pcm16(digits)
    frames = []
    for offset in range(0, len(pcm), _FRAME_BYTES):
        chunk = AudioChunk(data=pcm[offset : offset + _FRAME_BYTES], format=PCM16_MONO_8K)
        if token is not None:
            setattr(chunk, PROTECTED_PLAYBACK_CHUNK_ATTR, token)
        frames.append(chunk)
    return frames


def _speech_chunk() -> AudioChunk:
    return AudioChunk(data=b"\x10\x00" * 160, format=PCM16_MONO_8K)


async def _send_all(transport: TwilioAny, frames: Sequence[AudioChunk]) -> None:
    for frame in frames:
        assert await transport.send_audio(frame) is True


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _fence(transport: TwilioAny, ws: _RecordingTwilioWebSocket) -> None:
    """Run one fence round-trip, acknowledging the fence mark like Twilio."""
    waiting = asyncio.create_task(transport.wait_playout_fence(timeout_s=1.0))
    await _settle()
    fence_names = [detail for event, detail in ws.events() if event == "mark"]
    assert fence_names and fence_names[-1].startswith("easycat_dtmf_fence_")
    await transport._handle_message(_mark_msg(fence_names[-1]))
    assert await waiting is True


@pytest.mark.parametrize("kind", _KINDS)
class TestTwilioProtectedPlayback:
    async def test_capability_is_exposed(self, kind: str) -> None:
        transport, _ws = await _started(kind)

        assert isinstance(transport, ProtectedPlayback)
        assert protected_playback(transport) is transport

    async def test_fence_tones_then_guard_mark_decode_to_digits(self, kind: str) -> None:
        transport, ws = await _started(kind)

        await _fence(transport, ws)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await _send_all(transport, _tone_frames("12#", token))
        done = await transport.end_protected_playback(token)

        assert ws.kinds() == ["mark:easycat_dtmf_fence_1", "media", "mark:easycat_dtmf_1"]
        assert decode_dtmf(ws.media_pcm()) == "12#"
        assert not done.done()

        await transport._handle_message(_mark_msg("easycat_dtmf_1"))

        assert done.result() == "played"
        # An ack with no deferred clear writes no clear.
        assert "clear" not in ws.kinds()
        assert transport._hold_released.is_set()

    async def test_clear_during_fence_goes_to_the_wire_immediately(self, kind: str) -> None:
        transport, ws = await _started(kind)

        waiting = asyncio.create_task(transport.wait_playout_fence(timeout_s=1.0))
        await _settle()
        await transport.clear_audio()

        assert ws.kinds() == ["mark:easycat_dtmf_fence_1", "clear"]
        # Twilio echoes pending marks on clear, so the fence ends promptly.
        await transport._handle_message(_mark_msg("easycat_dtmf_fence_1"))
        assert await waiting is True

    async def test_clear_between_tone_frames_is_deferred(self, kind: str) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        frames = _tone_frames("1234#", token)
        half = len(frames) // 2

        await _send_all(transport, frames[:half])
        await asyncio.create_task(transport.clear_audio())  # a barge-in from another task
        await _send_all(transport, frames[half:])
        done = await transport.end_protected_playback(token)

        assert "clear" not in ws.kinds()
        assert decode_dtmf(ws.media_pcm()) == "1234#"

        await transport._handle_message(_mark_msg("easycat_dtmf_1"))

        assert done.result() == "played"
        assert ws.kinds() == ["media", "mark:easycat_dtmf_1", "clear"]

    async def test_held_audio_follows_the_deferred_clear(self, kind: str) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await _send_all(transport, _tone_frames("5", token))
        await transport.clear_audio()

        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()
        assert not held.done()

        done = await transport.end_protected_playback(token)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))

        assert await held is True
        assert done.result() == "played"
        assert ws.kinds() == ["media", "mark:easycat_dtmf_1", "clear", "media"]

    async def test_new_turn_audio_sent_during_tones_survives_the_deferred_clear(
        self,
        kind: str,
    ) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        frames = _tone_frames("9", token)

        await _send_all(transport, frames[:3])
        await transport.clear_audio()
        new_turn = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()
        # Tagged tone frames still bypass the hold that pins the new turn.
        await _send_all(transport, frames[3:])
        assert not new_turn.done()
        tone_frames_sent = sum(1 for event, _ in ws.events() if event == "media")
        assert tone_frames_sent == len(frames)

        await transport.end_protected_playback(token)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))

        assert await new_turn is True
        events = ws.events()
        clear_index = events.index(("clear", "MZ1"))
        assert events[clear_index + 1 :] == [("media", "MZ1")]

    async def test_guard_deadline_times_out_then_clears_then_releases(
        self,
        kind: str,
    ) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=0.05)
        await _send_all(transport, _tone_frames("7", token))
        done = await transport.end_protected_playback(token)
        await transport.clear_audio()
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))

        assert await asyncio.wait_for(held, timeout=2.0) is True

        assert done.result() == "timeout"
        assert ws.kinds() == ["media", "mark:easycat_dtmf_1", "clear", "media"]
        # A late ack for the expired guard is harmless.
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))
        assert ws.kinds()[-1] == "media"

    async def test_stop_resolves_disconnected_and_releases_held_sends(self, kind: str) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await _send_all(transport, _tone_frames("3", token))
        done = await transport.end_protected_playback(token)
        await transport.clear_audio()
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()

        await transport._handle_message(_stop_msg())

        assert await asyncio.wait_for(held, timeout=1.0) is False
        assert done.result() == "disconnected"
        assert "clear" not in ws.kinds()
        assert ws.kinds()[-1] == "mark:easycat_dtmf_1"

    async def test_connection_closed_resolves_disconnected(self, kind: str) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        frames = _tone_frames("3", token)
        await transport.send_audio(frames[0])
        await transport.clear_audio()
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()

        ws.fail_sends = True
        assert await transport.send_audio(frames[1]) is False

        assert await asyncio.wait_for(held, timeout=1.0) is False
        done = await transport.end_protected_playback(token)
        assert done.result() == "disconnected"
        assert transport._hold_released.is_set()
        assert not transport._clear_deferred

    async def test_disconnect_resolves_disconnected(self, kind: str) -> None:
        transport, _ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await transport.clear_audio()
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()

        await transport.disconnect()

        assert await asyncio.wait_for(held, timeout=1.0) is False
        done = await transport.end_protected_playback(token)
        assert done.result() == "disconnected"

    async def test_replacement_start_leaves_the_new_stream_untouched(self, kind: str) -> None:
        """gh 1103: an old stream's guard never holds or clears the replacement."""
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await _send_all(transport, _tone_frames("1", token))
        await transport.clear_audio()
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()

        await transport._handle_message(_start_msg("MZ2", "CA2"))

        # Old audio is neither sent on the dead stream nor leaked onto MZ2.
        assert await asyncio.wait_for(held, timeout=1.0) is False
        done = await transport.end_protected_playback(token)
        assert done.result() == "disconnected"
        assert ("mark", "easycat_dtmf_1") not in ws.events()
        assert not any(sid == "MZ2" for _event, sid in ws.events())

        # The replacement stream is unguarded: clear and audio flow at once.
        await transport.clear_audio()
        assert await transport.send_audio(_speech_chunk()) is True
        assert ws.events()[-2:] == [("clear", "MZ2"), ("media", "MZ2")]

        # A guard on MZ2 is unaffected by a late stop for the old stream.
        token2 = transport.begin_protected_playback(max_duration_s=5.0)
        await transport._handle_message(_stop_msg("MZ1"))
        assert transport._is_live_guard_token(token2)
        assert transport.stream_sid == "MZ2"

    async def test_inbound_dtmf_is_suppressed_during_guard_and_tail(self, kind: str) -> None:
        bus = EventBus()
        digits: list[str] = []
        bus.subscribe(DTMF, lambda event: digits.append(event.digit))
        config = TwilioTransportConfig(dtmf_echo_suppress_tail_ms=50)
        transport, _ws = await _started(kind, config=config, event_bus=bus)

        await transport._handle_message(_dtmf_msg("1"))
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await transport._handle_message(_dtmf_msg("2"))
        await transport.end_protected_playback(token)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))
        await transport._handle_message(_dtmf_msg("3"))  # inside the echo tail
        await asyncio.sleep(0.08)
        await transport._handle_message(_dtmf_msg("4"))
        await transport._drain_emit_tasks()

        assert digits == ["1", "4"]
        assert transport.suppressed_inbound_dtmf == 2

    async def test_inbound_dtmf_always_delivered_when_suppression_is_off(
        self,
        kind: str,
    ) -> None:
        bus = EventBus()
        digits: list[str] = []
        bus.subscribe(DTMF, lambda event: digits.append(event.digit))
        config = TwilioTransportConfig(suppress_inbound_dtmf_during_tones=False)
        transport, _ws = await _started(kind, config=config, event_bus=bus)

        token = transport.begin_protected_playback(max_duration_s=5.0)
        await transport._handle_message(_dtmf_msg("2"))
        await transport.end_protected_playback(token)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))
        await transport._handle_message(_dtmf_msg("3"))
        await transport._drain_emit_tasks()

        assert digits == ["2", "3"]
        assert transport.suppressed_inbound_dtmf == 0

    async def test_no_stream_raises_and_fence_reports_false(self, kind: str) -> None:
        ws = _RecordingTwilioWebSocket()
        transport: TwilioAny
        if kind == "server":
            transport = TwilioTransport()
        else:
            transport = TwilioConnectionTransport(ws)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="without an active stream"):
            transport.begin_protected_playback(max_duration_s=1.0)
        assert await transport.wait_playout_fence(timeout_s=0.1) is False
        with pytest.raises(ValueError, match="Unknown Twilio protected-playback token"):
            await transport.end_protected_playback(object())
        assert ws.sent == []

    async def test_invalid_durations_are_rejected(self, kind: str) -> None:
        transport, _ws = await _started(kind)

        with pytest.raises(ValueError, match="max_duration_s"):
            transport.begin_protected_playback(max_duration_s=float("nan"))
        with pytest.raises(ValueError, match="timeout_s"):
            await transport.wait_playout_fence(timeout_s=0)

    async def test_fence_times_out_without_an_echo(self, kind: str) -> None:
        transport, ws = await _started(kind)

        assert await transport.wait_playout_fence(timeout_s=0.02) is False
        assert ws.kinds() == ["mark:easycat_dtmf_fence_1"]
        assert transport._dtmf_fence_waiters == {}

    async def test_playback_mark_ack_is_still_emitted_for_dtmf_marks(self, kind: str) -> None:
        bus = EventBus()
        acks: list[str] = []
        bus.subscribe(PlaybackMarkAck, lambda event: acks.append(event.mark_name))
        transport, ws = await _started(kind, event_bus=bus)

        await _fence(transport, ws)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        await transport.end_protected_playback(token)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))
        await transport._drain_emit_tasks()

        assert acks == ["easycat_dtmf_fence_1", "easycat_dtmf_1"]

    async def test_abort_all_releases_held_audio_after_the_deferred_clear(
        self,
        kind: str,
    ) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=30.0)
        await _send_all(transport, _tone_frames("8", token))
        await transport.clear_audio()
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await _settle()

        transport.abort_protected_playback(None)

        assert await asyncio.wait_for(held, timeout=1.0) is True
        done = await transport.end_protected_playback(token)
        assert done.result() == "disconnected"
        # The stream is alive, so the barge-in clear still lands before held audio.
        assert ws.kinds() == ["media", "clear", "media"]

    async def test_abort_one_token_then_clear_flushes_the_deferred_clear_once(
        self,
        kind: str,
    ) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=30.0)
        await transport.clear_audio()

        transport.abort_protected_playback(token)
        transport.abort_protected_playback(object())  # unknown tokens are ignored
        await transport.clear_audio()

        assert ws.kinds() == ["clear"]
        assert transport._hold_released.is_set()
        assert not transport._clear_deferred

    async def test_stale_token_frames_are_held_like_untagged_audio(self, kind: str) -> None:
        transport, ws = await _started(kind)
        stale = transport.begin_protected_playback(max_duration_s=5.0)
        await transport.end_protected_playback(stale)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))

        live = transport.begin_protected_playback(max_duration_s=5.0)
        await transport.clear_audio()
        stale_frame = _tone_frames("1", stale)[0]
        held = asyncio.create_task(transport.send_audio(stale_frame))
        await _settle()
        assert not held.done()

        await transport.end_protected_playback(live)
        await transport._handle_message(_mark_msg("easycat_dtmf_2"))

        assert await held is True
        assert ws.kinds()[-2:] == ["clear", "media"]

    async def test_guard_expiring_during_next_fence_does_not_spin_the_loop(
        self,
        kind: str,
    ) -> None:
        """A deferred clear carried into the next guard keeps the hold closed."""
        transport, ws = await _started(kind)
        first = transport.begin_protected_playback(max_duration_s=0.05)
        await transport.clear_audio()  # deferred behind the first guard
        fence = asyncio.create_task(transport.wait_playout_fence(timeout_s=1.0))
        await asyncio.sleep(0.1)  # the first guard's timer fires mid-fence
        fence_name = [detail for event, detail in ws.events() if event == "mark"][-1]
        await transport._handle_message(_mark_msg(fence_name))
        assert await fence is True
        assert transport._clear_deferred
        assert first.future.result() == "timeout"  # type: ignore[attr-defined]

        second = transport.begin_protected_playback(max_duration_s=0.3)
        assert not transport._hold_released.is_set()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticking = asyncio.create_task(ticker())
        held = asyncio.create_task(transport.send_audio(_speech_chunk()))
        await asyncio.sleep(0.1)
        # The loop keeps running while the send is held, so the ack arrives.
        assert ticks >= 3
        assert not held.done()
        done = await transport.end_protected_playback(second)
        await transport._handle_message(_mark_msg("easycat_dtmf_1"))
        assert await asyncio.wait_for(held, timeout=1.0) is True
        ticking.cancel()

        assert done.result() == "played"
        assert ws.kinds()[-3:] == ["mark:easycat_dtmf_1", "clear", "media"]

    async def test_tagged_frames_after_replacement_start_are_rejected(self, kind: str) -> None:
        """gh 1103: an aborted burst's remaining frames never reach the new stream."""
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        frames = _tone_frames("1234#", token)
        half = len(frames) // 2
        await _send_all(transport, frames[:half])

        await transport._handle_message(_start_msg("MZ2", "CA2"))

        results = [await transport.send_audio(frame) for frame in frames[half:]]
        assert results == [False] * len(results)
        assert not any(sid == "MZ2" for _event, sid in ws.events())
        # Ordinary audio still flows on the replacement stream.
        assert await transport.send_audio(_speech_chunk()) is True
        assert ws.events()[-1] == ("media", "MZ2")

    async def test_tagged_frames_after_abort_are_rejected(self, kind: str) -> None:
        transport, ws = await _started(kind)
        token = transport.begin_protected_playback(max_duration_s=5.0)
        frames = _tone_frames("5", token)
        assert await transport.send_audio(frames[0]) is True

        transport.abort_protected_playback(token)

        assert await transport.send_audio(frames[1]) is False
        assert sum(1 for event, _ in ws.events() if event == "media") == 1


class TestTwilioTransportConfigDTMFGuard:
    def test_defaults(self) -> None:
        config = TwilioTransportConfig()

        assert config.suppress_inbound_dtmf_during_tones is True
        assert config.dtmf_echo_suppress_tail_ms == 500

    @pytest.mark.parametrize("tail_ms", [-1, 5001, 1.5, True])
    def test_rejects_invalid_tail(self, tail_ms: Any) -> None:
        with pytest.raises(ValueError, match="dtmf_echo_suppress_tail_ms"):
            TwilioTransportConfig(dtmf_echo_suppress_tail_ms=tail_ms)

    def test_rejects_non_bool_suppression_flag(self) -> None:
        with pytest.raises(TypeError, match="suppress_inbound_dtmf_during_tones"):
            TwilioTransportConfig(suppress_inbound_dtmf_during_tones=1)  # type: ignore[arg-type]


def test_protected_playback_lookup_requires_every_hook() -> None:
    class _Partial:
        async def wait_playout_fence(self, *, timeout_s: float) -> bool:
            return False

    assert protected_playback(None) is None
    assert protected_playback(object()) is None
    assert protected_playback(_Partial()) is None
