"""WebTransport connection transport and protocol conformance tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import easycat.transports.webtransport as webtransport_module
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.providers import Transport
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.transports.webtransport import (
    _MAX_STREAM_DATA,
    _OUTBOUND_SEND_BUFFER_HIGH_WATER,
    WebTransportConnectionTransport,
    WebTransportTransport,
    WebTransportTransportConfig,
)

from ._webtransport_helpers import (
    _audio_frame,
    _build_connection_transport,
    _FakeH3,
    _FakeQuicProtocol,
    _FakeStream,
)


class TestWebTransportConnectionTransport:
    @pytest.mark.parametrize("value", [0, -1, True, 1.5])
    def test_outbound_queue_bound_must_be_a_positive_integer(self, value: object) -> None:
        with pytest.raises(ValueError, match="outbound_max_pending"):
            WebTransportTransportConfig(outbound_max_pending=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [0, -1, True, 1.5])
    def test_session_cap_must_be_a_positive_integer(self, value: object) -> None:
        with pytest.raises(ValueError, match="max_concurrent_sessions"):
            WebTransportTransportConfig(max_concurrent_sessions=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [True, -0.1, float("nan"), float("inf"), 10**1000])
    def test_force_shutdown_timeout_must_be_nonnegative_and_finite(self, value: object) -> None:
        with pytest.raises(ValueError, match="force_shutdown_timeout_s"):
            WebTransportTransportConfig(force_shutdown_timeout_s=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "audio_format",
        [
            AudioFormat(sample_rate=16_000, channels=1, sample_width=1),
            AudioFormat(sample_rate=16_000, channels=2, sample_width=2),
            AudioFormat(sample_rate=16_000, channels=1, sample_width=2, encoding="mulaw"),
        ],
    )
    def test_wire_audio_format_must_be_mono_pcm16(self, audio_format: AudioFormat) -> None:
        with pytest.raises(ValueError, match="audio_format must be mono PCM16"):
            WebTransportTransportConfig(audio_format=audio_format)

    def test_default_wire_audio_format_remains_valid(self) -> None:
        assert WebTransportTransportConfig().audio_format == PCM16_MONO_16K

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
    async def test_connect_failure_rolls_back_before_retry_is_allowed(self) -> None:
        t = _build_connection_transport()
        session = t._session
        assert session is not None
        session.start = AsyncMock(  # type: ignore[method-assign]
            side_effect=[RuntimeError("writer startup failed"), None]
        )
        session.stop = AsyncMock()  # type: ignore[method-assign]
        session.close_connection = Mock()  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="writer startup failed"):
            await t.connect()

        assert t._connected is False
        assert t._session_stop_pending is False
        assert t._connection_close_pending is False
        session.stop.assert_awaited_once()
        session.close_connection.assert_called_once_with(reason="session ended")

        await t.connect()
        assert session.start.await_count == 2
        assert t._connected is True
        await t.disconnect()

    @pytest.mark.asyncio
    async def test_connect_preserves_startup_error_when_owned_rollback_fails(self) -> None:
        t = _build_connection_transport()
        session = t._session
        assert session is not None
        session.start = AsyncMock(side_effect=OSError("writer startup failed"))  # type: ignore[method-assign]
        session.stop = AsyncMock(  # type: ignore[method-assign]
            side_effect=[RuntimeError("rollback stop failed"), None]
        )
        session.close_connection = Mock()  # type: ignore[method-assign]

        with pytest.raises(OSError, match="writer startup failed") as exc_info:
            await t.connect()

        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "rollback stop failed"
        assert t._connected is False
        assert t._session_stop_pending is True
        assert t._connection_close_pending is False
        assert isinstance(t._disconnect_cleanup_error, RuntimeError)
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await t.connect()

        await t.disconnect()

        assert session.stop.await_count == 2
        assert t._session_stop_pending is False
        assert t._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_connect_rollback_preserves_new_caller_cancellation(self) -> None:
        t = _build_connection_transport()
        session = t._session
        assert session is not None
        startup_error = OSError("writer startup failed")
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def block_stop() -> None:
            cleanup_entered.set()
            await release_cleanup.wait()

        session.start = AsyncMock(side_effect=startup_error)  # type: ignore[method-assign]
        session.stop = AsyncMock(side_effect=block_stop)  # type: ignore[method-assign]
        session.close_connection = Mock()  # type: ignore[method-assign]

        connecting = asyncio.create_task(t.connect())
        await cleanup_entered.wait()
        connecting.cancel()
        release_cleanup.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await connecting

        assert exc_info.value.__cause__ is startup_error
        assert t._session_stop_pending is False
        assert t._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_disconnect_preserves_caller_cancellation_while_reaping_writer(self) -> None:
        t = _build_connection_transport()
        session = t._session
        assert session is not None
        child_cancelled = asyncio.Event()
        release_child = asyncio.Event()

        async def cancellation_resistant_writer() -> None:
            while not release_child.is_set():
                try:
                    await release_child.wait()
                except asyncio.CancelledError:
                    child_cancelled.set()

        async def start_resistant_writer() -> None:
            session._writer_task = asyncio.create_task(cancellation_resistant_writer())

        session.start = start_resistant_writer  # type: ignore[method-assign]
        await t.connect()

        disconnecting = asyncio.create_task(t.disconnect())
        await child_cancelled.wait()
        disconnecting.cancel()
        release_child.set()

        with pytest.raises(asyncio.CancelledError):
            await disconnecting

        assert isinstance(t._disconnect_cleanup_error, RuntimeError)
        assert t._session_stop_pending is True
        assert t._connection_close_pending is True
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await t.connect()

        await t.disconnect()

        assert t._session_stop_pending is False
        assert t._connection_close_pending is False
        assert t._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_writer_uses_attached_transport_scope(self) -> None:
        transport = _build_connection_transport()
        root = RuntimeScope.create_root(
            name="session",
            root_id="test-root:webtransport-writer",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        transport.set_runtime_scope(root, name="transport-runtime")

        await transport.connect()
        session = transport._session
        assert session is not None

        assert root.tasks("webtransport_writer") == (session._writer_task,)
        assert "transport-write" in root.cohorts(force=False)

        await transport.disconnect()

        assert not root.tasks("webtransport_writer")

    @pytest.mark.asyncio
    async def test_standalone_writer_releases_local_runtime_root(self) -> None:
        transport = _build_connection_transport()
        session = transport._session
        assert session is not None

        await transport.connect()

        assert session._writer_tasks.owns_root is True

        await transport.disconnect()

        assert session._writer_tasks.scope is None
        assert session._writer_tasks.owns_root is False

    @pytest.mark.asyncio
    async def test_clear_audio_drains_outbound_queue(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        try:
            for _ in range(5):
                ok = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
                assert ok
            await t.clear_audio()
            assert t._out_queue.qsize() == 0
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
            await t._session.stop()
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert not await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_large_audio_chunk_cannot_bypass_quic_send_buffer_high_water(self) -> None:
        """One giant TTS chunk must be fragmented before reaching aioquic.

        Model a stalled peer by retaining every write in the fake stream's
        private buffer.  Before fragmentation, a single 512 KiB input jumped
        the nominal 256 KiB high-water mark in one ``send_stream_data`` call.
        """
        t = _build_connection_transport()
        session = t._session
        assert session is not None
        quic = session._quic_protocol._quic
        original_send = quic.send_stream_data
        buffers: dict[int, bytearray] = {}
        quic._streams = {}

        def buffered_send(stream_id: int, data: bytes, end_stream: bool = False) -> None:
            original_send(stream_id, data, end_stream=end_stream)
            buffer = buffers.setdefault(stream_id, bytearray())
            buffer.extend(data)
            quic._streams[stream_id] = _FakeStream(buffer)

        quic.send_stream_data = buffered_send  # type: ignore[method-assign]
        await t.connect()
        try:
            source = b"\x00\x01" * _OUTBOUND_SEND_BUFFER_HIGH_WATER
            assert await t.send_audio(AudioChunk(data=source, format=PCM16_MONO_16K))
            await asyncio.sleep(0.1)

            audio_sid = session._outbound_audio_stream_id
            assert audio_sid is not None
            assert len(buffers[audio_sid]) <= _OUTBOUND_SEND_BUFFER_HIGH_WATER
            audio_writes = [data for sid, data in quic.sent if sid == audio_sid]
            # First write carries the WebTransport audio tag + sample rate.
            assert all(len(data) <= _MAX_STREAM_DATA for data in audio_writes[1:])
            assert not t._out_queue.empty()
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
    async def test_disconnect_cleanup_failures_are_best_effort_and_retryable(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        session = t._session
        assert session is not None
        original_stop = session.stop
        original_close = session.close_connection
        stop_calls = 0
        close_calls = 0

        async def fail_stop_once() -> None:
            nonlocal stop_calls
            stop_calls += 1
            if stop_calls == 1:
                raise RuntimeError("session stop failed")
            await original_stop()

        def fail_close_once(*, reason: str = "") -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise RuntimeError("connection close failed")
            original_close(reason=reason)

        session.stop = AsyncMock(side_effect=fail_stop_once)  # type: ignore[method-assign]
        session.close_connection = Mock(side_effect=fail_close_once)  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="session stop failed"):
            await t.disconnect()

        # Preserve the first failure while attempting the later connection
        # close and publishing terminal signals for every waiter.
        assert stop_calls == 1
        assert close_calls == 1
        assert t._connected is False
        assert t._on_close.is_set()
        assert t._session_stop_pending is True
        assert t._connection_close_pending is True

        await t.disconnect()

        assert stop_calls == 2
        assert close_calls == 2
        assert t._session_stop_pending is False
        assert t._connection_close_pending is False
        assert t._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_disconnect_retries_real_protocol_close_failure(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        session = t._session
        assert session is not None
        protocol = session._quic_protocol
        protocol.close = Mock(  # type: ignore[method-assign]
            side_effect=[RuntimeError("QUIC close failed"), None]
        )

        with pytest.raises(RuntimeError, match="QUIC close failed"):
            await t.disconnect()

        assert protocol.close.call_count == 1
        assert t._connection_close_pending is True
        assert isinstance(t._disconnect_cleanup_error, RuntimeError)

        await t.disconnect()

        assert protocol.close.call_count == 2
        assert t._connection_close_pending is False
        assert t._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_disconnect_retries_diagnostic_only_cleanup_failure(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        drain = AsyncMock(
            side_effect=[
                RuntimeError("diagnostic drain failed"),
                None,
                None,
            ]
        )
        t._drain_emit_tasks = drain  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="diagnostic drain failed"):
            await t.disconnect()

        assert t._session_stop_pending is False
        assert t._connection_close_pending is False
        assert isinstance(t._disconnect_cleanup_error, RuntimeError)
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await t.connect()

        await t.disconnect()

        assert drain.await_count == 2
        assert t._disconnect_cleanup_error is None

        await t.connect()
        await t.disconnect()

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
            session = t._session
            assert session is not None
            audio_sid = session._outbound_audio_stream_id
            assert audio_sid is not None

            await t.clear_audio()
            quic = session._quic_protocol._quic
            assert (audio_sid, 0) in quic.resets
            assert session._outbound_audio_stream_id is None
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
        t._feed_stream_data(stream_id=12, data=_audio_frame(early), ended=False)
        await t.connect()
        try:
            chunk = await asyncio.wait_for(t._in_queue.get(), timeout=1)
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
    async def test_force_close_wakes_consumers_when_quic_close_raises(self) -> None:
        t = _build_connection_transport()
        session = t._session
        assert session is not None
        session.close_connection = Mock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="close failed"):
            t.force_close(reason="shutdown")

        assert t._on_close.is_set()
        assert await t._in_queue.get() is None
        assert await t._out_queue.get() is None

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
            await t._session.stop()
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert not await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))

            t._mark_connection_lost()

            assert t._connected is False
            assert not t._client_connected.is_set()
            assert t._on_close.is_set()
            # The writer sentinel must have been delivered despite the
            # full queue (one chunk dropped to make room).
            seen_sentinel = False
            while not t._out_queue.empty():
                if t._out_queue.get_nowait() is None:
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
    async def test_disconnect_retries_failed_internal_server_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cleanup_error = RuntimeError("server cleanup failed")

        class _FailOnceServer:
            def __init__(self, *_: object, **__: object) -> None:
                self.stop_calls = 0
                self._cleanup_error: BaseException | None = None
                self._server: object | None = None
                self._started = False

            async def start(self) -> None:
                self._server = object()
                self._started = True

            async def stop(self) -> None:
                self.stop_calls += 1
                self._started = False
                if self.stop_calls == 1:
                    self._cleanup_error = cleanup_error
                    raise cleanup_error
                self._cleanup_error = None
                self._server = None

        monkeypatch.setattr(webtransport_module, "WebTransportServer", _FailOnceServer)
        transport = WebTransportTransport(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        )
        await transport.connect()
        server = transport._server
        assert server is not None

        with pytest.raises(RuntimeError, match="server cleanup failed"):
            await transport.disconnect()

        assert transport._connected is False
        assert transport._server is server

        await transport.disconnect()

        assert server.stop_calls == 2
        assert transport._server is None

    @pytest.mark.asyncio
    async def test_concurrent_connects_publish_exactly_one_internal_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        start_entered = asyncio.Event()
        release_start = asyncio.Event()

        class _BlockingServer:
            instances: list[_BlockingServer] = []  # noqa: RUF012 test fake uses shared class fixture

            def __init__(self, *_: object, **__: object) -> None:
                self._cleanup_error = None
                self._server: object | None = None
                self._started = False
                self.stop_calls = 0
                self.instances.append(self)

            async def start(self) -> None:
                start_entered.set()
                await release_start.wait()
                self._server = object()
                self._started = True

            async def stop(self) -> None:
                self.stop_calls += 1
                self._started = False
                self._server = None

        monkeypatch.setattr(webtransport_module, "WebTransportServer", _BlockingServer)
        transport = WebTransportTransport(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        )

        first = asyncio.create_task(transport.connect())
        await start_entered.wait()
        second = asyncio.create_task(transport.connect())
        await asyncio.sleep(0)
        assert len(_BlockingServer.instances) == 1

        release_start.set()
        await asyncio.gather(first, second)

        assert len(_BlockingServer.instances) == 1
        assert transport._server is _BlockingServer.instances[0]
        assert transport._connected is True
        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_waits_for_connect_and_cleans_the_same_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        start_entered = asyncio.Event()
        release_start = asyncio.Event()

        class _BlockingServer:
            def __init__(self, *_: object, **__: object) -> None:
                self._cleanup_error = None
                self._server: object | None = None
                self._started = False
                self.stop_calls = 0

            async def start(self) -> None:
                start_entered.set()
                await release_start.wait()
                self._server = object()
                self._started = True

            async def stop(self) -> None:
                self.stop_calls += 1
                self._started = False
                self._server = None

        monkeypatch.setattr(webtransport_module, "WebTransportServer", _BlockingServer)
        transport = WebTransportTransport(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        )

        connecting = asyncio.create_task(transport.connect())
        await start_entered.wait()
        server = transport._server
        assert server is not None
        disconnecting = asyncio.create_task(transport.disconnect())
        await asyncio.sleep(0)
        assert not disconnecting.done()

        release_start.set()
        await asyncio.gather(connecting, disconnecting)

        assert server.stop_calls == 1
        assert server._started is False
        assert transport._connected is False
        assert transport._server is None

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
        outer._active = inner
        outer._connected = True
        outer._client_connected.set()

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
        outer._connected = True
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
