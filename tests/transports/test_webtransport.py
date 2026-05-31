"""Tests for the WebTransport transport.

Coverage strategy:
  - Unit: ``_ControlCodec`` framing and ``_WebTransportSession`` stream
    dispatch via a fake :class:`H3Connection`.  No network.
  - Conformance: WebTransportTransport / WebTransportConnectionTransport
    satisfy the Transport protocol surface.
  - Integration: a loopback aioquic CONNECT-webtransport handshake against a
    real :class:`WebTransportServer` covering multi-client behaviour
    (marked ``integration_socket``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import EventBus, TransportDegraded
from easycat.providers import Transport
from easycat.transports.webtransport import (
    _DEGRADED_BARGE_IN_RESET_FAILED,
    _DEGRADED_CONTROL_CODEC_POISONED,
    _DEGRADED_INBOUND_QUEUE_FULL,
    _DEGRADED_OUTBOUND_QUEUE_FULL,
    _DEGRADED_OUTBOUND_WRITER_CRASHED,
    _DEGRADED_REJECTED_STREAM_FLOOD,
    _MAX_CONTROL_FRAME_BYTES,
    _MAX_REJECTED_STREAMS,
    _OUTBOUND_SEND_BUFFER_HIGH_WATER,
    _TAG_AUDIO,
    _TAG_CONTROL,
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransport,
    WebTransportTransportConfig,
    _ControlCodec,
    _get_protocol_class,
    _WebTransportSession,
)

from .conftest import find_free_port


def _audio_frame(pcm: bytes, rate: int = 16000) -> bytes:
    """Client→server audio framing: ``[tag][4-byte BE sample-rate][PCM]``.

    Symmetric with the server→client framing — the mic rate is inline so it
    can't race a ``config`` control frame on an independent QUIC stream.
    """
    return bytes([_TAG_AUDIO]) + struct.pack(">I", rate) + pcm


def _aioquic_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("aioquic") is not None


# ── _ControlCodec ─────────────────────────────────────────────────


class TestControlCodec:
    def test_encode_round_trip(self) -> None:
        codec = _ControlCodec()
        msg = {"type": "config", "sample_rate": 48000}
        encoded = _ControlCodec.encode(msg)
        (length,) = struct.unpack_from(">I", encoded, 0)
        assert length == len(encoded) - 4
        assert codec.feed(encoded) == [msg]

    def test_feed_partial_frames(self) -> None:
        codec = _ControlCodec()
        msg = {"type": "ready"}
        encoded = _ControlCodec.encode(msg)
        out: list[dict[str, Any]] = []
        for i in range(len(encoded)):
            out.extend(codec.feed(encoded[i : i + 1]))
        assert out == [msg]

    def test_feed_multiple_frames_in_one_chunk(self) -> None:
        codec = _ControlCodec()
        a = _ControlCodec.encode({"type": "start"})
        b = _ControlCodec.encode({"type": "stop"})
        assert codec.feed(a + b) == [{"type": "start"}, {"type": "stop"}]

    def test_malformed_json_is_skipped(self) -> None:
        codec = _ControlCodec()
        bad = struct.pack(">I", 4) + b"\xff\xff\xff\xff"
        good = _ControlCodec.encode({"type": "ready"})
        assert codec.feed(bad + good) == [{"type": "ready"}]

    def test_oversized_length_prefix_poisons_codec(self) -> None:
        """A malicious uint32 length prefix must not pin a multi-GB buffer."""
        codec = _ControlCodec()
        # Advertise a frame bigger than the cap; codec should refuse to grow.
        oversized = struct.pack(">I", 1 << 30)  # 1 GiB
        assert codec.feed(oversized + b"X") == []
        assert codec.poisoned is True
        # Subsequent valid frames are now dropped — the stream is considered
        # malicious until the session resets it.
        good = _ControlCodec.encode({"type": "ready"})
        assert codec.feed(good) == []


# ── _WebTransportSession with fake H3Connection ───────────────────


class _FakeH3:
    def __init__(self) -> None:
        self.next_stream_id = 1000

    def create_webtransport_stream(self, session_id: int, is_unidirectional: bool = False) -> int:
        sid = self.next_stream_id
        self.next_stream_id += 1
        return sid


class _FakeQuicConnection:
    """Records ``reset_stream`` and raw WebTransport stream sends.

    WebTransport stream payload goes out as raw QUIC stream data (not H3
    ``DATA`` frames), so outbound framing assertions read ``sent`` here.
    """

    def __init__(self) -> None:
        self.resets: list[tuple[int, int]] = []
        self.sent: list[tuple[int, bytes]] = []

    def reset_stream(self, stream_id: int, error_code: int) -> None:
        self.resets.append((stream_id, error_code))

    def send_stream_data(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:  # noqa: FBT001, FBT002
        self.sent.append((stream_id, data))


class _FakeStreamSender:
    """Stand-in for ``aioquic.quic.stream.QuicStreamSender``.

    Only ``_buffer`` (unsent + unacked bytes) is read by the outbound
    backpressure gate.
    """

    def __init__(self, buffer: bytearray) -> None:
        self._buffer = buffer


class _FakeStream:
    def __init__(self, buffer: bytearray) -> None:
        self.sender = _FakeStreamSender(buffer)


class _FakeQuicProtocol:
    def __init__(self) -> None:
        self.transmit_calls = 0
        self._quic = _FakeQuicConnection()
        self.close_calls: list[tuple[int, str]] = []

    def transmit(self) -> None:
        self.transmit_calls += 1

    def close(self, error_code: int = 0, reason_phrase: str = "") -> None:
        self.close_calls.append((error_code, reason_phrase))


class _DegradedRecorder:
    """Captures ``_WebTransportSession`` degraded-event emissions.

    Matches the :data:`_DegradedEmitter` signature so it can be injected in
    place of the bound ``WebTransportConnectionTransport._emit_degraded``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, reason: str, detail: str = "", *, fatal: bool = False) -> None:
        self.calls.append((reason, detail, fatal))

    @property
    def reasons(self) -> list[str]:
        return [c[0] for c in self.calls]


def _make_session(
    *,
    target_rate: int = 16000,
    in_max: int = 10,
    out_max: int = 10,
    emit_degraded: Any = None,
) -> tuple[_WebTransportSession, _FakeH3, asyncio.Queue, asyncio.Queue]:
    fake_h3 = _FakeH3()
    in_q: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=in_max)
    out_q: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=out_max)
    session = _WebTransportSession(
        h3=fake_h3,  # type: ignore[arg-type]
        quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
        session_id=0,
        target_sample_rate=target_rate,
        audio_format=PCM16_MONO_16K,
        in_queue=in_q,
        out_queue=out_q,
        on_close=asyncio.Event(),
        emit_degraded=emit_degraded,
    )
    return session, fake_h3, in_q, out_q


class TestWebTransportSession:
    @pytest.mark.asyncio
    async def test_audio_tag_dispatches_inbound_pcm(self) -> None:
        session, _h3, in_q, _out_q = _make_session()
        pcm = b"\x00\x02\x00\x03\x00\x04"
        session.handle_stream_data(stream_id=4, data=_audio_frame(pcm), ended=False)
        chunk = in_q.get_nowait()
        assert isinstance(chunk, AudioChunk)
        assert chunk.data == pcm

    @pytest.mark.asyncio
    async def test_inline_rate_resamples_inbound_audio(self) -> None:
        """The mic rate is carried inline on the audio stream (not a
        ``config`` control frame), so it can't race the PCM bytes."""
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        # 48 samples @ 48 kHz → 16 samples @ 16 kHz (32 bytes).
        pcm_48k = b"\x00\x00" * 48
        session.handle_stream_data(stream_id=4, data=_audio_frame(pcm_48k, 48000), ended=False)
        chunk = in_q.get_nowait()
        assert chunk.format.sample_rate == 16000
        assert len(chunk.data) == 32

    @pytest.mark.asyncio
    async def test_inline_rate_header_split_across_deliveries(self) -> None:
        """The 4-byte rate header may be fragmented across stream-data
        deliveries (even split from the tag byte)."""
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        frame = _audio_frame(b"\x00\x00" * 48, 48000)
        session.handle_stream_data(stream_id=4, data=frame[:1], ended=False)  # tag
        session.handle_stream_data(stream_id=4, data=frame[1:3], ended=False)  # 2/4 rate
        assert in_q.empty()  # header still incomplete
        session.handle_stream_data(stream_id=4, data=frame[3:], ended=False)  # rest
        chunk = in_q.get_nowait()
        assert chunk.format.sample_rate == 16000
        assert len(chunk.data) == 32

    @pytest.mark.asyncio
    async def test_reopened_audio_stream_rereads_inline_rate(self) -> None:
        """A re-opened audio stream is fresh and self-describing — its
        inline rate header must be parsed again, not carried over."""
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        session.handle_stream_data(
            stream_id=4, data=_audio_frame(b"\x00\x00" * 48, 48000), ended=True
        )
        first = in_q.get_nowait()
        assert first.format.sample_rate == 16000
        assert len(first.data) == 32
        # Same stream id reused for a brand-new stream: header re-read.
        session.handle_stream_data(
            stream_id=4, data=_audio_frame(b"\x01\x02" * 8, 16000), ended=False
        )
        second = in_q.get_nowait()
        assert second.data == b"\x01\x02" * 8

    @pytest.mark.asyncio
    async def test_invalid_inline_rate_falls_back_to_target(self) -> None:
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        pcm = b"\x00\x01" * 8
        # rate 0 is invalid → fall back to the server target (no resample).
        session.handle_stream_data(stream_id=4, data=_audio_frame(pcm, 0), ended=False)
        chunk = in_q.get_nowait()
        assert chunk.format.sample_rate == 16000
        assert chunk.data == pcm

    @pytest.mark.asyncio
    async def test_inbound_queue_full_drops_frame(self) -> None:
        session, _h3, in_q, _out_q = _make_session(in_max=1)
        pcm = b"\x00\x00" * 4
        session.handle_stream_data(stream_id=4, data=_audio_frame(pcm), ended=False)
        session.handle_stream_data(stream_id=4, data=pcm, ended=False)
        assert in_q.qsize() == 1

    @pytest.mark.asyncio
    async def test_unknown_tag_is_ignored(self) -> None:
        session, _h3, in_q, _out_q = _make_session()
        session.handle_stream_data(stream_id=4, data=bytes([0xFF, 0x00]), ended=False)
        assert in_q.empty()

    @pytest.mark.asyncio
    async def test_outbound_audio_stream_is_self_describing(self) -> None:
        """The server→client audio stream carries its sample rate inline as
        ``[0x01][4-byte BE rate][PCM]``.  There is deliberately **no**
        ``audio_format`` control frame — on independent QUIC streams it would
        race the audio bytes and play TTS at the wrong rate.
        """
        session, _fake_h3, _in_q, out_q = _make_session()
        await session.start()
        try:
            chunk = AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K)
            await out_q.put(chunk)
            await asyncio.sleep(0.05)
        finally:
            await session.stop()

        sent = session._quic_protocol._quic.sent  # noqa: SLF001
        by_stream: dict[int, bytearray] = {}
        for sid, data in sent:
            by_stream.setdefault(sid, bytearray()).extend(data)

        # Control stream: [0x02] then a length-prefixed {"type":"ready"}.
        ctrl = next(b for b in by_stream.values() if b and b[0] == _TAG_CONTROL)
        (clen,) = struct.unpack_from(">I", ctrl, 1)
        assert json.loads(bytes(ctrl[5 : 5 + clen]).decode()) == {"type": "ready"}

        # Audio stream: [0x01][BE 16000][chunk.data], no JSON framing.
        audio = next(b for b in by_stream.values() if b and b[0] == _TAG_AUDIO)
        (rate,) = struct.unpack_from(">I", audio, 1)
        assert rate == 16000
        assert bytes(audio[5:]) == chunk.data

        # No audio_format control frame anywhere on the wire.
        assert b"audio_format" not in b"".join(bytes(b) for b in by_stream.values())

    @pytest.mark.asyncio
    async def test_rate_change_opens_fresh_audio_stream(self) -> None:
        """A TTS sample-rate change FINs the old stream and opens a new one
        whose inline header carries the new rate."""
        session, _fake_h3, _in_q, out_q = _make_session(target_rate=16000)
        await session.start()
        try:
            await out_q.put(AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K))
            await asyncio.sleep(0.05)
            first_sid = session._outbound_audio_stream_id  # noqa: SLF001
            assert first_sid is not None

            hi = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
            await out_q.put(AudioChunk(data=b"\x02\x03" * 4, format=hi))
            await asyncio.sleep(0.05)
            second_sid = session._outbound_audio_stream_id  # noqa: SLF001
            assert second_sid is not None and second_sid != first_sid
        finally:
            await session.stop()

        by_stream: dict[int, bytearray] = {}
        for sid, data in session._quic_protocol._quic.sent:  # noqa: SLF001
            by_stream.setdefault(sid, bytearray()).extend(data)
        # Old stream header advertises 16k; new one advertises 24k.
        assert struct.unpack_from(">I", by_stream[first_sid], 1)[0] == 16000
        assert struct.unpack_from(">I", by_stream[second_sid], 1)[0] == 24000

    @pytest.mark.asyncio
    async def test_reset_audio_stream_aborts_in_flight_bytes(self) -> None:
        """After ``reset_audio_stream``, the next chunk opens a fresh stream."""
        session, fake_h3, _in_q, out_q = _make_session()
        await session.start()
        try:
            chunk = AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K)
            await out_q.put(chunk)
            await asyncio.sleep(0.05)
            first_audio_sid = session._outbound_audio_stream_id  # noqa: SLF001
            assert first_audio_sid is not None

            session.reset_audio_stream()
            assert session._outbound_audio_stream_id is None  # noqa: SLF001
            quic = session._quic_protocol._quic  # noqa: SLF001
            assert (first_audio_sid, 0) in quic.resets

            # Next chunk must allocate a new stream id.
            await out_q.put(chunk)
            await asyncio.sleep(0.05)
            second_audio_sid = session._outbound_audio_stream_id  # noqa: SLF001
            assert second_audio_sid is not None
            assert second_audio_sid != first_audio_sid
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_outbound_writer_signals_close_on_unexpected_error(self) -> None:
        """A crash in the writer must set ``on_close`` so the owning transport
        tears down instead of silently wedging."""
        session, _fake_h3, _in_q, out_q = _make_session()
        await session.start()
        try:
            # Sabotage the raw stream send after the initial ``ready`` control
            # frame so the next outbound audio chunk explodes inside the writer.
            def _explode(*_args, **_kwargs):
                raise RuntimeError("simulated send_stream_data failure")

            session._quic_protocol._quic.send_stream_data = _explode  # type: ignore[assignment]  # noqa: SLF001
            await out_q.put(AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K))
            await asyncio.wait_for(session._on_close.wait(), timeout=1)  # noqa: SLF001
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_outbound_backpressure_pauses_until_send_buffer_drains(self) -> None:
        """The writer must stop draining ``_out_queue`` while aioquic's
        per-stream send buffer is over the high-water mark, then resume once
        it drains — otherwise a stalled client grows memory unbounded.
        """
        session, _h3, _in_q, _out_q = _make_session()
        sid = 1000
        session._outbound_audio_stream_id = sid  # noqa: SLF001
        buf = bytearray(_OUTBOUND_SEND_BUFFER_HIGH_WATER + 1)
        session._quic_protocol._quic._streams = {sid: _FakeStream(buf)}  # noqa: SLF001
        task = asyncio.create_task(session._await_outbound_capacity())  # noqa: SLF001
        await asyncio.sleep(0.15)
        assert not task.done()  # still backpressured
        buf.clear()  # client caught up
        await asyncio.wait_for(task, timeout=1)

    @pytest.mark.asyncio
    async def test_outbound_backpressure_returns_on_close(self) -> None:
        """A backpressured writer must still unwedge when the connection is
        lost, so the owning transport can tear down."""
        session, _h3, _in_q, _out_q = _make_session()
        sid = 1000
        session._outbound_audio_stream_id = sid  # noqa: SLF001
        buf = bytearray(_OUTBOUND_SEND_BUFFER_HIGH_WATER + 1)
        session._quic_protocol._quic._streams = {sid: _FakeStream(buf)}  # noqa: SLF001
        task = asyncio.create_task(session._await_outbound_capacity())  # noqa: SLF001
        await asyncio.sleep(0.1)
        assert not task.done()
        session._on_close.set()  # noqa: SLF001
        await asyncio.wait_for(task, timeout=1)

    @pytest.mark.asyncio
    async def test_outbound_capacity_no_audio_stream_returns_immediately(self) -> None:
        """No open audio stream → nothing buffered → no backpressure."""
        session, _h3, _in_q, _out_q = _make_session()
        assert session._outbound_audio_stream_id is None  # noqa: SLF001
        await asyncio.wait_for(session._await_outbound_capacity(), timeout=1)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_server_send_does_not_block_client_control_reception(self) -> None:
        """Regression: ``_send_control`` from ``start()`` must not poison the
        inbound control stream id.  Without distinct in/out tracking, the
        client's tag byte on its control stream is rejected as an "extra
        control stream" and its frames are silently dropped.
        """
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        await session.start()
        try:
            # The server has now allocated its *outbound* control stream.
            # The client opening its *own* control stream must still be
            # accepted (distinct in/out stream-id tracking).
            client_ctrl_sid = 16  # arbitrary, distinct from server-initiated 1000+
            msg = _ControlCodec.encode({"type": "start"})
            session.handle_stream_data(
                stream_id=client_ctrl_sid,
                data=bytes([_TAG_CONTROL]) + msg,
                ended=False,
            )
            assert session._inbound_control_stream_id == client_ctrl_sid  # noqa: SLF001
            # An inbound 48k audio chunk on its own client stream is resampled
            # down to 16k via the inline rate header.
            client_audio_sid = 20
            pcm_48k = b"\x00\x00" * 48
            session.handle_stream_data(
                stream_id=client_audio_sid,
                data=_audio_frame(pcm_48k, 48000),
                ended=False,
            )
            chunk = in_q.get_nowait()
            assert chunk.format.sample_rate == 16000
            assert len(chunk.data) == 32
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_poisoned_control_codec_tears_down_session(self) -> None:
        """An oversized control length prefix must close the session, not just
        silently disable control (the codec's documented contract)."""
        session, _h3, _in_q, _out_q = _make_session()
        oversized = struct.pack(">I", 1 << 30) + b"X"  # 1 GiB advertised
        session.handle_stream_data(
            stream_id=8, data=bytes([_TAG_CONTROL]) + oversized, ended=False
        )
        assert session._control_codec.poisoned is True  # noqa: SLF001
        assert session._on_close.is_set()  # noqa: SLF001
        assert session._quic_protocol.close_calls == [(0, "control framing violation")]  # noqa: SLF001

    def test_pending_tags_dict_is_capped(self) -> None:
        """A flood of untagged streams must not grow ``_pending_tags`` past the cap."""
        session, _h3, _in_q, _out_q = _make_session()
        # Open many empty streams without ever sending the tag byte.
        for sid in range(100):
            session.handle_stream_data(stream_id=sid, data=b"", ended=False)
        assert len(session._pending_tags) <= 4  # noqa: SLF001 — matches _MAX_PENDING_TAG_STREAMS

    def test_large_first_delivery_is_dispatched_not_dropped(self) -> None:
        """A batched first delivery of ``[tag] + multi-KiB PCM`` in a single
        event must be routed to the audio handler, not dropped.

        Regression for the Copilot review: an earlier per-stream byte cap
        discarded the tag along with the payload and left the stream
        permanently mis-routed.  WebTransport write-batching / back-pressure
        can easily produce a >4 KiB first delivery.
        """
        session, _h3, in_q, _out_q = _make_session(in_max=4)
        big_pcm = b"\x00\x01" * 4096  # 8 KiB of PCM (> the old 4 KiB cap)
        session.handle_stream_data(stream_id=7, data=_audio_frame(big_pcm), ended=False)
        chunk = in_q.get_nowait()
        assert chunk.data == big_pcm
        # Stream is now identified; a follow-up event routes without re-tagging.
        session.handle_stream_data(stream_id=7, data=b"\x02\x03", ended=False)
        assert in_q.get_nowait().data == b"\x02\x03"
        assert 7 not in session._pending_tags  # noqa: SLF001

    def test_nonempty_first_delivery_dispatched_even_when_cap_full(self) -> None:
        """The pending-tag cap must never refuse a *non-empty* first delivery
        (that would drop the tag byte and permanently mis-route the stream).
        Only zero-byte pending streams count against the cap.
        """
        session, _h3, in_q, _out_q = _make_session(in_max=8)
        # Saturate the cap with zero-byte streams.
        for sid in range(10):
            session.handle_stream_data(stream_id=sid, data=b"", ended=False)
        assert len(session._pending_tags) == 4  # noqa: SLF001

        # A brand-new stream that arrives *with* its tag+payload must still be
        # dispatched despite the cap being full.
        pcm = b"\x07\x07" * 4
        session.handle_stream_data(stream_id=999, data=_audio_frame(pcm), ended=False)
        assert in_q.get_nowait().data == pcm
        assert len(session._pending_tags) == 4  # noqa: SLF001 — unchanged

    def test_rejected_duplicate_audio_stream_stays_rejected(self) -> None:
        """Regression: a duplicate audio stream is rejected with its
        tag/header byte already consumed.  Later chunks on it must keep being
        ignored — not re-dispatched, where a PCM byte equal to 0x01 could be
        misread as a fresh audio header once the original stream has ended.
        """
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        session.handle_stream_data(stream_id=4, data=_audio_frame(b"\xaa\xbb"), ended=False)
        assert session._inbound_audio_stream_id == 4  # noqa: SLF001
        in_q.get_nowait()  # drain the legit chunk

        # A second audio stream opened while the first is active is rejected.
        # Its payload deliberately looks like a fresh audio tag+rate header so
        # the pre-fix code would later misroute it.
        poison = bytes([_TAG_AUDIO]) + struct.pack(">I", 16000) + b"\x01\x02"
        session.handle_stream_data(stream_id=8, data=poison, ended=False)
        assert 8 in session._rejected_stream_ids  # noqa: SLF001
        assert in_q.empty()

        # The original audio stream ends — no audio stream is now active.
        session.handle_stream_data(stream_id=4, data=b"", ended=True)
        assert session._inbound_audio_stream_id is None  # noqa: SLF001

        # A full audio frame on the rejected stream must NOT be accepted as a
        # fresh audio stream just because none is currently active (pre-fix it
        # would be: tag re-read, PCM enqueued, stream id re-bound).
        session.handle_stream_data(stream_id=8, data=_audio_frame(b"\x33\x44"), ended=False)
        assert in_q.empty()
        assert session._inbound_audio_stream_id is None  # noqa: SLF001

        # A FIN on the rejected stream clears its bookkeeping.
        session.handle_stream_data(stream_id=8, data=b"", ended=True)
        assert 8 not in session._rejected_stream_ids  # noqa: SLF001

    def test_rejected_stream_flood_tears_down_session(self) -> None:
        """A flood of rejected streams is a malicious-peer signal: past the
        cap the session is torn down (mirrors the poisoned-codec path) rather
        than silently dropping tracking and reopening the misroute.
        """
        session, _h3, _in_q, _out_q = _make_session()
        # One legit audio stream so every later audio stream is a duplicate.
        session.handle_stream_data(stream_id=2, data=_audio_frame(b"\x00\x00"), ended=False)
        for sid in range(_MAX_REJECTED_STREAMS + 1):
            session.handle_stream_data(
                stream_id=100 + sid, data=_audio_frame(b"\x00\x00"), ended=False
            )
        assert session._on_close.is_set()  # noqa: SLF001
        assert session._quic_protocol.close_calls == [  # noqa: SLF001
            (0, "too many rejected streams")
        ]

    @pytest.mark.asyncio
    async def test_control_stream_end_resets_codec(self) -> None:
        """Regression: a control stream that closes mid-frame must not leave
        stale length/payload bytes that corrupt — and here poison — the first
        frame of a re-opened control stream.
        """
        session, _h3, _in_q, _out_q = _make_session()
        # Open a control stream, feed an *incomplete* frame (4-byte length
        # announcing a 10-byte body, only 3 body bytes), then FIN it.
        partial = struct.pack(">I", 10) + b"abc"
        session.handle_stream_data(stream_id=12, data=bytes([_TAG_CONTROL]) + partial, ended=True)
        assert session._inbound_control_stream_id is None  # noqa: SLF001

        # A re-opened control stream sends a clean frame.  Without the codec
        # reset, the stale 7 bytes would shift framing and the trailing bytes
        # decode to an oversized length that poisons the codec and tears the
        # session down.
        msg = _ControlCodec.encode({"type": "start"})
        session.handle_stream_data(stream_id=16, data=bytes([_TAG_CONTROL]) + msg, ended=False)
        assert session._inbound_control_stream_id == 16  # noqa: SLF001
        assert session._control_codec.poisoned is False  # noqa: SLF001
        assert not session._on_close.is_set()  # noqa: SLF001


# ── Journal integration: TransportDegraded emission ───────────────


class TestWebTransportDegradedEvents:
    """Each drop/poison/abort path must surface a ``TransportDegraded`` so
    it lands in the journal (not just the debug log)."""

    @pytest.mark.asyncio
    async def test_inbound_queue_full_emits_degraded(self) -> None:
        rec = _DegradedRecorder()
        session, _h3, _in_q, _out_q = _make_session(in_max=1, emit_degraded=rec)
        pcm = b"\x00\x00" * 4
        # First frame parses the inline rate header and fills the queue.
        session.handle_stream_data(stream_id=4, data=_audio_frame(pcm), ended=False)
        # Second frame has nowhere to go and must be reported as dropped.
        session.handle_stream_data(stream_id=4, data=pcm, ended=False)
        assert rec.reasons == [_DEGRADED_INBOUND_QUEUE_FULL]
        assert rec.calls[0][2] is False  # recoverable, non-fatal
        # Routes through the shared ``_enqueue_inbound_chunk`` path with the
        # canonical message shape (context="WebTransport"), not the old
        # hand-rolled "mic frame" wording.
        assert rec.calls[0][1] == f"dropped {len(pcm)}-byte WebTransport frame; inbound queue full"

    def test_rejected_stream_flood_emits_fatal(self) -> None:
        rec = _DegradedRecorder()
        session, _h3, _in_q, _out_q = _make_session(emit_degraded=rec)
        session.handle_stream_data(stream_id=2, data=_audio_frame(b"\x00\x00"), ended=False)
        for sid in range(_MAX_REJECTED_STREAMS + 1):
            session.handle_stream_data(
                stream_id=100 + sid, data=_audio_frame(b"\x00\x00"), ended=False
            )
        assert _DEGRADED_REJECTED_STREAM_FLOOD in rec.reasons
        flood = next(c for c in rec.calls if c[0] == _DEGRADED_REJECTED_STREAM_FLOOD)
        assert flood[2] is True  # fatal teardown

    def test_control_codec_poisoned_emits_fatal(self) -> None:
        rec = _DegradedRecorder()
        session, _h3, _in_q, _out_q = _make_session(emit_degraded=rec)
        oversized = struct.pack(">I", _MAX_CONTROL_FRAME_BYTES + 1) + b"X"
        session.handle_stream_data(
            stream_id=8, data=bytes([_TAG_CONTROL]) + oversized, ended=False
        )
        assert rec.reasons == [_DEGRADED_CONTROL_CODEC_POISONED]
        assert rec.calls[0][2] is True

    @pytest.mark.asyncio
    async def test_barge_in_reset_failure_emits_degraded(self) -> None:
        rec = _DegradedRecorder()
        session, _h3, _in_q, _out_q = _make_session(emit_degraded=rec)
        session._outbound_audio_stream_id = 1000  # noqa: SLF001 — pretend a stream is open

        def _boom(stream_id: int, error_code: int) -> None:
            raise RuntimeError("reset boom")

        session._quic_protocol._quic.reset_stream = _boom  # noqa: SLF001
        session.reset_audio_stream()
        assert rec.reasons == [_DEGRADED_BARGE_IN_RESET_FAILED]
        assert rec.calls[0][2] is False  # client may still hear TTS, but not fatal

    @pytest.mark.asyncio
    async def test_outbound_writer_crash_emits_fatal(self) -> None:
        rec = _DegradedRecorder()
        session, _h3, _in_q, out_q = _make_session(emit_degraded=rec)
        await session.start()  # "ready" goes out on the unpatched fake first

        def _boom(stream_id: int, data: bytes, end_stream: bool = False) -> None:  # noqa: FBT001, FBT002
            raise RuntimeError("send boom")

        session._quic_protocol._quic.send_stream_data = _boom  # noqa: SLF001
        out_q.put_nowait(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
        await asyncio.wait_for(session._on_close.wait(), timeout=1)  # noqa: SLF001
        await session.stop()
        crash = next(c for c in rec.calls if c[0] == _DEGRADED_OUTBOUND_WRITER_CRASHED)
        assert crash[2] is True

    @pytest.mark.asyncio
    async def test_connection_transport_emits_on_event_bus(self) -> None:
        """End-to-end through the real seam: a dropped TTS frame is published
        on the session ``EventBus`` (scheduled, not awaited) where
        :class:`SessionJournalSink` would record it."""
        transport = WebTransportConnectionTransport(
            config=WebTransportTransportConfig(outbound_max_pending=1),
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )
        received: list[TransportDegraded] = []
        bus = EventBus()
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus  # noqa: SLF001 — mirrors Session._maybe_attach_event_bus
        transport._connected = True  # noqa: SLF001 — skip the draining writer
        chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
        assert await transport.send_audio(chunk) is True  # fills the 1-slot queue
        assert await transport.send_audio(chunk) is False  # dropped
        # Emission is fire-and-forget; let the scheduled bus.emit task run.
        for _ in range(3):
            await asyncio.sleep(0)
        assert [e.reason for e in received] == [_DEGRADED_OUTBOUND_QUEUE_FULL]
        assert received[0].provider == "webtransport"
        assert received[0].fatal is False


# ── Conformance: protocol shape and types ─────────────────────────


def _build_connection_transport() -> WebTransportConnectionTransport:
    return WebTransportConnectionTransport(
        _h3=_FakeH3(),  # type: ignore[arg-type]
        _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
        _session_id=0,
    )


class TestWebTransportConnectionTransport:
    def test_satisfies_transport_protocol(self) -> None:
        assert isinstance(_build_connection_transport(), Transport)

    def test_has_protocol_methods(self) -> None:
        t = _build_connection_transport()
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_not_connected(self) -> None:
        t = _build_connection_transport()
        result = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
        assert result is False

    @pytest.mark.asyncio
    async def test_clear_audio_drains_outbound_queue(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        try:
            for _ in range(5):
                ok = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
                assert ok
            await t.clear_audio()
            assert t._out_queue.qsize() == 0  # noqa: SLF001
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_queue_full(self) -> None:
        t = WebTransportConnectionTransport(
            config=WebTransportTransportConfig(outbound_max_pending=2),
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )
        await t.connect()
        try:
            await t._session.stop()  # noqa: SLF001
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert not await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_enqueues_sentinel(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        await t.disconnect()
        chunks = []
        async for c in t.receive_audio():
            chunks.append(c)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_wait_closed_resolves_on_disconnect(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        wait_task = asyncio.create_task(t.wait_closed(timeout=2))
        await t.disconnect()
        await wait_task

    @pytest.mark.asyncio
    async def test_connect_without_session_raises(self) -> None:
        t = WebTransportConnectionTransport()
        with pytest.raises(RuntimeError, match="no underlying session"):
            await t.connect()

    @pytest.mark.asyncio
    async def test_clear_audio_resets_in_flight_quic_stream(self) -> None:
        """``clear_audio`` must reset the QUIC audio stream, not just the app queue."""
        t = _build_connection_transport()
        await t.connect()
        try:
            # Send enough audio that the writer task allocates an audio stream.
            await t.send_audio(AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K))
            await asyncio.sleep(0.05)
            session = t._session  # noqa: SLF001
            assert session is not None
            audio_sid = session._outbound_audio_stream_id  # noqa: SLF001
            assert audio_sid is not None

            await t.clear_audio()
            quic = session._quic_protocol._quic  # noqa: SLF001
            assert (audio_sid, 0) in quic.resets
            assert session._outbound_audio_stream_id is None  # noqa: SLF001
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_after_disconnect(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        await t.disconnect()
        result = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_preserves_frames_fed_before_connect(self) -> None:
        """Regression: mic frames the aioquic protocol enqueues between
        session-accept and the task-scheduled ``connect()`` must survive.
        ``connect()`` must not reset the inbound queue.
        """
        t = _build_connection_transport()
        # Simulate the protocol feeding early audio before connect() runs:
        # client opens its audio stream (tag 0x01) and writes a frame.
        early = b"\x11\x22\x33\x44"
        t._feed_stream_data(  # noqa: SLF001
            stream_id=12, data=_audio_frame(early), ended=False
        )
        await t.connect()
        try:
            chunk = await asyncio.wait_for(t._in_queue.get(), timeout=1)  # noqa: SLF001
            assert chunk is not None
            assert chunk.data == early
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_force_close_terminates_quic_before_connect(self) -> None:
        """Regression: overflow rejection must actively close the QUIC
        connection.  ``disconnect()`` early-returns pre-``connect()`` so it
        cannot — ``force_close()`` must send CONNECTION_CLOSE regardless.
        """
        proto = _FakeQuicProtocol()
        t = WebTransportConnectionTransport(
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=proto,  # type: ignore[arg-type]
            _session_id=0,
        )
        # Never connected — disconnect() would be a no-op here.
        await t.disconnect()
        assert proto.close_calls == []

        t.force_close(reason="session cap reached")
        assert proto.close_calls == [(0, "session cap reached")]
        # Sentinel enqueued so any consumer iterating receive_audio() exits.
        chunks = []
        async for c in t.receive_audio():
            chunks.append(c)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_connection_lost_marks_disconnected_and_wakes_writer(self) -> None:
        """On QUIC loss the transport must mark itself disconnected (so
        ``send_audio`` stops accepting undeliverable TTS) and still deliver
        the writer sentinel even when ``_out_queue`` is full.
        """
        t = WebTransportConnectionTransport(
            config=WebTransportTransportConfig(outbound_max_pending=2),
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )
        await t.connect()
        try:
            # Stop the writer so it cannot drain, then fill the queue.
            await t._session.stop()  # noqa: SLF001
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert not await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))

            t._mark_connection_lost()  # noqa: SLF001

            assert t._connected is False  # noqa: SLF001
            assert not t._client_connected.is_set()  # noqa: SLF001
            assert t._on_close.is_set()  # noqa: SLF001
            # The writer sentinel must have been delivered despite the
            # full queue (one chunk dropped to make room).
            seen_sentinel = False
            while not t._out_queue.empty():  # noqa: SLF001
                if t._out_queue.get_nowait() is None:  # noqa: SLF001
                    seen_sentinel = True
            assert seen_sentinel
            # send_audio now refuses — the transport is marked disconnected.
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K)) is False
        finally:
            await t.disconnect()


# ── WebTransportTransport conformance ─────────────────────────────


class TestWebTransportTransportConformance:
    def test_has_protocol_methods(self) -> None:
        cfg = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        t = WebTransportTransport(cfg)
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    def test_satisfies_transport_protocol(self) -> None:
        cfg = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        assert isinstance(WebTransportTransport(cfg), Transport)

    @pytest.mark.asyncio
    async def test_connect_requires_cert_files(self) -> None:
        t = WebTransportTransport()
        with pytest.raises(ValueError, match="certfile and keyfile"):
            await t.connect()

    @pytest.mark.asyncio
    async def test_receive_audio_exits_after_inner_session_ends(self) -> None:
        """When the inner session terminates, the wrapper's ``receive_audio``
        iteration must stop.  ``WebTransportTransport`` delegates directly to
        the inner transport — when the inner stream ends, the outer iteration
        ends naturally.
        """
        outer_cfg = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        outer = WebTransportTransport(outer_cfg)

        inner = _build_connection_transport()
        await inner.connect()
        # Wire the outer's "active" slot manually since we're not running a
        # real server in this unit test.
        outer._active = inner  # noqa: SLF001
        outer._connected = True  # noqa: SLF001
        outer._client_connected.set()  # noqa: SLF001

        # Start iterating; should block waiting for chunks.
        recv_task = asyncio.create_task(self._collect_chunks(outer))
        await asyncio.sleep(0)
        # Inner disconnects → its receive_audio sentinel fires → outer's
        # ``async for`` exits.
        await inner.disconnect()
        chunks = await asyncio.wait_for(recv_task, timeout=1)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_receive_audio_exits_when_disconnect_precedes_client(self) -> None:
        """If ``disconnect()`` runs before any client arrives, iterating
        ``receive_audio()`` must still exit (not hang on ``_client_connected``).
        """
        outer = WebTransportTransport(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        )
        outer._connected = True  # noqa: SLF001
        recv_task = asyncio.create_task(self._collect_chunks(outer))
        await asyncio.sleep(0)
        await outer.disconnect()
        chunks = await asyncio.wait_for(recv_task, timeout=1)
        assert chunks == []

    @staticmethod
    async def _collect_chunks(outer: WebTransportTransport) -> list[AudioChunk]:
        out: list[AudioChunk] = []
        async for chunk in outer.receive_audio():
            out.append(chunk)
        return out


# ── WebTransportServer wiring (no network) ────────────────────────


class TestWebTransportServerWiring:
    @pytest.mark.asyncio
    async def test_start_requires_cert(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(WebTransportTransportConfig(), _noop)
        with pytest.raises(ValueError, match="certfile and keyfile"):
            await server.start()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_before_start(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"), _noop
        )
        await server.stop()

    @pytest.mark.asyncio
    async def test_stop_safe_when_called_from_within_handler(self) -> None:
        """A handler that triggers ``server.stop()`` mustn't deadlock by
        gathering its own task (regression for review #3/#8).
        """
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda transport: asyncio.sleep(0),  # type: ignore[arg-type]
        )
        server._started = True  # noqa: SLF001 — fake "started"

        # Run ``stop()`` from within a separate task so that
        # ``asyncio.current_task()`` inside ``stop()`` reliably matches
        # the handler-task registration on every Python version.
        # (3.11's ``asyncio.wait_for`` wraps the inner coro in a new
        # task, which would otherwise mask the regression we're guarding.)
        async def handler_calls_stop() -> None:
            handler_task = asyncio.current_task()
            assert handler_task is not None
            server._handler_tasks.add(handler_task)  # noqa: SLF001
            await server.stop()

        await asyncio.wait_for(asyncio.create_task(handler_calls_stop()), timeout=1)

    @pytest.mark.asyncio
    async def test_max_concurrent_sessions_force_closes_overflow(self) -> None:
        """The real dispatch path must accept up to the cap and **force-close**
        the over-cap connection (regression: ``disconnect()`` was a no-op
        pre-``connect()`` so the cap wasn't actually enforced).
        """
        cfg = WebTransportTransportConfig(
            certfile="cert.pem",
            keyfile="key.pem",
            max_concurrent_sessions=2,
        )
        handler_started = asyncio.Event()

        async def _handler(_t: WebTransportConnectionTransport) -> None:
            handler_started.set()
            await asyncio.sleep(10)  # hold the slot

        server = WebTransportServer(cfg, _handler)

        def _make_transport() -> tuple[WebTransportConnectionTransport, _FakeQuicProtocol]:
            proto = _FakeQuicProtocol()
            t = WebTransportConnectionTransport(
                _h3=_FakeH3(),  # type: ignore[arg-type]
                _quic_protocol=proto,  # type: ignore[arg-type]
                _session_id=0,
            )
            return t, proto

        accepted = [_make_transport() for _ in range(2)]
        for t, _proto in accepted:
            server._dispatch_session(t)  # noqa: SLF001 — exercise the real path
        await asyncio.sleep(0)
        assert len(server._handler_tasks) == 2  # noqa: SLF001

        # Third session is over the cap → force-closed, handler not invoked.
        overflow, overflow_proto = _make_transport()
        server._dispatch_session(overflow)  # noqa: SLF001
        assert overflow_proto.close_calls == [(0, "session cap reached")]
        assert len(server._handler_tasks) == 2  # noqa: SLF001 — unchanged

        for task in list(server._handler_tasks):  # noqa: SLF001
            task.cancel()
        await asyncio.gather(*server._handler_tasks, return_exceptions=True)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_can_accept_session_gate_reflects_cap(self) -> None:
        """``_can_accept_session`` is the pre-200 gate: the protocol consults
        it before sending the 200 so an over-cap CONNECT gets a clean 503
        instead of 200-then-CONNECTION_CLOSE.
        """
        cfg = WebTransportTransportConfig(
            certfile="cert.pem", keyfile="key.pem", max_concurrent_sessions=2
        )

        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(cfg, _noop)
        assert server._can_accept_session() is True  # noqa: SLF001

        held = [asyncio.create_task(asyncio.sleep(10)) for _ in range(2)]
        server._handler_tasks.update(held)  # noqa: SLF001
        try:
            # At the cap → the protocol would send 503 and create no transport.
            assert server._can_accept_session() is False  # noqa: SLF001
        finally:
            for task in held:
                task.cancel()
            await asyncio.gather(*held, return_exceptions=True)
            server._handler_tasks.difference_update(held)  # noqa: SLF001
        assert server._can_accept_session() is True  # noqa: SLF001 — slots freed


# ── Protocol session-id isolation ─────────────────────────────────


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_protocol_rejects_stream_data_for_other_session() -> None:
    """A QUIC connection accepts exactly one WebTransport session.  Stream
    data tagged with a *different* ``session_id`` (e.g. a stream opened
    against a CONNECT we rejected with 409) must never be fed into the one
    accepted session.
    """
    from aioquic.h3.events import WebTransportStreamDataReceived

    class _Recorder:
        def __init__(self) -> None:
            self.fed: list[tuple[int, bytes, bool]] = []

        def _feed_stream_data(self, stream_id: int, data: bytes, ended: bool) -> None:
            self.fed.append((stream_id, data, ended))

    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    proto._h3 = object()  # only asserted non-None  # noqa: SLF001
    rec = _Recorder()
    proto._wt_transport = rec  # type: ignore[assignment]  # noqa: SLF001
    proto._accepted_session_id = 5  # noqa: SLF001

    proto._handle_h3_event(  # noqa: SLF001
        WebTransportStreamDataReceived(data=b"hi", stream_id=8, stream_ended=False, session_id=5)
    )
    proto._handle_h3_event(  # noqa: SLF001
        WebTransportStreamDataReceived(data=b"x", stream_id=12, stream_ended=False, session_id=9)
    )
    # Only the matching-session frame was dispatched.
    assert rec.fed == [(8, b"hi", False)]


# ── Protocol QUIC/session termination ─────────────────────────────


class _LostRecorder:
    """Stand-in transport that records ``_mark_connection_lost`` calls."""

    def __init__(self) -> None:
        self.lost_calls = 0

    def _mark_connection_lost(self) -> None:
        self.lost_calls += 1


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_quic_connection_terminated_marks_session_lost() -> None:
    """A peer QUIC CONNECTION_CLOSE / idle timeout arrives as a
    ``ConnectionTerminated`` QUIC event (never as asyncio
    ``connection_lost()`` on the per-connection protocol).  It must still mark
    the transport disconnected so ``wait_closed()`` unblocks.
    """
    from aioquic.quic.events import ConnectionTerminated

    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    rec = _LostRecorder()
    proto._wt_transport = rec  # type: ignore[assignment]  # noqa: SLF001

    proto.quic_event_received(  # noqa: SLF001
        ConnectionTerminated(error_code=0, frame_type=None, reason_phrase="bye")
    )
    assert rec.lost_calls == 1


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_connect_stream_fin_marks_session_lost() -> None:
    """A browser ``transport.close()`` FINs the CONNECT stream; aioquic
    surfaces that as a ``DataReceived`` with ``stream_ended`` on the accepted
    session/CONNECT stream id.  That must tear the session down — a FIN on a
    *different* stream, or a non-final DATA frame, must not.
    """
    from aioquic.h3.events import DataReceived

    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    proto._h3 = object()  # only asserted non-None  # noqa: SLF001
    rec = _LostRecorder()
    proto._wt_transport = rec  # type: ignore[assignment]  # noqa: SLF001
    proto._accepted_session_id = 5  # noqa: SLF001

    # FIN on an unrelated stream id → not our session.
    proto._handle_h3_event(DataReceived(data=b"", stream_id=9, stream_ended=True))  # noqa: SLF001
    # Non-final data on the CONNECT stream → session still open.
    proto._handle_h3_event(  # noqa: SLF001
        DataReceived(data=b"x", stream_id=5, stream_ended=False)
    )
    assert rec.lost_calls == 0

    # Lone FIN on the accepted CONNECT/session stream → session closed.
    proto._handle_h3_event(DataReceived(data=b"", stream_id=5, stream_ended=True))  # noqa: SLF001
    assert rec.lost_calls == 1


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_termination_paths_are_noop_without_accepted_session() -> None:
    """Termination events for a connection that never had an accepted session
    (e.g. a CONNECT rejected with 503) must be safe no-ops."""
    from aioquic.h3.events import DataReceived
    from aioquic.quic.events import ConnectionTerminated

    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    proto._h3 = object()  # noqa: SLF001
    proto._wt_transport = None  # noqa: SLF001
    proto._accepted_session_id = None  # noqa: SLF001

    proto.quic_event_received(  # noqa: SLF001
        ConnectionTerminated(error_code=0, frame_type=None, reason_phrase="")
    )
    proto._handle_h3_event(DataReceived(data=b"", stream_id=7, stream_ended=True))  # noqa: SLF001
    # No transport to mark, no crash.


# ── Protocol CONNECT accept path ──────────────────────────────────


class _RecordingH3:
    """Records ``send_headers`` so accept/reject decisions can be asserted."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, list[tuple[bytes, bytes]], bool]] = []

    def send_headers(
        self, stream_id: int, headers: list[tuple[bytes, bytes]], end_stream: bool = False
    ) -> None:  # noqa: FBT001, FBT002
        self.sent.append((stream_id, headers, end_stream))


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_connect_with_end_stream_is_rejected_without_session() -> None:
    """A CONNECT whose HEADERS arrive with END_STREAM set is malformed for
    WebTransport (the CONNECT stream must stay open).  aioquic surfaces that
    only as ``HeadersReceived(stream_ended=True)`` — no later ``DataReceived``
    FIN ever fires — so accepting it would create a transport whose
    ``wait_closed()`` only unblocks at the QUIC idle timeout, pinning a
    session slot.  It must be rejected (400) with no transport created.
    """
    from aioquic.h3.events import HeadersReceived

    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    h3 = _RecordingH3()
    proto._h3 = h3  # type: ignore[assignment]  # noqa: SLF001
    proto._accept_path = "/easycat"  # noqa: SLF001
    proto._wt_transport = None  # noqa: SLF001
    proto._accepted_session_id = None  # noqa: SLF001
    on_session_calls: list[Any] = []
    proto._on_session = on_session_calls.append  # noqa: SLF001
    proto._can_accept = lambda: True  # noqa: SLF001
    proto.transmit = lambda: None  # type: ignore[method-assign]

    proto._handle_h3_event(  # noqa: SLF001
        HeadersReceived(
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"webtransport"),
                (b":path", b"/easycat"),
            ],
            stream_id=0,
            stream_ended=True,
        )
    )

    assert proto._wt_transport is None  # noqa: SLF001 — no session resources held
    assert on_session_calls == []  # handler never invoked
    assert len(h3.sent) == 1
    sid, hdrs, end = h3.sent[0]
    assert sid == 0
    assert dict(hdrs).get(b":status") == b"400"
    assert end is True


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_connect_without_end_stream_is_accepted() -> None:
    """Sanity counterpart: a well-formed CONNECT (HEADERS without END_STREAM)
    is accepted with a 200 and creates the per-session transport.
    """
    from aioquic.h3.events import HeadersReceived

    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    h3 = _RecordingH3()
    proto._h3 = h3  # type: ignore[assignment]  # noqa: SLF001
    proto._accept_path = "/easycat"  # noqa: SLF001
    proto._wt_transport = None  # noqa: SLF001
    proto._accepted_session_id = None  # noqa: SLF001
    on_session_calls: list[Any] = []
    proto._on_session = on_session_calls.append  # noqa: SLF001
    proto._can_accept = lambda: True  # noqa: SLF001
    proto._session_config = WebTransportTransportConfig()  # noqa: SLF001
    proto.transmit = lambda: None  # type: ignore[method-assign]

    proto._handle_h3_event(  # noqa: SLF001
        HeadersReceived(
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"webtransport"),
                (b":path", b"/easycat"),
            ],
            stream_id=0,
            stream_ended=False,
        )
    )

    assert proto._wt_transport is not None  # noqa: SLF001
    assert len(on_session_calls) == 1
    sid, hdrs, end = h3.sent[0]
    assert dict(hdrs).get(b":status") == b"200"
    assert end is False


# ── Top-level lazy exports ────────────────────────────────────────


def test_top_level_lazy_exports() -> None:
    import easycat

    assert hasattr(easycat, "WebTransportTransportConfig")
    assert hasattr(easycat, "WebTransportConnectionTransport")
    assert hasattr(easycat, "WebTransportServer")
    from easycat.transports import WebTransportTransport as _Wt  # noqa: F401


# ── Integration: loopback aioquic CONNECT handshake ───────────────


def _write_self_signed_pair(tmp: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")],
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _udp_loopback_available() -> bool:
    """Both AF_INET and AF_INET6 UDP sockets are needed.

    aioquic's :func:`aioquic.asyncio.client.connect` unconditionally opens an
    IPv6 socket (relying on dual-stack to reach IPv4 hosts), so the
    integration test is skipped in environments that lack IPv6 entirely
    (e.g. some container sandboxes).
    """
    import socket

    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            s = socket.socket(family, socket.SOCK_DGRAM)
        except OSError:
            return False
        s.close()
    return True


@contextlib.asynccontextmanager
async def _wt_client(port: int, cert_path: Path):
    """Connect a WebTransport client whose protocol owns its own H3 layer.

    The stock ``QuicConnectionProtocol`` + a post-hoc ``quic_event_received``
    monkeypatch lets aioquic's default stream handling build (and then GC)
    asyncio ``StreamWriter`` objects for the server's QPACK/control
    unidirectional streams, which raise "Cannot send data on peer-initiated
    unidirectional stream" from ``StreamWriter.__del__``.  A protocol that
    owns H3 from construction and never chains to the base handler avoids that
    entirely (this mirrors the server's ``_EasyCatH3Protocol``).

    Yields the connected protocol; use ``client.h3`` and ``client.events``.
    """
    from aioquic.asyncio.client import connect as quic_connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3Connection
    from aioquic.quic.configuration import QuicConfiguration

    cfg = QuicConfiguration(
        alpn_protocols=["h3"],
        is_client=True,
        max_datagram_frame_size=65536,
    )
    cfg.load_verify_locations(str(cert_path))
    # The self-signed cert's SAN is ``localhost``; we dial the 127.0.0.1
    # bind, so pin the TLS server name to what the cert actually attests.
    cfg.server_name = "localhost"

    class _ClientProtocol(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.h3 = H3Connection(self._quic, enable_webtransport=True)
            self.events: asyncio.Queue = asyncio.Queue()

        def quic_event_received(self, event: Any) -> None:
            for h3_event in self.h3.handle_event(event):
                self.events.put_nowait(h3_event)

    async with quic_connect(
        "127.0.0.1",
        port,
        configuration=cfg,
        create_protocol=_ClientProtocol,
    ) as client:
        yield client


@pytest.mark.integration_socket
@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
@pytest.mark.skipif(not _udp_loopback_available(), reason="UDP loopback unavailable")
class TestWebTransportServerLoopback:
    """Drive real aioquic CONNECT-webtransport handshakes against the server.

    Exercises multi-client semantics: spawn two concurrent clients, verify
    each is handed its own ``WebTransportConnectionTransport`` and can round-
    trip PCM independently.
    """

    @staticmethod
    async def _run_one_client(
        port: int,
        cert_path: Path,
        pcm_in: bytes,
        result_audio: asyncio.Future[bytes],
    ) -> None:
        """Open one WebTransport session and send/recv one PCM frame."""
        from aioquic.h3.events import HeadersReceived as ClientHeadersReceived
        from aioquic.h3.events import WebTransportStreamDataReceived as ClientStreamData

        async with _wt_client(port, cert_path) as client:
            client_h3 = client.h3
            events_q = client.events

            connect_stream_id = client._quic.get_next_available_stream_id()
            client_h3.send_headers(
                connect_stream_id,
                [
                    (b":method", b"CONNECT"),
                    (b":scheme", b"https"),
                    (b":authority", b"localhost"),
                    (b":path", b"/easycat"),
                    (b":protocol", b"webtransport"),
                    (b"sec-webtransport-http3-draft02", b"1"),
                ],
                end_stream=False,
            )
            client.transmit()

            async def _await_status_ok() -> None:
                while True:
                    ev = await events_q.get()
                    if isinstance(ev, ClientHeadersReceived):
                        status = dict(ev.headers).get(b":status")
                        assert status == b"200", f"unexpected status: {status!r}"
                        return

            await asyncio.wait_for(_await_status_ok(), timeout=5)

            audio_sid = client_h3.create_webtransport_stream(connect_stream_id)
            # WebTransport stream payload is raw QUIC stream data, not an H3
            # DATA frame — mirror what the server/browser do.  Client→server
            # audio is self-describing: [tag][4-byte BE rate][PCM].
            client._quic.send_stream_data(
                audio_sid,
                bytes([_TAG_AUDIO]) + struct.pack(">I", 16000) + pcm_in,
                end_stream=False,
            )
            client.transmit()

            # Server→client audio is [0x01][4-byte BE rate][PCM]; the header
            # may be split across stream-data events, so accumulate per
            # stream id and strip the 5-byte header once enough has arrived.
            audio_sid: int | None = None
            audio_buf = bytearray()
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                try:
                    ev = await asyncio.wait_for(events_q.get(), timeout=1)
                except TimeoutError:
                    continue
                if isinstance(ev, ClientStreamData):
                    if audio_sid is None and ev.data and ev.data[0] == _TAG_AUDIO:
                        audio_sid = ev.stream_id
                        audio_buf.extend(ev.data)
                    elif ev.stream_id == audio_sid:
                        audio_buf.extend(ev.data)
                    if len(audio_buf) >= 5 + len(pcm_in):
                        break
            result_audio.set_result(bytes(audio_buf[5 : 5 + len(pcm_in)]))

    @pytest.mark.asyncio
    async def test_two_concurrent_clients(self, tmp_path: Path) -> None:
        cert_path, key_path = _write_self_signed_pair(tmp_path)
        port = find_free_port()

        # Track handlers and their per-client transports.
        client_pcms: list[bytes] = []
        handler_started: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]

        async def handle(transport: WebTransportConnectionTransport) -> None:
            idx = len(client_pcms)
            client_pcms.append(b"")
            handler_started[idx].set()
            try:
                # Echo the first inbound frame back as TTS.
                async for chunk in transport.receive_audio():
                    client_pcms[idx] = chunk.data
                    await transport.send_audio(chunk)
                    break
                await transport.wait_closed()
            finally:
                pass

        server = WebTransportServer(
            WebTransportTransportConfig(
                host="127.0.0.1",
                port=port,
                certfile=str(cert_path),
                keyfile=str(key_path),
            ),
            handle,
        )
        await server.start()
        try:
            f1: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
            f2: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
            pcm_a = b"\x10\x00" * 8
            pcm_b = b"\x20\x00" * 8
            await asyncio.gather(
                self._run_one_client(port, cert_path, pcm_a, f1),
                self._run_one_client(port, cert_path, pcm_b, f2),
            )
            # Each client should have received its own echo back.
            echoed = sorted([f1.result(), f2.result()])
            sent = sorted([pcm_a, pcm_b])
            assert echoed == sent
            assert sorted(client_pcms) == sent
        finally:
            await server.stop()
