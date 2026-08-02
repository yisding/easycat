"""WebTransport per-session stream dispatch and framing tests."""

from __future__ import annotations

import asyncio
import json
import struct
from unittest.mock import Mock

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.transports.webtransport import (
    _MAX_REJECTED_STREAMS,
    _OUTBOUND_SEND_BUFFER_HIGH_WATER,
    _TAG_AUDIO,
    _TAG_CONTROL,
    _ControlCodec,
)

from ._webtransport_helpers import _audio_frame, _FakeStream, _make_session


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
        session.handle_stream_data(stream_id=4, data=_audio_frame(pcm_48k, 48000), ended=True)
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
        session.handle_stream_data(stream_id=4, data=frame[3:], ended=True)  # rest
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
                ended=True,
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

    def test_poisoned_control_codec_marks_closed_when_quic_close_raises(self) -> None:
        session, _h3, _in_q, _out_q = _make_session()
        session.close_connection = Mock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]
        oversized = struct.pack(">I", 1 << 30) + b"X"

        with pytest.raises(RuntimeError, match="close failed"):
            session.handle_stream_data(
                stream_id=8, data=bytes([_TAG_CONTROL]) + oversized, ended=False
            )

        assert session._on_close.is_set()  # noqa: SLF001

    def test_rejected_stream_flood_marks_closed_when_quic_close_raises(self) -> None:
        session, _h3, _in_q, _out_q = _make_session()
        session.close_connection = Mock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="close failed"):
            for stream_id in range(_MAX_REJECTED_STREAMS + 1):
                session._reject_stream(stream_id)  # noqa: SLF001

        assert session._on_close.is_set()  # noqa: SLF001

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
