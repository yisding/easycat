"""WebTransport connection transport and protocol conformance tests."""

from __future__ import annotations

import asyncio

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.providers import Transport
from easycat.transports.webtransport import (
    WebTransportConnectionTransport,
    WebTransportTransport,
    WebTransportTransportConfig,
)

from ._webtransport_helpers import (
    _audio_frame,
    _build_connection_transport,
    _FakeH3,
    _FakeQuicProtocol,
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
