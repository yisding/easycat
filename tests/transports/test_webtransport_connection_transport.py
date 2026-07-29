"""WebTransport connection transport and protocol conformance tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import easycat.transports.webtransport as webtransport_module
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
    @pytest.mark.parametrize("value", [0, -1, True, 1.5])
    def test_outbound_queue_bound_must_be_a_positive_integer(self, value: object) -> None:
        with pytest.raises(ValueError, match="outbound_max_pending"):
            WebTransportTransportConfig(outbound_max_pending=value)  # type: ignore[arg-type]

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
        session = t._session  # noqa: SLF001
        assert session is not None
        session.start = AsyncMock(  # type: ignore[method-assign]
            side_effect=[RuntimeError("writer startup failed"), None]
        )
        session.stop = AsyncMock()  # type: ignore[method-assign]
        session.close_connection = Mock()  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="writer startup failed"):
            await t.connect()

        assert t._connected is False  # noqa: SLF001
        assert t._session_stop_pending is False  # noqa: SLF001
        assert t._connection_close_pending is False  # noqa: SLF001
        session.stop.assert_awaited_once()
        session.close_connection.assert_called_once_with(reason="session ended")

        await t.connect()
        assert session.start.await_count == 2
        assert t._connected is True  # noqa: SLF001
        await t.disconnect()

    @pytest.mark.asyncio
    async def test_connect_preserves_startup_error_when_owned_rollback_fails(self) -> None:
        t = _build_connection_transport()
        session = t._session  # noqa: SLF001
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
        assert t._connected is False  # noqa: SLF001
        assert t._session_stop_pending is True  # noqa: SLF001
        assert t._connection_close_pending is False  # noqa: SLF001
        assert isinstance(t._disconnect_cleanup_error, RuntimeError)  # noqa: SLF001
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await t.connect()

        await t.disconnect()

        assert session.stop.await_count == 2
        assert t._session_stop_pending is False  # noqa: SLF001
        assert t._disconnect_cleanup_error is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_disconnect_preserves_caller_cancellation_while_reaping_writer(self) -> None:
        t = _build_connection_transport()
        session = t._session  # noqa: SLF001
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

        assert isinstance(t._disconnect_cleanup_error, RuntimeError)  # noqa: SLF001
        assert t._session_stop_pending is True  # noqa: SLF001
        assert t._connection_close_pending is True  # noqa: SLF001
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await t.connect()

        await t.disconnect()

        assert t._session_stop_pending is False  # noqa: SLF001
        assert t._connection_close_pending is False  # noqa: SLF001
        assert t._disconnect_cleanup_error is None  # noqa: SLF001

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
    async def test_disconnect_cleanup_failures_are_best_effort_and_retryable(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        session = t._session  # noqa: SLF001
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
        assert t._connected is False  # noqa: SLF001
        assert t._on_close.is_set()  # noqa: SLF001
        assert t._session_stop_pending is True  # noqa: SLF001
        assert t._connection_close_pending is True  # noqa: SLF001

        await t.disconnect()

        assert stop_calls == 2
        assert close_calls == 2
        assert t._session_stop_pending is False  # noqa: SLF001
        assert t._connection_close_pending is False  # noqa: SLF001
        assert t._disconnect_cleanup_error is None  # noqa: SLF001

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

        assert t._session_stop_pending is False  # noqa: SLF001
        assert t._connection_close_pending is False  # noqa: SLF001
        assert isinstance(t._disconnect_cleanup_error, RuntimeError)  # noqa: SLF001
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await t.connect()

        await t.disconnect()

        assert drain.await_count == 2
        assert t._disconnect_cleanup_error is None  # noqa: SLF001

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
        server = transport._server  # noqa: SLF001
        assert server is not None

        with pytest.raises(RuntimeError, match="server cleanup failed"):
            await transport.disconnect()

        assert transport._connected is False  # noqa: SLF001
        assert transport._server is server  # noqa: SLF001

        await transport.disconnect()

        assert server.stop_calls == 2
        assert transport._server is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_concurrent_connects_publish_exactly_one_internal_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        start_entered = asyncio.Event()
        release_start = asyncio.Event()

        class _BlockingServer:
            instances: list[_BlockingServer] = []

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
        assert transport._server is _BlockingServer.instances[0]  # noqa: SLF001
        assert transport._connected is True  # noqa: SLF001
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
        server = transport._server  # noqa: SLF001
        assert server is not None
        disconnecting = asyncio.create_task(transport.disconnect())
        await asyncio.sleep(0)
        assert not disconnecting.done()

        release_start.set()
        await asyncio.gather(connecting, disconnecting)

        assert server.stop_calls == 1
        assert server._started is False
        assert transport._connected is False  # noqa: SLF001
        assert transport._server is None  # noqa: SLF001

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
