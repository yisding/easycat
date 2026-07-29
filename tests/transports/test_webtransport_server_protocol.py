"""WebTransport server wiring and H3 protocol tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import easycat.transports.webtransport as webtransport_module
from easycat.server.webtransport import (
    run_webtransport_config_server,
    serve_webtransport_config_sessions,
)
from easycat.transports.webtransport import (
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransportConfig,
    _get_protocol_class,
    _preflight_aioquic_backpressure_api,
)

from ._webtransport_helpers import _aioquic_available, _FakeH3, _FakeQuicProtocol


class TestWebTransportServerWiring:
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _aioquic_available(),
        reason="aioquic not installed ([webtransport] extra)",
    )
    async def test_installed_aioquic_passes_backpressure_preflight(self) -> None:
        _preflight_aioquic_backpressure_api()

    @pytest.mark.asyncio
    async def test_backpressure_preflight_rejects_missing_private_buffer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeConfiguration:
            def __init__(self, *, is_client: bool) -> None:
                self.is_client = is_client

        class FakeQuic:
            def __init__(self, *, configuration: object) -> None:
                self.configuration = configuration
                self._streams: dict[int, object] = {}

            def get_next_available_stream_id(self) -> int:
                return 0

            def send_stream_data(self, stream_id: int, _data: bytes) -> None:
                self._streams[stream_id] = SimpleNamespace(sender=object())

        class FakeProtocol:
            def __init__(self, quic: FakeQuic) -> None:
                self._quic = quic

        modules = {
            "aioquic.quic.configuration": SimpleNamespace(QuicConfiguration=FakeConfiguration),
            "aioquic.quic.connection": SimpleNamespace(QuicConnection=FakeQuic),
        }
        monkeypatch.setattr(
            webtransport_module,
            "require_module",
            lambda name, **_kwargs: modules[name],
        )
        monkeypatch.setattr(webtransport_module, "_get_protocol_class", lambda: FakeProtocol)

        with pytest.raises(
            RuntimeError,
            match=r"QuicStream\.sender\._buffer missing.*Required access path",
        ):
            _preflight_aioquic_backpressure_api()

    @pytest.mark.asyncio
    async def test_start_preflights_before_binding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        monkeypatch.setattr(
            webtransport_module,
            "_build_quic_configuration",
            lambda _cert, _key: object(),
        )

        def fail_preflight() -> None:
            raise RuntimeError("incompatible aioquic")

        monkeypatch.setattr(
            webtransport_module,
            "_preflight_aioquic_backpressure_api",
            fail_preflight,
        )

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            _noop,
        )
        with pytest.raises(RuntimeError, match="incompatible aioquic"):
            await server.start()
        assert server._server is None  # noqa: SLF001
        assert server._started is False  # noqa: SLF001

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
        release_handlers = asyncio.Event()
        handler_start_count = 0

        async def _handler(_t: WebTransportConnectionTransport) -> None:
            nonlocal handler_start_count
            handler_start_count += 1
            if handler_start_count == 2:
                handler_started.set()
            await release_handlers.wait()

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
        await asyncio.wait_for(handler_started.wait(), timeout=1.0)
        assert len(server._handler_tasks) == 2  # noqa: SLF001

        # Third session is over the cap → force-closed, handler not invoked.
        overflow, overflow_proto = _make_transport()
        server._dispatch_session(overflow)  # noqa: SLF001
        assert overflow_proto.close_calls == [(0, "session cap reached")]
        assert len(server._handler_tasks) == 2  # noqa: SLF001 — unchanged

        release_handlers.set()
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

        release_slots = asyncio.Event()

        async def hold_slot() -> None:
            await release_slots.wait()

        held = [asyncio.create_task(hold_slot()) for _ in range(2)]
        server._handler_tasks.update(held)  # noqa: SLF001
        try:
            # At the cap → the protocol would send 503 and create no transport.
            assert server._can_accept_session() is False  # noqa: SLF001
        finally:
            release_slots.set()
            await asyncio.gather(*held, return_exceptions=True)
            server._handler_tasks.difference_update(held)  # noqa: SLF001
        assert server._can_accept_session() is True  # noqa: SLF001 — slots freed


class _FakeManagedSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self) -> None:
        self.stopped.set()


class _FakeAcceptedWebTransport:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def wait_closed(self) -> None:
        await self.closed.wait()


@pytest.mark.asyncio
async def test_serve_webtransport_config_sessions_manages_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.config as config_module
    import easycat.server.webtransport as webtransport_module

    config = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
    stop_event = asyncio.Event()
    accepted_transport = _FakeAcceptedWebTransport()
    server_started = asyncio.Event()
    sessions: list[_FakeManagedSession] = []
    configs: list[dict[str, object]] = []
    transports: list[object] = []

    class FakeWebTransportServer:
        def __init__(self, cfg: WebTransportTransportConfig, handler: Any) -> None:
            self.config = cfg
            self.handler = handler
            self.task: asyncio.Task[None] | None = None

        async def start(self) -> None:
            assert self.config is config
            self.task = asyncio.create_task(self.handler(accepted_transport))
            server_started.set()

        async def stop(self) -> None:
            accepted_transport.closed.set()
            if self.task is not None:
                await asyncio.wait_for(self.task, timeout=1)

    def create_session(config: dict[str, object]) -> _FakeManagedSession:
        configs.append(config)
        session = _FakeManagedSession()
        sessions.append(session)
        return session

    def config_factory(transport: object) -> dict[str, object]:
        transports.append(transport)
        return {"transport": transport, "agent": object()}

    monkeypatch.setattr(webtransport_module, "WebTransportServer", FakeWebTransportServer)
    monkeypatch.setattr(config_module, "create_session", create_session)

    task = asyncio.create_task(
        serve_webtransport_config_sessions(
            config_factory,
            config,
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
        )
    )
    try:
        await asyncio.wait_for(server_started.wait(), timeout=1)
        assert transports == [accepted_transport]
        assert len(configs) == 1
        assert configs[0]["transport"] is accepted_transport
        assert "agent" in configs[0]
        await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(sessions[0].stopped.wait(), timeout=1)
    finally:
        if not task.done():
            stop_event.set()
            accepted_transport.closed.set()
            await asyncio.wait_for(task, timeout=1)


def test_run_webtransport_config_server_delegates_to_async_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.webtransport as webtransport_module

    config = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
    calls: list[dict[str, object]] = []

    def config_factory(transport: object) -> object:
        return {"transport": transport}

    async def fake_serve(
        factory: object,
        cfg: WebTransportTransportConfig,
        *,
        runtime_feedback: bool,
        announce: bool,
    ) -> None:
        calls.append(
            {
                "factory": factory,
                "config": cfg,
                "runtime_feedback": runtime_feedback,
                "announce": announce,
            }
        )

    monkeypatch.setattr(webtransport_module, "serve_webtransport_config_sessions", fake_serve)

    run_webtransport_config_server(
        config_factory,
        config,
        runtime_feedback=False,
        announce=False,
    )

    assert calls == [
        {
            "factory": config_factory,
            "config": config,
            "runtime_feedback": False,
            "announce": False,
        }
    ]


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
