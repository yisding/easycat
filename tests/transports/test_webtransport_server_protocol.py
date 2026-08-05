"""WebTransport server wiring and H3 protocol tests."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import easycat.transports.webtransport as webtransport_module
from easycat.runtime.scope import RuntimeScope, RuntimeScopeState, RuntimeSupervisor
from easycat.server.auth import BearerTokenAuth
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
    def test_config_defaults_to_loopback_without_auth(self) -> None:
        config = WebTransportTransportConfig()
        assert config.host == "127.0.0.1"
        assert config.auth_token is None
        assert config.allow_query_token is False
        assert config.unsafe_allow_no_auth is False

    def test_auth_fields_preserve_existing_positional_config_order(self) -> None:
        names = [field.name for field in fields(WebTransportTransportConfig)]
        assert names[:9] == [
            "host",
            "port",
            "certfile",
            "keyfile",
            "audio_format",
            "max_pending_chunks",
            "outbound_max_pending",
            "path",
            "max_concurrent_sessions",
        ]
        assert names[9:] == [
            "auth_token",
            "allow_query_token",
            "unsafe_allow_no_auth",
            "max_pending_bytes",
            "force_shutdown_timeout_s",
        ]

    def test_server_keeps_auth_token_out_of_per_session_config(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(
            WebTransportTransportConfig(auth_token="sekrit"),
            _noop,
        )

        assert server._auth_policy is not None
        assert server._session_config.auth_token is None

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
        assert server._server is None
        assert server._started is False

    @pytest.mark.asyncio
    async def test_start_requires_cert(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(WebTransportTransportConfig(), _noop)
        with pytest.raises(ValueError, match="certfile and keyfile"):
            await server.start()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("auth_token", [None, "   "])
    async def test_start_refuses_public_bind_without_auth_before_tls_setup(
        self,
        auth_token: str | None,
    ) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(
            WebTransportTransportConfig(
                host="0.0.0.0",
                certfile="cert.pem",
                keyfile="key.pem",
                auth_token=auth_token,
            ),
            _noop,
        )
        with pytest.raises(ValueError) as exc:
            await server.start()
        assert "0.0.0.0" in str(exc.value)
        assert "unsafe_allow_no_auth" in str(exc.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config",
        [
            WebTransportTransportConfig(host="0.0.0.0", auth_token="sekrit"),
            WebTransportTransportConfig(host="0.0.0.0", unsafe_allow_no_auth=True),
        ],
    )
    async def test_public_bind_auth_or_explicit_escape_reaches_tls_validation(
        self,
        config: WebTransportTransportConfig,
    ) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        with pytest.raises(ValueError, match="certfile and keyfile"):
            await WebTransportServer(config, _noop).start()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_before_start(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"), _noop
        )
        await server.stop()

    @staticmethod
    def _patch_server_start_dependencies(
        monkeypatch: pytest.MonkeyPatch,
        serve: Any,
    ) -> None:
        monkeypatch.setattr(
            webtransport_module,
            "_build_quic_configuration",
            lambda _cert, _key: object(),
        )
        monkeypatch.setattr(
            webtransport_module,
            "_preflight_aioquic_backpressure_api",
            lambda: None,
        )
        monkeypatch.setattr(
            webtransport_module,
            "_protocol_factory",
            lambda **_kwargs: object(),
        )
        monkeypatch.setattr(
            webtransport_module,
            "require_module",
            lambda *_args, **_kwargs: SimpleNamespace(serve=serve),
        )

    @pytest.mark.asyncio
    async def test_concurrent_starts_bind_one_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        serve_started = asyncio.Event()
        release_serve = asyncio.Event()
        bound = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        serve_calls = 0

        async def serve(*_args: object, **_kwargs: object) -> object:
            nonlocal serve_calls
            serve_calls += 1
            serve_started.set()
            await release_serve.wait()
            return bound

        self._patch_server_start_dependencies(monkeypatch, serve)
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda _transport: asyncio.sleep(0),
        )
        first = asyncio.create_task(server.start())
        await serve_started.wait()
        second = asyncio.create_task(server.start())
        await asyncio.sleep(0)

        assert serve_calls == 1
        assert not second.done()
        release_serve.set()
        await asyncio.gather(first, second)
        assert server._accepting_sessions is True
        await server.stop()

        assert serve_calls == 1
        bound.close.assert_called_once()
        bound.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restart_reopens_session_admission(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_bound = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        second_bound = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        bounds = iter([first_bound, second_bound])

        async def serve(*_args: object, **_kwargs: object) -> object:
            return next(bounds)

        self._patch_server_start_dependencies(monkeypatch, serve)
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda _transport: asyncio.sleep(0),
        )

        await server.start()
        assert server._can_accept_session() is True
        await server.stop()
        assert server._can_accept_session() is False

        await server.start()
        assert server._can_accept_session() is True
        await server.stop()

        first_bound.close.assert_called_once()
        first_bound.wait_closed.assert_awaited_once()
        second_bound.close.assert_called_once()
        second_bound.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_during_start_waits_then_closes_published_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        serve_started = asyncio.Event()
        release_serve = asyncio.Event()
        bound = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())

        async def serve(*_args: object, **_kwargs: object) -> object:
            serve_started.set()
            await release_serve.wait()
            return bound

        self._patch_server_start_dependencies(monkeypatch, serve)
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda _transport: asyncio.sleep(0),
        )
        starting = asyncio.create_task(server.start())
        await serve_started.wait()
        stopping = asyncio.create_task(server.stop())
        await asyncio.sleep(0)
        assert not stopping.done()

        release_serve.set()
        await asyncio.gather(starting, stopping)

        assert server._started is False
        assert server._accepting_sessions is False
        assert server._server is None
        bound.close.assert_called_once()
        bound.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_rejects_late_session_during_wait_closed(self) -> None:
        wait_closed_entered = asyncio.Event()
        release_wait_closed = asyncio.Event()
        handler_called = asyncio.Event()

        async def wait_closed() -> None:
            wait_closed_entered.set()
            await release_wait_closed.wait()

        async def handler(_transport: WebTransportConnectionTransport) -> None:
            handler_called.set()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            handler,
        )
        bound = SimpleNamespace(close=Mock(), wait_closed=AsyncMock(side_effect=wait_closed))
        server._server = bound
        server._started = True
        server._accepting_sessions = True

        stopping = asyncio.create_task(server.stop())
        await wait_closed_entered.wait()

        late_protocol = _FakeQuicProtocol()
        late_transport = WebTransportConnectionTransport(
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=late_protocol,  # type: ignore[arg-type]
            _session_id=0,
        )
        assert server._can_accept_session() is False
        server._dispatch_session(late_transport)

        assert late_protocol.close_calls == [(0, "server not accepting sessions")]
        assert server._handler_tasks == set()
        assert handler_called.is_set() is False

        release_wait_closed.set()
        await stopping

        assert server._started is False
        assert server._accepting_sessions is False
        assert server._server is None
        assert server._handler_tasks == set()

    @pytest.mark.asyncio
    async def test_server_cleanup_failures_are_best_effort_and_retryable(self) -> None:
        async def _noop(_transport: WebTransportConnectionTransport) -> None:
            return

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            _noop,
        )
        bound = SimpleNamespace(
            close=Mock(side_effect=[RuntimeError("server close failed"), None]),
            wait_closed=AsyncMock(side_effect=[RuntimeError("server wait failed"), None]),
        )
        server._server = bound
        server._started = True

        with pytest.raises(RuntimeError, match="server close failed"):
            await server.stop()

        bound.close.assert_called_once()
        bound.wait_closed.assert_awaited_once()
        assert server._started is False
        assert server._server is bound
        with pytest.raises(RuntimeError, match="previous cleanup is incomplete"):
            await server.start()

        await server.stop()

        assert bound.close.call_count == 2
        assert bound.wait_closed.await_count == 2
        assert server._server is None
        assert server._cleanup_error is None

    @pytest.mark.asyncio
    async def test_server_stop_does_not_hide_attribute_error_from_wait_closed(self) -> None:
        async def _noop(_transport: WebTransportConnectionTransport) -> None:
            return

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            _noop,
        )
        wait_error = AttributeError("wait implementation failed")
        bound = SimpleNamespace(
            close=Mock(),
            wait_closed=AsyncMock(side_effect=wait_error),
        )
        server._server = bound
        server._started = True

        with pytest.raises(AttributeError, match="wait implementation failed"):
            await server.stop()

        assert server._cleanup_error is wait_error
        assert server._server is bound

    @pytest.mark.asyncio
    async def test_handler_disconnect_failure_is_retained_and_retried_by_stop(self) -> None:
        async def _noop(_transport: WebTransportConnectionTransport) -> None:
            return

        server = WebTransportServer(
            WebTransportTransportConfig(max_concurrent_sessions=1),
            _noop,
        )
        server._started = True
        server._accepting_sessions = True
        transport = WebTransportConnectionTransport()
        stop_error = RuntimeError("session stop failed once")
        session = SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(side_effect=[stop_error, None]),
            close_connection=Mock(),
        )
        transport._session = session  # type: ignore[assignment]

        await server._run_handler(transport)

        assert transport._session_stop_pending is True
        assert server._pending_transport_cleanup == {transport}
        assert server._cleanup_error is stop_error
        assert server._can_accept_session() is False

        await server.stop()

        assert session.stop.await_count == 2
        assert transport._session_stop_pending is False
        assert server._pending_transport_cleanup == set()
        assert server._cleanup_error is None

    @pytest.mark.asyncio
    async def test_handler_does_not_suppress_process_control_from_disconnect(self) -> None:
        async def _noop(_transport: WebTransportConnectionTransport) -> None:
            return

        server = WebTransportServer(WebTransportTransportConfig(), _noop)
        stop = SystemExit("stop process")
        transport = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=AsyncMock(side_effect=stop),
            _disconnect_cleanup_error=None,
        )

        with pytest.raises(SystemExit, match="stop process"):
            await server._run_handler(transport)  # type: ignore[arg-type]

        assert server._pending_transport_cleanup == set()
        assert server._cleanup_error is None

    @pytest.mark.asyncio
    async def test_stop_retains_transport_cleanup_until_a_later_retry_succeeds(self) -> None:
        async def _noop(_transport: WebTransportConnectionTransport) -> None:
            return

        server = WebTransportServer(WebTransportTransportConfig(), _noop)
        transport = WebTransportConnectionTransport()
        first_error = RuntimeError("handler disconnect failed")
        retry_error = RuntimeError("server stop retry failed")
        session = SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(side_effect=[first_error, retry_error, None]),
            close_connection=Mock(),
        )
        transport._session = session  # type: ignore[assignment]

        await server._run_handler(transport)

        with pytest.raises(RuntimeError, match="server stop retry failed"):
            await server.stop()

        assert server._pending_transport_cleanup == {transport}
        assert server._cleanup_error is retry_error

        await server.stop()

        assert session.stop.await_count == 3
        assert server._pending_transport_cleanup == set()
        assert server._cleanup_error is None

    @pytest.mark.asyncio
    async def test_cancelled_stop_blocks_restart_until_server_cleanup_retry(self) -> None:
        wait_entered = asyncio.Event()

        async def block_wait_closed() -> None:
            wait_entered.set()
            await asyncio.Event().wait()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda _transport: asyncio.sleep(0),
        )
        bound = SimpleNamespace(
            close=Mock(),
            wait_closed=AsyncMock(side_effect=block_wait_closed),
        )
        server._server = bound
        server._started = True

        stopping = asyncio.create_task(server.stop())
        await wait_entered.wait()
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping

        assert server._started is False
        assert server._server is bound
        assert isinstance(server._cleanup_error, RuntimeError)
        with pytest.raises(RuntimeError, match="previous cleanup is incomplete"):
            await server.start()

        bound.wait_closed = AsyncMock()
        await server.stop()
        assert server._server is None
        assert server._cleanup_error is None

    @pytest.mark.asyncio
    async def test_stop_bounds_cancellation_resistant_handler_until_retry(self) -> None:
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def ignores_cancellation() -> None:
            handler_started.set()
            while not release_handler.is_set():
                try:
                    await release_handler.wait()
                except asyncio.CancelledError:
                    continue

        server = WebTransportServer(
            WebTransportTransportConfig(
                certfile="cert.pem",
                keyfile="key.pem",
                force_shutdown_timeout_s=0.01,
            ),
            lambda _transport: asyncio.sleep(0),
        )
        bound = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        server._server = bound
        server._started = True
        handler = asyncio.create_task(ignores_cancellation())
        server._handler_task_scope.adopt_task(handler)
        await handler_started.wait()

        with pytest.raises(RuntimeError, match="session handler.*did not stop"):
            await asyncio.wait_for(server.stop(), timeout=1)

        assert handler in server._handler_tasks
        assert server._server is None
        assert server._cleanup_error is not None
        with pytest.raises(RuntimeError, match="previous cleanup is incomplete"):
            await server.start()

        release_handler.set()
        await handler
        await server.stop()

        assert server._handler_tasks == set()
        assert server._cleanup_error is None

    @pytest.mark.asyncio
    async def test_stop_bounds_cancellation_resistant_listener_until_retry(self) -> None:
        wait_entered = asyncio.Event()
        release_wait_closed = asyncio.Event()

        async def ignores_cancellation() -> None:
            wait_entered.set()
            while not release_wait_closed.is_set():
                try:
                    await release_wait_closed.wait()
                except asyncio.CancelledError:
                    continue

        server = WebTransportServer(
            WebTransportTransportConfig(
                certfile="cert.pem",
                keyfile="key.pem",
                force_shutdown_timeout_s=0.01,
            ),
            lambda _transport: asyncio.sleep(0),
        )
        bound = SimpleNamespace(
            close=Mock(),
            wait_closed=AsyncMock(side_effect=ignores_cancellation),
        )
        server._server = bound
        server._started = True

        stopping = asyncio.create_task(server.stop())
        await wait_entered.wait()
        with pytest.raises(RuntimeError, match="listener did not close"):
            await asyncio.wait_for(stopping, timeout=1)

        assert server._server is bound
        assert server._cleanup_error is not None
        with pytest.raises(RuntimeError, match="previous cleanup is incomplete"):
            await server.start()

        # A retry while the first cancellation-resistant waiter is still live
        # must re-await it, not invoke ``wait_closed()`` a second time against
        # the same aioquic server.
        with pytest.raises(RuntimeError, match="listener did not close"):
            await asyncio.wait_for(server.stop(), timeout=1)
        assert bound.wait_closed.await_count == 1

        release_wait_closed.set()
        await asyncio.sleep(0)
        await server.stop()

        assert server._server is None
        assert server._cleanup_error is None
        assert bound.wait_closed.await_count == 1

    @pytest.mark.asyncio
    async def test_stop_safe_when_called_from_within_handler(self) -> None:
        """A handler that triggers ``server.stop()`` mustn't deadlock by
        gathering its own task (regression for review #3/#8).
        """
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda transport: asyncio.sleep(0),  # type: ignore[arg-type]
        )
        server._started = True

        # Run ``stop()`` from within a separate task so that
        # ``asyncio.current_task()`` inside ``stop()`` reliably matches
        # the handler-task registration on every Python version.
        # (3.11's ``asyncio.wait_for`` wraps the inner coro in a new
        # task, which would otherwise mask the regression we're guarding.)
        async def handler_calls_stop() -> None:
            handler_task = asyncio.current_task()
            assert handler_task is not None
            server._handler_task_scope.adopt_task(handler_task)
            await server.stop()

        await asyncio.wait_for(asyncio.create_task(handler_calls_stop()), timeout=1)
        await server.stop()

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
        server._started = True
        server._accepting_sessions = True

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
            server._dispatch_session(t)
        await asyncio.wait_for(handler_started.wait(), timeout=1.0)
        assert len(server._handler_tasks) == 2

        # Third session is over the cap → force-closed, handler not invoked.
        overflow, overflow_proto = _make_transport()
        server._dispatch_session(overflow)
        assert overflow_proto.close_calls == [(0, "session cap reached")]
        assert len(server._handler_tasks) == 2

        release_handlers.set()
        await asyncio.gather(*server._handler_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_dispatch_handler_uses_attached_server_scope(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(_transport: WebTransportConnectionTransport) -> None:
            started.set()
            await release.wait()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            handler,
        )
        root = RuntimeScope.create_root(
            name="application",
            root_id="test-root:webtransport-handlers",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        server.set_runtime_scope(root, name="webtransport-server-runtime")
        server._started = True
        server._accepting_sessions = True
        transport = WebTransportConnectionTransport(
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )

        server._dispatch_session(transport)
        await started.wait()
        task = root.tasks("webtransport_handler")[0]

        assert server._handler_tasks == {task}
        assert "transport-handlers" in root.cohorts(force=False)

        release.set()
        await task
        await asyncio.sleep(0)

        assert not root.tasks("webtransport_handler")
        await server.stop()

    @pytest.mark.asyncio
    async def test_completed_standalone_handler_closes_local_runtime_root(self) -> None:
        release = asyncio.Event()
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda _transport: release.wait(),
        )
        server._started = True
        server._accepting_sessions = True
        transport = WebTransportConnectionTransport(
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )

        server._dispatch_session(transport)
        await asyncio.sleep(0)
        standalone = server._handler_task_scope.scope
        assert standalone is not None
        task = server._handler_tasks.pop()

        release.set()
        await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert standalone.state is RuntimeScopeState.CLOSED
        await server.stop()

    @pytest.mark.asyncio
    async def test_standalone_handler_remains_visible_through_transport_cleanup(self) -> None:
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda _transport: asyncio.sleep(0),
        )
        server._started = True
        server._accepting_sessions = True
        transport = WebTransportConnectionTransport(
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )
        transport.connect = AsyncMock()

        async def disconnect() -> None:
            disconnect_started.set()
            await release_disconnect.wait()

        transport.disconnect = disconnect  # type: ignore[method-assign]
        server._dispatch_session(transport)
        await disconnect_started.wait()

        task = next(iter(server._handler_tasks))
        assert not task.done()

        release_disconnect.set()
        await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not server._handler_tasks

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
        assert server._can_accept_session() is False
        server._started = True
        server._accepting_sessions = True
        assert server._can_accept_session() is True

        release_slots = asyncio.Event()

        async def hold_slot() -> None:
            await release_slots.wait()

        held = [asyncio.create_task(hold_slot()) for _ in range(2)]
        for task in held:
            server._handler_task_scope.adopt_task(task)
        try:
            # At the cap → the protocol would send 503 and create no transport.
            assert server._can_accept_session() is False
        finally:
            release_slots.set()
            await asyncio.gather(*held, return_exceptions=True)
            for task in held:
                server._handler_task_scope.discard_task(task)
            await server._handler_task_scope.release_standalone_if_empty()
        assert server._can_accept_session() is True


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


@pytest.mark.asyncio
async def test_webtransport_shutdown_surfaces_failed_session_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from easycat.server.webtransport import _shutdown_webtransport_sessions
    from easycat.session_manager import SessionStopFailure, SessionStopReport

    report = SessionStopReport(
        attempted_keys=(7,),
        stopped_keys=(),
        failures=(
            SessionStopFailure(key=7, exception=RuntimeError("webtransport teardown failed")),
        ),
    )
    server = SimpleNamespace(stop=AsyncMock())
    manager = SimpleNamespace(stop_all=AsyncMock(return_value=report))

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(
            RuntimeError,
            match="WebTransport shutdown retained 1 session",
        ),
    ):
        await _shutdown_webtransport_sessions(server, manager)  # type: ignore[arg-type]

    assert "WebTransport session shutdown failed to stop 1 of 1 session" in caplog.text
    assert "webtransport teardown failed" in caplog.text
    server.stop.assert_awaited_once()
    manager.stop_all.assert_awaited_once()


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
    proto._h3 = object()  # only asserted non-None
    rec = _Recorder()
    proto._wt_transport = rec  # type: ignore[assignment]
    proto._accepted_session_id = 5

    proto._handle_h3_event(
        WebTransportStreamDataReceived(data=b"hi", stream_id=8, stream_ended=False, session_id=5)
    )
    proto._handle_h3_event(
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
    proto._wt_transport = rec  # type: ignore[assignment]

    proto.quic_event_received(
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
    proto._h3 = object()  # only asserted non-None
    rec = _LostRecorder()
    proto._wt_transport = rec  # type: ignore[assignment]
    proto._accepted_session_id = 5

    # FIN on an unrelated stream id → not our session.
    proto._handle_h3_event(DataReceived(data=b"", stream_id=9, stream_ended=True))
    # Non-final data on the CONNECT stream → session still open.
    proto._handle_h3_event(DataReceived(data=b"x", stream_id=5, stream_ended=False))
    assert rec.lost_calls == 0

    # Lone FIN on the accepted CONNECT/session stream → session closed.
    proto._handle_h3_event(DataReceived(data=b"", stream_id=5, stream_ended=True))
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
    proto._h3 = object()
    proto._wt_transport = None
    proto._accepted_session_id = None

    proto.quic_event_received(
        ConnectionTerminated(error_code=0, frame_type=None, reason_phrase="")
    )
    proto._handle_h3_event(DataReceived(data=b"", stream_id=7, stream_ended=True))


class _RecordingH3:
    """Records ``send_headers`` so accept/reject decisions can be asserted."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, list[tuple[bytes, bytes]], bool]] = []

    def send_headers(
        self, stream_id: int, headers: list[tuple[bytes, bytes]], end_stream: bool = False
    ) -> None:
        self.sent.append((stream_id, headers, end_stream))


def _connect_protocol(auth_policy: BearerTokenAuth | None) -> tuple[Any, _RecordingH3, list[Any]]:
    cls = _get_protocol_class()
    proto = cls.__new__(cls)  # skip QUIC-bound __init__
    h3 = _RecordingH3()
    proto._h3 = h3  # type: ignore[assignment]
    proto._accept_path = "/easycat"
    proto._wt_transport = None
    proto._accepted_session_id = None
    on_session_calls: list[Any] = []
    proto._on_session = on_session_calls.append
    proto._can_accept = lambda: True
    proto._session_config = WebTransportTransportConfig()
    proto._auth_policy = auth_policy
    proto.transmit = lambda: None  # type: ignore[method-assign]
    return proto, h3, on_session_calls


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
@pytest.mark.parametrize(
    "authorization",
    [None, b"Bearer wrong"],
)
def test_connect_bearer_auth_rejects_missing_or_invalid_token(
    authorization: bytes | None,
) -> None:
    from aioquic.h3.events import HeadersReceived

    proto, h3, on_session_calls = _connect_protocol(BearerTokenAuth(token="sekrit"))
    headers = [
        (b":method", b"CONNECT"),
        (b":protocol", b"webtransport"),
        (b":path", b"/easycat"),
    ]
    if authorization is not None:
        headers.append((b"authorization", authorization))

    proto._handle_h3_event(HeadersReceived(headers=headers, stream_id=0, stream_ended=False))

    assert proto._wt_transport is None
    assert on_session_calls == []
    assert dict(h3.sent[0][1]).get(b":status") == b"401"
    assert dict(h3.sent[0][1]).get(b"www-authenticate") == b"Bearer"
    assert h3.sent[0][2] is True


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_connect_bearer_auth_accepts_correct_header() -> None:
    from aioquic.h3.events import HeadersReceived

    proto, h3, on_session_calls = _connect_protocol(BearerTokenAuth(token="sekrit"))
    proto._handle_h3_event(
        HeadersReceived(
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"webtransport"),
                (b":path", b"/easycat"),
                (b"authorization", b"Bearer sekrit"),
            ],
            stream_id=0,
            stream_ended=False,
        )
    )

    assert proto._wt_transport is not None
    assert len(on_session_calls) == 1
    assert dict(h3.sent[0][1]).get(b":status") == b"200"


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
@pytest.mark.parametrize(
    ("allow_query_token", "expected_status"),
    [(False, b"401"), (True, b"200")],
)
def test_connect_query_token_requires_explicit_opt_in(
    allow_query_token: bool,
    expected_status: bytes,
) -> None:
    from aioquic.h3.events import HeadersReceived

    proto, h3, on_session_calls = _connect_protocol(
        BearerTokenAuth(token="sekrit", allow_query_token=allow_query_token)
    )
    proto._handle_h3_event(
        HeadersReceived(
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"webtransport"),
                (b":path", b"/easycat?token=sekrit"),
            ],
            stream_id=0,
            stream_ended=False,
        )
    )

    assert dict(h3.sent[0][1]).get(b":status") == expected_status
    assert len(on_session_calls) == (1 if allow_query_token else 0)


@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
def test_connect_malformed_path_is_rejected_without_protocol_crash() -> None:
    from aioquic.h3.events import HeadersReceived

    proto, h3, on_session_calls = _connect_protocol(None)
    proto._handle_h3_event(
        HeadersReceived(
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"webtransport"),
                (b":path", b"//[malformed"),
            ],
            stream_id=0,
            stream_ended=False,
        )
    )

    assert dict(h3.sent[0][1]).get(b":status") == b"400"
    assert on_session_calls == []


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
    proto._h3 = h3  # type: ignore[assignment]
    proto._accept_path = "/easycat"
    proto._wt_transport = None
    proto._accepted_session_id = None
    on_session_calls: list[Any] = []
    proto._on_session = on_session_calls.append
    proto._can_accept = lambda: True
    proto.transmit = lambda: None  # type: ignore[method-assign]

    proto._handle_h3_event(
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

    assert proto._wt_transport is None
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
    proto._h3 = h3  # type: ignore[assignment]
    proto._accept_path = "/easycat"
    proto._wt_transport = None
    proto._accepted_session_id = None
    on_session_calls: list[Any] = []
    proto._on_session = on_session_calls.append
    proto._can_accept = lambda: True
    proto._session_config = WebTransportTransportConfig()
    proto.transmit = lambda: None  # type: ignore[method-assign]

    proto._handle_h3_event(
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

    assert proto._wt_transport is not None
    assert len(on_session_calls) == 1
    _sid, hdrs, end = h3.sent[0]
    assert dict(hdrs).get(b":status") == b"200"
    assert end is False
