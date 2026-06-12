"""WebTransport TransportDegraded emission tests."""

from __future__ import annotations

import asyncio
import struct

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import EventBus, TransportDegraded
from easycat.transports.webtransport import (
    _DEGRADED_BARGE_IN_RESET_FAILED,
    _DEGRADED_CONTROL_CODEC_POISONED,
    _DEGRADED_INBOUND_QUEUE_FULL,
    _DEGRADED_OUTBOUND_QUEUE_FULL,
    _DEGRADED_OUTBOUND_WRITER_CRASHED,
    _DEGRADED_REJECTED_STREAM_FLOOD,
    _MAX_CONTROL_FRAME_BYTES,
    _MAX_REJECTED_STREAMS,
    _TAG_CONTROL,
    WebTransportConnectionTransport,
    WebTransportTransportConfig,
)

from ._webtransport_helpers import (
    _audio_frame,
    _DegradedRecorder,
    _FakeH3,
    _FakeQuicProtocol,
    _make_session,
)


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
