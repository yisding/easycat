"""Run the shared lifecycle scenarios against WebTransport's two lifecycle layers."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

import easycat.transports.webtransport as webtransport_mod
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import EventBus, TransportDegraded
from easycat.testing._transport_lifecycle import (
    ConnectLeadershipObservation,
    DegradedEmissionObservation,
    DisconnectDuringConnectObservation,
    InterruptedDisconnectObservation,
    LateFrameObservation,
    MidStreamTeardownObservation,
    NormalizedDegradedEvent,
    NormalizedTransportLifecycleState,
    QueueOverflowObservation,
    StartupRollbackObservation,
    TransportLifecycleScenarioSuite,
)
from easycat.transports.webtransport import (
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransport,
    WebTransportTransportConfig,
)
from tests.transports._webtransport_helpers import _FakeH3, _FakeQuicProtocol


def _webtransport_chunk(payload: bytes = b"aa") -> AudioChunk:
    return AudioChunk(data=payload, format=PCM16_MONO_16K)


def _connection(
    config: WebTransportTransportConfig | None = None,
) -> tuple[WebTransportConnectionTransport, _FakeQuicProtocol]:
    protocol = _FakeQuicProtocol()
    transport = WebTransportConnectionTransport(
        config=config,
        _h3=_FakeH3(),  # type: ignore[arg-type]
        _quic_protocol=protocol,  # type: ignore[arg-type]
        _session_id=0,
    )
    return transport, protocol


class _ControlledServer:
    """Deterministic internal server used by the outer transport rows."""

    def __init__(self, backend: _ControlledServerBackend) -> None:
        self.backend = backend
        self._cleanup_error: Exception | None = None
        self._server: object | None = None
        self._started = False
        self.stop_calls = 0

    async def start(self) -> None:
        self.backend.start_calls += 1
        self.backend.start_entered.set()
        await self.backend.release_start.wait()
        self._server = object()
        self._started = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._started = False
        self._server = None


class _ControlledServerBackend:
    def __init__(self) -> None:
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.start_calls = 0
        self.instances: list[_ControlledServer] = []

    def create(self, *_args: object, **_kwargs: object) -> _ControlledServer:
        server = _ControlledServer(self)
        self.instances.append(server)
        return server

    @property
    def close_calls(self) -> int:
        return sum(server.stop_calls for server in self.instances)


class _BoundServer:
    def __init__(self) -> None:
        self.close_calls = 0
        self.wait_closed_entered = asyncio.Event()
        self.release_wait_closed = asyncio.Event()

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        self.wait_closed_entered.set()
        await self.release_wait_closed.wait()


class _WebTransportLifecycleDriver:
    def __init__(self) -> None:
        self.connections: list[WebTransportConnectionTransport] = []
        self.outers: list[WebTransportTransport] = []
        self.servers: list[object] = []
        self.backend_start_calls = 0
        self.backend_close_calls = 0
        self.publications: list[str | None] = []

    def _clear_tracking(self) -> None:
        self.connections = []
        self.outers = []
        self.servers = []
        self.backend_start_calls = 0
        self.backend_close_calls = 0
        self.publications = []

    def _new_connection(
        self,
        config: WebTransportTransportConfig | None = None,
    ) -> tuple[WebTransportConnectionTransport, _FakeQuicProtocol]:
        transport, protocol = _connection(config)
        self.connections.append(transport)
        return transport, protocol

    def _new_outer(self) -> WebTransportTransport:
        transport = WebTransportTransport(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        )
        self.outers.append(transport)
        return transport

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation:
        self._clear_tracking()
        transport = self._new_outer()
        backend = _ControlledServerBackend()
        with patch.object(webtransport_mod, "WebTransportServer", backend.create):
            first = asyncio.create_task(transport.connect())
            await backend.start_entered.wait()
            second = asyncio.create_task(transport.connect())
            await asyncio.sleep(0)
            backend.release_start.set()
            await asyncio.gather(first, second)
            generation = "quic-server-1"
            self.publications.append(generation)
            await transport.disconnect()
        self.publications.append(None)
        self.servers.extend(backend.instances)
        self.backend_start_calls = backend.start_calls
        self.backend_close_calls = backend.close_calls
        return ConnectLeadershipObservation(
            backend_start_calls=self.backend_start_calls,
            caller_generations=(generation, generation),
            connected_publications=(generation,),
        )

    async def observe_degraded_emission(self) -> DegradedEmissionObservation:
        self._clear_tracking()
        transport, _protocol = self._new_connection(
            WebTransportTransportConfig(outbound_max_pending=1)
        )
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        transport.set_event_bus(bus)
        transport._connected = True
        transport._out_queue.put_nowait(_webtransport_chunk(b"first"))
        assert await transport.send_audio(_webtransport_chunk()) is False
        await transport._drain_emit_tasks()
        transport._out_queue.get_nowait()
        transport._connected = False
        return DegradedEmissionObservation(
            events=tuple(
                NormalizedDegradedEvent(
                    provider=event.provider,
                    reason=event.reason,
                    detail=event.detail,
                    fatal=event.fatal,
                )
                for event in events
            )
        )

    async def observe_disconnect_during_connect(
        self,
    ) -> DisconnectDuringConnectObservation:
        self._clear_tracking()
        transport = self._new_outer()
        backend = _ControlledServerBackend()
        with patch.object(webtransport_mod, "WebTransportServer", backend.create):
            connecting = asyncio.create_task(transport.connect())
            await backend.start_entered.wait()
            disconnecting = asyncio.create_task(transport.disconnect())
            await asyncio.sleep(0)
            backend.release_start.set()
            await asyncio.gather(connecting, disconnecting)
        generation = "quic-server-1"
        self.publications.extend((generation, None))
        self.servers.extend(backend.instances)
        self.backend_close_calls = backend.close_calls
        return DisconnectDuringConnectObservation(
            connect_cancelled=connecting.cancelled(),
            backend_close_calls=self.backend_close_calls,
            connected_publications=(generation,),
        )

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation:
        self._clear_tracking()
        transport, protocol = self._new_connection()
        session = transport._session
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
        await transport.connect()
        self.publications.append("session-0")
        disconnecting = asyncio.create_task(transport.disconnect())
        await child_cancelled.wait()
        disconnecting.cancel()
        release_child.set()
        caller_cancelled = False
        try:
            await disconnecting
        except asyncio.CancelledError:
            caller_cancelled = True
        self.publications.append(None)
        connected_during_cleanup = transport.is_connected
        retained_cleanup = int(
            transport._disconnect_cleanup_error is not None
            and transport._session_stop_pending
            and transport._connection_close_pending
        )
        await transport.disconnect()
        self.backend_close_calls = len(protocol.close_calls)
        return InterruptedDisconnectObservation(
            caller_cancelled=caller_cancelled,
            connected_during_retained_cleanup=connected_during_cleanup,
            retained_cleanup_during_cancel=retained_cleanup,
            backend_close_calls=self.backend_close_calls,
            lifecycle_publications=tuple(self.publications),
        )

    async def observe_late_frames(self) -> LateFrameObservation:
        self._clear_tracking()
        delivered: list[str] = []
        handler_called = asyncio.Event()

        async def handler(_transport: WebTransportConnectionTransport) -> None:
            delivered.append("fresh")
            handler_called.set()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            handler,
        )
        self.servers.append(server)
        server._started = True
        server._accepting_sessions = True
        active, _active_protocol = self._new_connection()
        active_accepted = server._can_accept_session()
        server._dispatch_session(active)
        await handler_called.wait()
        handler_tasks = tuple(server._handler_tasks)
        if handler_tasks:
            await asyncio.gather(*handler_tasks)
        await asyncio.sleep(0)

        bound = _BoundServer()
        server._server = bound  # type: ignore[assignment]
        stopping = asyncio.create_task(server.stop())
        await bound.wait_closed_entered.wait()
        stale, stale_protocol = self._new_connection()
        stale_accepted = server._can_accept_session()
        server._dispatch_session(stale)
        bound.release_wait_closed.set()
        await stopping
        assert stale_protocol.close_calls == [(0, "server not accepting sessions")]
        self.backend_close_calls = bound.close_calls
        return LateFrameObservation(
            stale_generation="server-stopping",
            active_generation="server-active",
            stale_accepted=stale_accepted,
            active_accepted=active_accepted,
            delivered_frames=tuple(delivered),
        )

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation:
        self._clear_tracking()
        outer = self._new_outer()
        inner, protocol = self._new_connection()
        await inner.connect()
        outer._active = inner
        outer._connected = True
        outer._client_connected.set()
        receiver_terminated = asyncio.Event()

        async def consume() -> None:
            async for _ in outer.receive_audio():
                pass
            receiver_terminated.set()

        receiver = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await inner.disconnect()
        await receiver
        outer._active = None
        outer._connected = False
        outer._client_connected.clear()
        self.backend_close_calls = len(protocol.close_calls)
        return MidStreamTeardownObservation(
            receiver_terminated=receiver_terminated.is_set(),
            backend_close_calls=self.backend_close_calls,
            owned_work_after_disconnect=self.normalized_state().owned_work,
        )

    async def observe_queue_overflow(self) -> QueueOverflowObservation:
        self._clear_tracking()
        transport, _protocol = self._new_connection(
            WebTransportTransportConfig(max_pending_chunks=1)
        )
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        transport.set_event_bus(bus)
        transport._connected = True
        before_first = transport._in_queue.qsize()
        transport._enqueue_chunk(_webtransport_chunk(b"first"), context="WebTransport")
        first_accepted = transport._in_queue.qsize() > before_first
        before_second = transport._in_queue.qsize()
        transport._enqueue_chunk(_webtransport_chunk(b"overflow"), context="WebTransport")
        second_accepted = transport._in_queue.qsize() > before_second
        await transport._drain_emit_tasks()
        transport._in_queue.get_nowait()
        transport._connected = False
        return QueueOverflowObservation(
            accepted=(first_accepted, second_accepted),
            dropped_frames=len(events),
            degraded_reasons=tuple(event.reason for event in events),
        )

    async def observe_startup_rollback(self) -> StartupRollbackObservation:
        self._clear_tracking()
        transport, protocol = self._new_connection()
        session = transport._session
        assert session is not None
        original_start = session.start
        start_calls = 0

        async def fail_first_start() -> None:
            nonlocal start_calls
            start_calls += 1
            if start_calls == 1:
                raise RuntimeError("controlled startup failure")
            await original_start()

        session.start = fail_first_start  # type: ignore[method-assign]
        startup_error = ""
        try:
            await transport.connect()
        except RuntimeError as exc:
            startup_error = str(exc)
        writer = session._writer_task
        live_resources = int(writer is not None and not writer.done()) + int(
            transport._session_stop_pending or transport._connection_close_pending
        )
        connected_after_failure = transport.is_connected
        await transport.connect()
        retry_generation = f"session-{start_calls}"
        await transport.disconnect()
        self.backend_start_calls = start_calls
        self.backend_close_calls = len(protocol.close_calls)
        return StartupRollbackObservation(
            startup_error=startup_error,
            live_resources_after_failure=live_resources,
            connected_after_failure=connected_after_failure,
            retry_generation=retry_generation,
            backend_start_calls=self.backend_start_calls,
            backend_close_calls=self.backend_close_calls,
        )

    def normalized_state(self) -> NormalizedTransportLifecycleState:
        connected_connections = [
            transport for transport in self.connections if transport.is_connected
        ]
        connected_outers = [transport for transport in self.outers if transport.is_connected]
        owned_work = 0
        queued_frames = 0
        retained_cleanup = 0
        for connection in self.connections:
            session = connection._session
            writer = None if session is None else session._writer_task
            owned_work += int(writer is not None and not writer.done())
            owned_work += sum(not task.done() for task in connection._emit_tasks)
            if connection.is_connected:
                queued_frames += connection._in_queue.qsize() + connection._out_queue.qsize()
            retained_cleanup += int(
                connection._disconnect_cleanup_error is not None
                or connection._session_stop_pending
                or connection._connection_close_pending
            )
        for outer in self.outers:
            owned_work += sum(not task.done() for task in outer._emit_tasks)
            if outer.is_connected:
                queued_frames += outer._in_queue.qsize()
            retained_cleanup += int(not outer.is_connected and outer._server is not None)
        for server in self.servers:
            handler_tasks = getattr(server, "_handler_tasks", ())
            owned_work += sum(not task.done() for task in handler_tasks)
            wait_task = getattr(server, "_server_wait_closed_task", None)
            owned_work += int(wait_task is not None and not wait_task.done())
            retained_cleanup += int(getattr(server, "_cleanup_error", None) is not None)
        connected = bool(connected_connections or connected_outers)
        return NormalizedTransportLifecycleState(
            connected=connected,
            active_generation="webtransport-active" if connected else None,
            owned_work=owned_work,
            queued_frames=queued_frames,
            retained_cleanup=retained_cleanup,
        )

    def snapshot_state(self) -> dict[str, object]:
        state = self.normalized_state()
        return {
            "connected": state.connected,
            "active_generation": state.active_generation,
            "owned_work": state.owned_work,
            "queued_frames": state.queued_frames,
            "retained_cleanup": state.retained_cleanup,
            "connection_count": len(self.connections),
            "outer_count": len(self.outers),
            "server_count": len(self.servers),
            "backend_start_calls": self.backend_start_calls,
            "backend_close_calls": self.backend_close_calls,
            "lifecycle_publications": self.publications,
        }

    def reset(self) -> None:
        assert self.normalized_state() == NormalizedTransportLifecycleState()
        self._clear_tracking()


class TestWebTransportLifecycleScenarios(TransportLifecycleScenarioSuite):
    pytestmark: ClassVar[list[Any]] = [
        pytest.mark.contract,
        pytest.mark.surface_transport,
        pytest.mark.provider("webtransport"),
    ]
    driver_factory = _WebTransportLifecycleDriver
    expected_degraded_event = NormalizedDegradedEvent(
        provider="webtransport",
        reason="outbound_queue_full",
        detail="dropped 2-byte TTS frame; outbound queue full",
        fatal=False,
    )
    expected_disconnect_during_connect = (False, 1)
    expected_interrupted_disconnect_state = (False, 1)
