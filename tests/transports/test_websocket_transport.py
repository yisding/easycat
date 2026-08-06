"""WebSocket server transport tests."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest
import websockets

from easycat.audio_format import AudioChunk
from easycat.events import EventBus
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketTransport,
    WebSocketTransportConfig,
)

from ._webrtc_fakes import _UsesPytestTcpPortFactory
from .conftest import make_chunk

_make_chunk = make_chunk


class _ClosingReadyWebSocket:
    async def send(self, _message: str | bytes) -> None:
        raise websockets.exceptions.ConnectionClosed(None, None)


class _BlockingReadyWebSocket:
    def __init__(self) -> None:
        self.send_started = asyncio.Event()
        self.closed = False

    async def send(self, _message: str | bytes) -> None:
        self.send_started.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class _FailingReadyWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, _message: str | bytes) -> None:
        raise RuntimeError("ready send failed")

    async def close(self) -> None:
        self.closed = True


class _FailOnceClosingWebSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("socket close failed")


class _CancelOnceClosingWebSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise asyncio.CancelledError


class _BlockingFirstCloseWebSocket:
    def __init__(self) -> None:
        self.close_calls = 0
        self.close_started = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            self.close_started.set()
            await asyncio.Future()


class _TrackingCloseWebSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _ClosedServer:
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _DeferredEndWebSocket:
    def __init__(self) -> None:
        self.receive_started = asyncio.Event()
        self.release_receive = asyncio.Event()

    def __aiter__(self) -> _DeferredEndWebSocket:
        return self

    async def __anext__(self) -> bytes:
        self.receive_started.set()
        await self.release_receive.wait()
        raise StopAsyncIteration


class _TailOnlyResampler:
    def __init__(self, _target_rate: int) -> None:
        pass

    def finish(self) -> bytes:
        return b"stale-resampler-tail"


def test_repository_websocket_servers_set_message_size_limit():
    """Every shipped WebSocket listener must bound decoded message size."""
    repository_root = Path(__file__).resolve().parents[2]
    server_calls: list[tuple[Path, int]] = []
    missing_limit: list[tuple[Path, int]] = []

    for root in (repository_root / "src", repository_root / "examples"):
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "websockets.serve(" not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "serve"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "websockets"
                ):
                    continue
                server_calls.append((path, node.lineno))
                if not any(keyword.arg == "max_size" for keyword in node.keywords):
                    missing_limit.append((path, node.lineno))

    assert server_calls
    assert missing_limit == []


def test_websocket_transport_config_defaults_to_loopback():
    config = WebSocketTransportConfig()

    assert config.host == "127.0.0.1"


@pytest.mark.parametrize(
    "method_name",
    [
        "_receive_loop",
        "_handle_control_message",
        "send_audio",
        "clear_audio",
        "_send_client_event",
    ],
)
def test_websocket_transports_share_wire_protocol_methods(method_name: str):
    assert getattr(WebSocketTransport, method_name) is getattr(
        WebSocketConnectionTransport,
        method_name,
    )


@pytest.mark.asyncio
async def test_replaced_websocket_drops_prior_connection_resampler_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_ws = _DeferredEndWebSocket()
    replacement_ws = object()
    transport = WebSocketTransport()
    transport._ws = old_ws  # type: ignore[assignment]
    transport._connection_epoch.bump(old_ws)  # type: ignore[arg-type]
    old_connection = transport._connection_epoch.capture()

    monkeypatch.setattr(
        "easycat.transports.websocket.PCM16StreamResampler",
        _TailOnlyResampler,
    )
    receive_task = asyncio.create_task(transport._receive_loop(old_ws))  # type: ignore[arg-type]
    await old_ws.receive_started.wait()

    transport._ws = replacement_ws  # type: ignore[assignment]
    transport._connection_epoch.bump(replacement_ws)  # type: ignore[arg-type]
    transport._reset_audio_queue()
    old_ws.release_receive.set()
    await receive_task

    assert not old_connection.guard()
    assert transport._ws is replacement_ws
    assert transport._in_queue.empty()


def test_connection_transport_handles_start_and_stop_control_messages(
    caplog: pytest.LogCaptureFixture,
):
    transport = WebSocketConnectionTransport(object())  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG, logger="easycat.transports.websocket"):
        transport._handle_control_message('{"type":"start"}')
        transport._handle_control_message('{"type":"stop"}')

    assert "Client sent start signal" in caplog.messages
    assert "Client sent stop signal" in caplog.messages


@pytest.mark.asyncio
async def test_connection_transport_ready_disconnect_is_not_raised():
    transport = WebSocketConnectionTransport(_ClosingReadyWebSocket())  # type: ignore[arg-type]

    await transport.connect()

    assert transport.is_connected is False
    assert transport._ws is None
    assert transport._receive_task is None
    assert transport._in_queue.get_nowait() is None


@pytest.mark.asyncio
async def test_connection_receive_loop_attaches_to_transport_scope():
    class _ConnectedWebSocket:
        def __init__(self) -> None:
            self.receive_started = asyncio.Event()
            self.close_calls = 0

        async def send(self, _message: str | bytes) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            self.receive_started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

    ws = _ConnectedWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    root = RuntimeScope.create_root(
        name="session",
        root_id="test-root:websocket-receive",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    transport.set_runtime_scope(root, name="transport-runtime")

    await transport.connect()
    await ws.receive_started.wait()
    connection = transport._connection_epoch.capture()

    assert root.tasks("websocket_receive") == (transport._receive_task,)
    assert "transport-receive" in root.cohorts(force=False)
    assert connection.guard()
    assert connection.value is ws

    await transport.disconnect()

    assert not connection.guard()
    assert transport._connection_epoch.capture().value is None
    assert not root.tasks("websocket_receive")
    assert ws.close_calls == 1


@pytest.mark.asyncio
async def test_connection_transport_clear_audio_close_race_is_not_raised():
    transport = WebSocketConnectionTransport(_ClosingReadyWebSocket())  # type: ignore[arg-type]
    transport._connected = True

    await transport.clear_audio()

    assert transport._ws is None


def test_connection_transport_ignores_large_integer_control_message():
    transport = WebSocketConnectionTransport(object())  # type: ignore[arg-type]

    transport._handle_control_message('{"type":' + "9" * 5000 + "}")
    transport._handle_control_message('{"type":"config","sample_rate":24000}')

    assert transport._audio_format.sample_rate == 24000


@pytest.mark.asyncio
async def test_connection_transport_connect_cancellation_keeps_socket_for_disconnect():
    ws = _BlockingReadyWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]

    connect_task = asyncio.create_task(transport.connect())
    await ws.send_started.wait()
    connect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect_task

    assert transport.is_connected is False
    assert transport._ws is ws
    await transport.disconnect()
    assert ws.closed is True
    assert transport._ws is None


@pytest.mark.asyncio
async def test_connection_transport_ready_error_keeps_socket_for_disconnect():
    ws = _FailingReadyWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ready send failed"):
        await transport.connect()

    assert transport.is_connected is False
    assert transport._ws is ws
    await transport.disconnect()
    assert ws.closed is True
    assert transport._ws is None


@pytest.mark.asyncio
async def test_connection_transport_concurrent_connects_share_ready_failure():
    class _DeferredFailingReadyWebSocket:
        def __init__(self) -> None:
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.closed = False

        async def send(self, _message: str | bytes) -> None:
            self.send_started.set()
            await self.release_send.wait()
            raise RuntimeError("ready send failed")

        async def close(self) -> None:
            self.closed = True

    ws = _DeferredFailingReadyWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    first = asyncio.create_task(transport.connect())
    await ws.send_started.wait()
    second = asyncio.create_task(transport.connect())
    await asyncio.sleep(0)

    assert not second.done()
    assert transport._receive_task is None
    assert transport._lifecycle_tasks.active("websocket-connection-connect")

    ws.release_send.set()
    with pytest.raises(RuntimeError, match="ready send failed"):
        await first
    with pytest.raises(RuntimeError, match="ready send failed"):
        await second

    assert not transport._lifecycle_tasks.active("websocket-connection-connect")
    assert transport.is_connected is False
    assert transport._ws is ws
    await transport.disconnect()
    assert ws.closed is True


@pytest.mark.asyncio
async def test_connection_transport_concurrent_disconnects_share_close_failure():
    class _BlockingFailOnceCloseWebSocket:
        def __init__(self) -> None:
            self.close_calls = 0
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                self.close_started.set()
                await self.release_close.wait()
                raise RuntimeError("socket close failed")

    ws = _BlockingFailOnceCloseWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    transport._connected = True

    first = asyncio.create_task(transport.disconnect())
    await ws.close_started.wait()
    second = asyncio.create_task(transport.disconnect())
    await asyncio.sleep(0)

    assert not second.done()
    assert ws.close_calls == 1
    assert transport._lifecycle_tasks.active("websocket-connection-disconnect")

    ws.release_close.set()
    with pytest.raises(RuntimeError, match="socket close failed"):
        await first
    with pytest.raises(RuntimeError, match="socket close failed"):
        await second

    assert not transport._lifecycle_tasks.active("websocket-connection-disconnect")
    assert ws.close_calls == 1
    assert transport._ws is ws
    assert isinstance(transport._disconnect_cleanup_error, RuntimeError)

    await transport.disconnect()

    assert ws.close_calls == 2
    assert transport._ws is None
    assert transport._disconnect_cleanup_error is None


@pytest.mark.asyncio
async def test_connection_transport_disconnect_follower_cancellation_is_shielded():
    class _BlockingCloseWebSocket:
        def __init__(self) -> None:
            self.close_calls = 0
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()

    ws = _BlockingCloseWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    transport._connected = True

    leader = asyncio.create_task(transport.disconnect())
    await ws.close_started.wait()
    follower = asyncio.create_task(transport.disconnect())
    await asyncio.sleep(0)

    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower

    assert not leader.done()
    assert ws.close_calls == 1

    ws.release_close.set()
    await leader

    assert ws.close_calls == 1
    assert transport._ws is None


@pytest.mark.asyncio
async def test_connection_transport_disconnect_emit_observer_does_not_join_itself():
    class _BlockingCloseWebSocket:
        def __init__(self) -> None:
            self.close_calls = 0
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()

    ws = _BlockingCloseWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    transport._connected = True

    disconnecting = asyncio.create_task(transport.disconnect())
    await ws.close_started.wait()

    reentrant = asyncio.create_task(transport.disconnect())
    transport._track_emit_task(reentrant)
    await asyncio.wait_for(reentrant, timeout=1)

    ws.release_close.set()
    await disconnecting

    assert ws.close_calls == 1
    assert transport._ws is None


@pytest.mark.asyncio
async def test_connection_transport_emit_observer_can_initiate_disconnect():
    ws = _TrackingCloseWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    transport._connected = True

    initiated = asyncio.create_task(transport.disconnect())
    transport._track_emit_task(initiated)

    await asyncio.wait_for(initiated, timeout=1)

    assert ws.close_calls == 1
    assert transport._ws is None


@pytest.mark.asyncio
async def test_connection_transport_retries_failed_socket_close() -> None:
    ws = _FailOnceClosingWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    transport._connected = True

    with pytest.raises(RuntimeError, match="socket close failed"):
        await transport.disconnect()

    assert transport.is_connected is False
    assert transport._ws is ws
    assert transport._disconnect_cleanup_error is not None
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await transport.connect()

    await transport.disconnect()

    assert ws.close_calls == 2
    assert transport._ws is None
    assert transport._disconnect_cleanup_error is None


@pytest.mark.asyncio
async def test_connection_disconnect_preserves_caller_cancellation_and_cleanup_ownership() -> None:
    ws = _TrackingCloseWebSocket()
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
    transport._connected = True
    child_cancelled = asyncio.Event()
    release_child = asyncio.Event()

    async def cancellation_resistant_receive_loop() -> None:
        while not release_child.is_set():
            try:
                await release_child.wait()
            except asyncio.CancelledError:
                child_cancelled.set()

    transport._receive_task = asyncio.create_task(cancellation_resistant_receive_loop())
    disconnecting = asyncio.create_task(transport.disconnect())
    await child_cancelled.wait()

    disconnecting.cancel()
    release_child.set()
    with pytest.raises(asyncio.CancelledError):
        await disconnecting

    assert ws.close_calls == 0
    assert transport._ws is ws
    assert isinstance(transport._disconnect_cleanup_error, RuntimeError)
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await transport.connect()

    await transport.disconnect()

    assert ws.close_calls == 1
    assert transport._ws is None
    assert transport._disconnect_cleanup_error is None


def test_websocket_transports_leave_server_side_aec_off_by_default():
    assert WebSocketTransportConfig.default_echo_cancellation_enabled is False
    assert WebSocketTransport.default_echo_cancellation_enabled is False
    assert WebSocketConnectionTransport.default_echo_cancellation_enabled is False


@pytest.mark.asyncio
async def test_server_websocket_transports_disable_compression(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    class FakeServer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def fake_serve(*_args: object, **kwargs: object) -> FakeServer:
        calls.append(kwargs)
        return FakeServer()

    monkeypatch.setattr("easycat.transports._base.websockets.serve", fake_serve)

    transport = WebSocketTransport(WebSocketTransportConfig())
    await transport.connect()
    try:
        assert calls == [
            {
                "compression": None,
                "max_size": MAX_WEBSOCKET_MESSAGE_BYTES,
            }
        ]
    finally:
        await transport.disconnect()


@pytest.mark.asyncio
async def test_concurrent_server_connects_publish_one_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners: list[FakeServer] = []
    first_serve_started = asyncio.Event()
    release_first_serve = asyncio.Event()

    class FakeServer:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            pass

    async def fake_serve(*_args: object, **_kwargs: object) -> FakeServer:
        server = FakeServer()
        listeners.append(server)
        if len(listeners) == 1:
            first_serve_started.set()
            await release_first_serve.wait()
        return server

    monkeypatch.setattr("easycat.transports._base.websockets.serve", fake_serve)
    transport = WebSocketTransport(WebSocketTransportConfig())

    first = asyncio.create_task(transport.connect())
    await first_serve_started.wait()
    second = asyncio.create_task(transport.connect())
    await asyncio.sleep(0)

    assert len(listeners) == 1

    release_first_serve.set()
    await asyncio.gather(first, second)
    await transport.disconnect()

    assert len(listeners) == 1
    assert listeners[0].close_calls == 1


@pytest.mark.asyncio
async def test_server_disconnect_retains_failed_client_close_for_retry() -> None:
    transport = WebSocketTransport(WebSocketTransportConfig())
    client = _FailOnceClosingWebSocket()
    transport._connected = True
    transport._ws = client  # type: ignore[assignment]
    transport._server = _ClosedServer()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="socket close failed"):
        await transport.disconnect()

    assert transport._pending_client_close is client
    assert transport._ws is client
    with pytest.raises(RuntimeError, match="client cleanup is incomplete"):
        await transport.connect()

    await transport.disconnect()

    assert client.close_calls == 2
    assert transport._pending_client_close is None
    assert transport._ws is None


@pytest.mark.asyncio
async def test_server_disconnect_treats_internal_client_cancel_as_retryable_failure() -> None:
    transport = WebSocketTransport(WebSocketTransportConfig())
    client = _CancelOnceClosingWebSocket()
    transport._connected = True
    transport._ws = client  # type: ignore[assignment]
    transport._server = _ClosedServer()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="client close was interrupted"):
        await transport.disconnect()

    caller = asyncio.current_task()
    assert caller is not None
    assert caller.cancelling() == 0
    assert transport._pending_client_close is client
    assert transport._ws is client
    with pytest.raises(RuntimeError, match="client cleanup is incomplete"):
        await transport.connect()

    await transport.disconnect()

    assert client.close_calls == 2
    assert transport._pending_client_close is None
    assert transport._ws is None
    assert transport._disconnect_cleanup_error is None


@pytest.mark.asyncio
async def test_server_disconnect_preserves_client_close_caller_cancel_for_retry() -> None:
    transport = WebSocketTransport(WebSocketTransportConfig())
    client = _BlockingFirstCloseWebSocket()
    transport._connected = True
    transport._ws = client  # type: ignore[assignment]
    transport._server = _ClosedServer()  # type: ignore[assignment]

    disconnecting = asyncio.create_task(transport.disconnect())
    await client.close_started.wait()
    disconnecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnecting

    assert transport._pending_client_close is client
    assert transport._ws is client

    await transport.disconnect()

    assert client.close_calls == 2
    assert transport._pending_client_close is None
    assert transport._ws is None


@pytest.mark.asyncio
async def test_interrupted_server_wait_blocks_connect_and_retries_exact_cleanup() -> None:
    class _InterruptibleCloseServer:
        def __init__(self) -> None:
            self.close_calls = 0
            self.wait_started = asyncio.Event()
            self.release_wait = asyncio.Event()

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            self.wait_started.set()
            await self.release_wait.wait()

    transport = WebSocketTransport(WebSocketTransportConfig())
    root = RuntimeScope.create_root(
        name="session",
        root_id="test-root:websocket-listener-close",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    transport.set_runtime_scope(root, name="transport-runtime")
    client = _FailOnceClosingWebSocket()
    server = _InterruptibleCloseServer()
    transport._connected = True
    transport._ws = client  # type: ignore[assignment]
    transport._server = server  # type: ignore[assignment]

    disconnecting = asyncio.create_task(transport.disconnect())
    await server.wait_started.wait()
    disconnecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnecting

    assert transport.is_connected is False
    assert transport._disconnect_cleanup_pending is True
    assert transport._pending_client_close is client
    assert transport._server is server
    assert transport._server_wait_task is not None
    assert root.tasks("transport_listener_close") == (transport._server_wait_task,)
    assert "transport-listener" in root.cohorts(force=False)
    signal = root.signal_cohort("transport-listener", force=True)
    assert transport._server_wait_task.cancelling() == 0
    with pytest.raises(RuntimeError, match="client cleanup is incomplete"):
        await transport.connect()

    server.release_wait.set()
    await transport.disconnect()

    assert client.close_calls == 2
    assert server.close_calls == 1
    assert transport._pending_client_close is None
    assert transport._server is None
    assert transport._server_wait_task is None
    assert not root.tasks("transport_listener_close")
    assert transport._disconnect_emit_cleanup_task is None
    assert transport._disconnect_cleanup_pending is False
    assert transport._disconnect_cleanup_error is None
    await root.drain_cohort(signal)


@pytest.mark.asyncio
async def test_internal_server_wait_cancel_ignores_preexisting_caller_cancel_count() -> None:
    class _CancelOnceWaitServer:
        def __init__(self) -> None:
            self.close_calls = 0
            self.wait_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise asyncio.CancelledError

    transport = WebSocketTransport(WebSocketTransportConfig())
    server = _CancelOnceWaitServer()
    transport._connected = True
    transport._server = server  # type: ignore[assignment]

    async def disconnect_after_caught_cancel() -> int:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        with pytest.raises(RuntimeError, match="server close was interrupted"):
            await transport.disconnect()
        return caller.cancelling()

    cancellation_requests = await asyncio.create_task(disconnect_after_caught_cancel())

    assert cancellation_requests == 1
    assert transport._server is server
    assert transport._server_wait_task is None
    assert transport._listener_tasks.scope is None
    assert transport._disconnect_cleanup_pending is True
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await transport.connect()

    await transport.disconnect()

    assert server.close_calls == 2
    assert server.wait_calls == 2
    assert transport._server is None
    assert transport._disconnect_cleanup_pending is False
    assert transport._disconnect_cleanup_error is None


@pytest.mark.asyncio
async def test_interrupted_diagnostic_drain_is_retained_for_disconnect_retry() -> None:
    transport = WebSocketTransport(WebSocketTransportConfig())
    root = RuntimeScope.create_root(
        name="session",
        root_id="test-root:websocket-diagnostic-cleanup",
        supervisor=RuntimeSupervisor(capacity=2),
        survivor_capacity=2,
    )
    transport.set_runtime_scope(root, name="transport-runtime")
    transport._connected = True
    transport._server = _ClosedServer()  # type: ignore[assignment]
    emit_started = asyncio.Event()
    drain_started = asyncio.Event()
    release_emit = asyncio.Event()
    original_drain = transport._drain_emit_tasks

    async def tracked_drain() -> None:
        drain_started.set()
        await original_drain()

    transport._drain_emit_tasks = tracked_drain  # type: ignore[method-assign]

    async def pending_emit() -> None:
        emit_started.set()
        await release_emit.wait()

    emit_task = asyncio.create_task(pending_emit())
    transport._track_emit_task(emit_task)

    disconnecting = asyncio.create_task(transport.disconnect())
    await emit_started.wait()
    await drain_started.wait()
    disconnecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnecting

    retained_cleanup = transport._disconnect_emit_cleanup_task
    assert transport.is_connected is False
    assert transport._disconnect_cleanup_pending is True
    assert retained_cleanup is not None
    assert retained_cleanup.done() is False
    assert root.tasks("transport_diagnostic_cleanup") == (retained_cleanup,)
    assert "transport-events" in root.cohorts(force=False)
    signal = root.signal_cohort("transport-events", force=True)
    assert retained_cleanup.cancelling() == 0
    assert emit_task.cancelled() is False
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await transport.connect()

    release_emit.set()
    await transport.disconnect()

    assert emit_task.done()
    assert transport._emit_tasks == set()
    assert transport._disconnect_emit_cleanup_task is None
    assert not root.tasks("transport_diagnostic_cleanup")
    assert transport._disconnect_cleanup_pending is False
    assert transport._disconnect_cleanup_error is None
    await root.drain_cohort(signal)


@pytest.mark.asyncio
async def test_internal_diagnostic_cancel_ignores_preexisting_caller_cancel_count() -> None:
    transport = WebSocketTransport(WebSocketTransportConfig())
    transport._connected = True
    transport._server = _ClosedServer()  # type: ignore[assignment]
    original_drain = transport._drain_emit_tasks
    drain_calls = 0

    async def cancel_once_then_drain() -> None:
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            raise asyncio.CancelledError
        await original_drain()

    transport._drain_emit_tasks = cancel_once_then_drain  # type: ignore[method-assign]

    async def disconnect_after_caught_cancel() -> int:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        with pytest.raises(RuntimeError, match="diagnostic cleanup was interrupted"):
            await transport.disconnect()
        return caller.cancelling()

    cancellation_requests = await asyncio.create_task(disconnect_after_caught_cancel())

    assert cancellation_requests == 1
    assert transport._disconnect_emit_cleanup_task is None
    assert transport._disconnect_cleanup_pending is True
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await transport.connect()

    await transport.disconnect()

    assert drain_calls == 2
    assert transport._disconnect_emit_cleanup_task is None
    assert transport._diagnostic_cleanup_tasks.scope is None
    assert transport._disconnect_cleanup_pending is False
    assert transport._disconnect_cleanup_error is None


@pytest.mark.asyncio
async def test_disconnect_queued_behind_connect_closes_late_browser_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_started = asyncio.Event()
    release_serve = asyncio.Event()

    class FakeServer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def fake_serve(*_args: object, **_kwargs: object) -> FakeServer:
        serve_started.set()
        await release_serve.wait()
        return FakeServer()

    monkeypatch.setattr("easycat.transports._base.websockets.serve", fake_serve)
    transport = WebSocketTransport(WebSocketTransportConfig())
    transport.set_event_bus(EventBus())

    connecting = asyncio.create_task(transport.connect())
    await serve_started.wait()
    disconnecting = asyncio.create_task(transport.disconnect())
    await asyncio.sleep(0)

    release_serve.set()
    await asyncio.gather(connecting, disconnecting)

    assert transport.is_connected is False
    assert transport._browser_event_forwarder is None


@pytest.mark.integration_socket
class TestWebSocketTransport(_UsesPytestTcpPortFactory):
    """Tests for WebSocketTransport with a real test client."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)

        await transport.connect()
        assert transport.is_connected
        assert not transport.has_client

        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_default_host_accepts_loopback_client(self):
        port = self._unused_port()
        config = WebSocketTransportConfig(port=port)
        transport = WebSocketTransport(config)

        await transport.connect()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                ready = await ws.recv()
                assert json.loads(ready)["type"] == "ready"
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_server_rejects_message_over_wire_size_limit(self):
        port = self._unused_port()
        transport = WebSocketTransport(WebSocketTransportConfig(host="127.0.0.1", port=port))
        await transport.connect()

        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # ready
                await ws.send(bytes(MAX_WEBSOCKET_MESSAGE_BYTES + 1))
                with pytest.raises(websockets.exceptions.ConnectionClosedError) as exc_info:
                    await ws.recv()
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1009
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_receive_audio(self):
        """Client sends audio, server yields it via receive_audio."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        received_chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                received_chunks.append(chunk)
                if len(received_chunks) >= 3:
                    break

        collect_task = asyncio.create_task(collect())

        # Connect a test client and send binary frames.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # Should receive ready message.
            ready = await ws.recv()
            assert json.loads(ready)["type"] == "ready"

            # Send 3 audio frames.
            for _ in range(3):
                await ws.send(bytes(320))

            await asyncio.wait_for(collect_task, timeout=2.0)

        await transport.disconnect()
        assert len(received_chunks) == 3
        assert all(len(c.data) == 320 for c in received_chunks)

    @pytest.mark.asyncio
    async def test_server_sends_audio_to_client(self):
        """Server sends audio chunk, client receives binary frame."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # Consume ready message.
            await ws.recv()
            await asyncio.sleep(0.05)

            # Send audio from server to client.
            chunk = _make_chunk(640)
            await transport.send_audio(chunk)
            fmt_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)  # audio_format
            assert json.loads(fmt_msg)["type"] == "audio_format"
            data = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert isinstance(data, bytes)
            assert len(data) == 640

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_clear_audio_sends_client_playback_reset(self):
        port = self._unused_port()
        transport = WebSocketTransport(WebSocketTransportConfig(host="127.0.0.1", port=port))
        await transport.connect()

        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # ready
                await asyncio.wait_for(transport.wait_for_client(), timeout=2.0)

                await transport.clear_audio()

                message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                assert json.loads(message) == {"type": "clear"}
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_server_forwards_session_events_as_json_text_frames(self):
        """Session events reach the browser as JSON control messages."""
        from easycat.events import STTFinal

        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        bus = EventBus()
        transport._event_bus = bus  # Session attaches the bus pre-connect.
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await asyncio.sleep(0.05)

            await bus.emit(STTFinal(text="hello there", turn_id="t1"))
            message = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert json.loads(message) == {
                "type": "stt_final",
                "text": "hello there",
                "turn_id": "t1",
            }

        await transport.disconnect()

        # Teardown unsubscribes the forwarder; later emits must not raise.
        await bus.emit(STTFinal(text="late", turn_id="t2"))

    @pytest.mark.asyncio
    async def test_control_message_config(self):
        """Client can send a config control message to negotiate format."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(json.dumps({"type": "config", "sample_rate": 24000}))
            await asyncio.sleep(0.1)
            assert transport._audio_format.sample_rate == 24000

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_invalid_sample_rate_config_is_ignored(self):
        """Invalid config messages must not poison the negotiated audio format."""
        transport = WebSocketTransport()

        for sample_rate in (True, False, 0, 1, 7999, -16000, 384001, 16000.0, "16000", None):
            transport._handle_control_message(
                json.dumps({"type": "config", "sample_rate": sample_rate})
            )
            assert transport._audio_format.sample_rate == 16000

        transport._handle_control_message(json.dumps({"type": "config", "sample_rate": 44100}))
        assert transport._audio_format.sample_rate == 44100

    @pytest.mark.asyncio
    async def test_client_disconnect_signals_end(self):
        """When client disconnects, receive_audio iterator should end."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        received: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                received.append(chunk)

        collect_task = asyncio.create_task(collect())

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()
            await ws.send(bytes(320))
            await asyncio.sleep(0.05)

        # Client disconnected; collect should finish.
        await asyncio.wait_for(collect_task, timeout=2.0)
        assert len(received) == 1

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_audio_format_resets_after_client_disconnect(self):
        """Negotiated audio format resets to default when client disconnects."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        # First client negotiates 24kHz.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(json.dumps({"type": "config", "sample_rate": 24000}))
            await asyncio.sleep(0.1)
            assert transport._audio_format.sample_rate == 24000

        # Client disconnected — format should reset to 16kHz default.
        await asyncio.sleep(0.1)
        assert transport._audio_format.sample_rate == 16000

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_rejects_second_client(self):
        """Only one client at a time is allowed."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws1:
            await ws1.recv()  # ready

            # Second client should be rejected.
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
                try:
                    await asyncio.wait_for(ws2.recv(), timeout=1.0)
                except websockets.exceptions.ConnectionClosed:
                    pass  # Expected — server closes with 4000.

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_wait_for_client_waits_for_new_connection_after_disconnect(self):
        """wait_for_client should not stay set after a client disconnects."""
        port = self._unused_port()
        config = WebSocketTransportConfig(host="127.0.0.1", port=port)
        transport = WebSocketTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await asyncio.sleep(0.05)
        assert not transport.has_client

        with pytest.raises(asyncio.TimeoutError):
            await transport.wait_for_client(timeout=0.1)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
            await ws2.recv()  # ready
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await transport.disconnect()
