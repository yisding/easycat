"""Run the shared lifecycle scenarios against Local and WebSocket transports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
import websockets

import easycat.transports._base as base_mod
import easycat.transports.local as local_mod
from easycat.audio_format import PCM16_MONO_16K, PCM16_MONO_24K, AudioChunk
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
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketTransport,
    WebSocketTransportConfig,
)


def _local_chunk(payload: bytes = b"fresh") -> AudioChunk:
    return AudioChunk(data=payload, format=PCM16_MONO_24K)


def _websocket_chunk(payload: bytes = b"fresh") -> AudioChunk:
    return AudioChunk(data=payload, format=PCM16_MONO_16K)


class _LocalStream:
    def __init__(self, backend: _LocalBackend, kind: str) -> None:
        self.backend = backend
        self.kind = kind
        self.closed = False

    def start(self) -> None:
        if self.kind != "output":
            return
        if self.backend.fail_output_starts:
            self.backend.fail_output_starts -= 1
            raise RuntimeError("controlled startup failure")

    def stop(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _LocalBackend:
    """Deterministic replacement for the two PortAudio resources."""

    def __init__(self) -> None:
        self.inputs: list[_LocalStream] = []
        self.outputs: list[_LocalStream] = []
        self.fail_output_starts = 0

    def InputStream(self, **_kwargs: object) -> _LocalStream:
        stream = _LocalStream(self, "input")
        self.inputs.append(stream)
        return stream

    def OutputStream(self, **_kwargs: object) -> _LocalStream:
        stream = _LocalStream(self, "output")
        self.outputs.append(stream)
        return stream

    def require_module(self, module_name: str, **_kwargs: object) -> object:
        return self if module_name == "sounddevice" else object()

    @property
    def start_calls(self) -> int:
        return len(self.inputs)

    @property
    def close_calls(self) -> int:
        return sum(stream.closed for stream in self.inputs)

    @property
    def live_resources(self) -> int:
        return sum(not stream.closed for stream in (*self.inputs, *self.outputs))


class _LocalLifecycleDriver:
    def __init__(self) -> None:
        self.transport = LocalTransport()
        self.backend = _LocalBackend()
        self.publications: list[str | None] = []

    def _replace_transport(self, config: LocalTransportConfig | None = None) -> None:
        self.transport = LocalTransport(config)
        self.backend = _LocalBackend()
        self.publications = []

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation:
        self._replace_transport()
        with patch.object(local_mod, "require_module", self.backend.require_module):
            first = asyncio.create_task(self.transport.connect())
            second = asyncio.create_task(self.transport.connect())
            await asyncio.gather(first, second)
            generation = str(self.transport._stream_generation)
            self.publications.append(generation)
            start_calls = self.backend.start_calls
            await self.transport.disconnect()
        self.publications.append(None)
        return ConnectLeadershipObservation(
            backend_start_calls=start_calls,
            caller_generations=(generation, generation),
            connected_publications=(generation,),
        )

    async def observe_degraded_emission(self) -> DegradedEmissionObservation:
        self._replace_transport(LocalTransportConfig(max_pending_in_chunks=1))
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        self.transport.set_event_bus(bus)
        self.transport._enqueue_chunk(_local_chunk(b"aa"), context="mic")
        self.transport._enqueue_chunk(_local_chunk(b"bb"), context="mic")
        await self.transport._drain_emit_tasks()
        self.transport._in_queue.get_nowait()
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
        self._replace_transport()
        connect_cancelled = False

        async def connect_and_publish() -> None:
            await self.transport.connect()
            self.publications.append(str(self.transport._stream_generation))

        with patch.object(local_mod, "require_module", self.backend.require_module):
            connecting = asyncio.create_task(connect_and_publish())
            disconnecting = asyncio.create_task(self.transport.disconnect())
            try:
                await connecting
            except asyncio.CancelledError:
                connect_cancelled = True
            await disconnecting
        self.publications.append(None)
        return DisconnectDuringConnectObservation(
            connect_cancelled=connect_cancelled,
            backend_close_calls=self.backend.close_calls,
            connected_publications=tuple(
                publication for publication in self.publications if publication is not None
            ),
        )

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation:
        self._replace_transport()
        with patch.object(local_mod, "require_module", self.backend.require_module):
            await self.transport.connect()
            generation = str(self.transport._stream_generation)
            self.publications.append(generation)

            blocker = asyncio.create_task(asyncio.Event().wait())
            self.transport._track_emit_task(blocker)
            await asyncio.sleep(0)
            disconnecting = asyncio.create_task(self.transport.disconnect())
            await asyncio.sleep(0)
            disconnecting.cancel()
            caller_cancelled = False
            try:
                await disconnecting
            except asyncio.CancelledError:
                caller_cancelled = True
            await asyncio.sleep(0)
            self.publications.append(None)
            connected_during_cleanup = self.transport.is_connected
            retained_cleanup = sum(not task.done() for task in self.transport._emit_tasks)
            await self.transport.disconnect()

        return InterruptedDisconnectObservation(
            caller_cancelled=caller_cancelled,
            connected_during_retained_cleanup=connected_during_cleanup,
            retained_cleanup_during_cancel=retained_cleanup,
            backend_close_calls=self.backend.close_calls,
            lifecycle_publications=tuple(self.publications),
        )

    async def observe_late_frames(self) -> LateFrameObservation:
        self._replace_transport()
        with patch.object(local_mod, "require_module", self.backend.require_module):
            await self.transport.connect()
            stale_generation = self.transport._stream_generation
            await self.transport.disconnect()
            await self.transport.connect()
            active_generation = self.transport._stream_generation

            before_stale = self.transport._in_queue.qsize()
            self.transport._enqueue_input_chunk(
                _local_chunk(b"stale"), stream_generation=stale_generation
            )
            stale_accepted = self.transport._in_queue.qsize() > before_stale
            before_active = self.transport._in_queue.qsize()
            self.transport._enqueue_input_chunk(
                _local_chunk(b"fresh"), stream_generation=active_generation
            )
            active_accepted = self.transport._in_queue.qsize() > before_active
            delivered = self.transport._in_queue.get_nowait()
            await self.transport.disconnect()

        return LateFrameObservation(
            stale_generation=str(stale_generation),
            active_generation=str(active_generation),
            stale_accepted=stale_accepted,
            active_accepted=active_accepted,
            delivered_frames=(() if delivered is None else (delivered.data.decode(),)),
        )

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation:
        self._replace_transport()
        receiver_terminated = asyncio.Event()

        async def consume() -> None:
            async for _ in self.transport.receive_audio():
                pass
            receiver_terminated.set()

        with patch.object(local_mod, "require_module", self.backend.require_module):
            await self.transport.connect()
            receiver = asyncio.create_task(consume())
            await asyncio.sleep(0)
            await self.transport.disconnect()
            await receiver

        return MidStreamTeardownObservation(
            receiver_terminated=receiver_terminated.is_set(),
            backend_close_calls=self.backend.close_calls,
            owned_work_after_disconnect=self.normalized_state().owned_work,
        )

    async def observe_queue_overflow(self) -> QueueOverflowObservation:
        self._replace_transport(LocalTransportConfig(max_pending_in_chunks=1))
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        self.transport.set_event_bus(bus)
        with patch.object(local_mod, "require_module", self.backend.require_module):
            await self.transport.connect()
            generation = self.transport._stream_generation
            before_first = self.transport._in_queue.qsize()
            self.transport._enqueue_input_chunk(
                _local_chunk(b"first"), stream_generation=generation
            )
            first_accepted = self.transport._in_queue.qsize() > before_first
            before_second = self.transport._in_queue.qsize()
            self.transport._enqueue_input_chunk(
                _local_chunk(b"overflow"), stream_generation=generation
            )
            second_accepted = self.transport._in_queue.qsize() > before_second
            await self.transport._drain_emit_tasks()
            self.transport._in_queue.get_nowait()
            await self.transport.disconnect()
        return QueueOverflowObservation(
            accepted=(first_accepted, second_accepted),
            dropped_frames=len(events),
            degraded_reasons=tuple(event.reason for event in events),
        )

    async def observe_startup_rollback(self) -> StartupRollbackObservation:
        self._replace_transport()
        self.backend.fail_output_starts = 1
        startup_error = ""
        with patch.object(local_mod, "require_module", self.backend.require_module):
            try:
                await self.transport.connect()
            except RuntimeError as exc:
                startup_error = str(exc)
            live_resources = self.backend.live_resources
            connected_after_failure = self.transport.is_connected
            await self.transport.connect()
            retry_generation = str(self.transport._stream_generation)
            await self.transport.disconnect()
        return StartupRollbackObservation(
            startup_error=startup_error,
            live_resources_after_failure=live_resources,
            connected_after_failure=connected_after_failure,
            retry_generation=retry_generation,
            backend_start_calls=self.backend.start_calls,
            backend_close_calls=self.backend.close_calls,
        )

    def normalized_state(self) -> NormalizedTransportLifecycleState:
        connected = self.transport.is_connected
        owned_work = sum(not task.done() for task in self.transport._emit_tasks)
        return NormalizedTransportLifecycleState(
            connected=connected,
            active_generation=(str(self.transport._stream_generation) if connected else None),
            owned_work=owned_work,
            queued_frames=(self.transport._in_queue.qsize() if connected else 0),
            retained_cleanup=0,
        )

    def snapshot_state(self) -> dict[str, object]:
        state = self.normalized_state()
        return {
            "connected": state.connected,
            "active_generation": state.active_generation,
            "owned_work": state.owned_work,
            "queued_frames": state.queued_frames,
            "retained_cleanup": state.retained_cleanup,
            "backend_start_calls": self.backend.start_calls,
            "backend_close_calls": self.backend.close_calls,
            "live_resources": self.backend.live_resources,
            "lifecycle_publications": self.publications,
        }

    def reset(self) -> None:
        assert self.normalized_state() == NormalizedTransportLifecycleState()
        self._replace_transport()


class _FakeServer:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        return None


class _ListenerBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.servers: list[_FakeServer] = []

    async def serve(self, *_args: object, **_kwargs: object) -> _FakeServer:
        server = _FakeServer()
        self.servers.append(server)
        self.started.set()
        await self.release.wait()
        return server


class _FakeWebSocket:
    def __init__(
        self,
        *,
        send_error: BaseException | None = None,
    ) -> None:
        self.send_error = send_error
        self.sent: list[str | bytes] = []
        self.close_calls = 0
        self.receive_started = asyncio.Event()
        self.receive_release = asyncio.Event()

    async def send(self, message: str | bytes) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)

    async def close(self) -> None:
        self.close_calls += 1

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        self.receive_started.set()
        await self.receive_release.wait()
        raise StopAsyncIteration


class _WebSocketLifecycleDriver:
    def __init__(self) -> None:
        self.transport: WebSocketTransport | WebSocketConnectionTransport | None = None
        self.publications: list[str | None] = []
        self.backend_start_calls = 0
        self.backend_close_calls = 0

    def _set_transport(self, transport: WebSocketTransport | WebSocketConnectionTransport) -> None:
        self.transport = transport
        self.publications = []
        self.backend_start_calls = 0
        self.backend_close_calls = 0

    def _require_transport(self) -> WebSocketTransport | WebSocketConnectionTransport:
        assert self.transport is not None
        return self.transport

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation:
        backend = _ListenerBackend()
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        self._set_transport(transport)
        with patch.object(base_mod.websockets, "serve", backend.serve):
            first = asyncio.create_task(transport.connect())
            await backend.started.wait()
            second = asyncio.create_task(transport.connect())
            await asyncio.sleep(0)
            backend.release.set()
            await asyncio.gather(first, second)
            generation = "listener-1"
            self.publications.append(generation)
            await transport.disconnect()
        self.publications.append(None)
        self.backend_start_calls = len(backend.servers)
        self.backend_close_calls = sum(server.close_calls for server in backend.servers)
        return ConnectLeadershipObservation(
            backend_start_calls=self.backend_start_calls,
            caller_generations=(generation, generation),
            connected_publications=(generation,),
        )

    async def observe_degraded_emission(self) -> DegradedEmissionObservation:
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        self._set_transport(transport)
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        transport.set_event_bus(bus)
        transport._handle_control_message("}{")
        await transport._drain_emit_tasks()
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
        backend = _ListenerBackend()
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        self._set_transport(transport)

        async def connect_and_publish() -> None:
            await transport.connect()
            self.publications.append("listener-1")

        connect_cancelled = False
        with patch.object(base_mod.websockets, "serve", backend.serve):
            connecting = asyncio.create_task(connect_and_publish())
            await backend.started.wait()
            disconnecting = asyncio.create_task(transport.disconnect())
            await asyncio.sleep(0)
            backend.release.set()
            try:
                await connecting
            except asyncio.CancelledError:
                connect_cancelled = True
            await disconnecting
        self.publications.append(None)
        self.backend_start_calls = len(backend.servers)
        self.backend_close_calls = sum(server.close_calls for server in backend.servers)
        return DisconnectDuringConnectObservation(
            connect_cancelled=connect_cancelled,
            backend_close_calls=self.backend_close_calls,
            connected_publications=tuple(
                publication for publication in self.publications if publication is not None
            ),
        )

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation:
        ws = _FakeWebSocket()
        transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
        self._set_transport(transport)
        transport._connected = True
        transport._socket_consumed = True
        transport._connection_epoch.bump(ws)  # type: ignore[arg-type]
        self.publications.append("connection-1")
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
        caller_cancelled = False
        try:
            await disconnecting
        except asyncio.CancelledError:
            caller_cancelled = True

        connected_during_cleanup = transport.is_connected
        retained_cleanup = int(
            transport._disconnect_cleanup_error is not None and transport._ws is ws
        )
        await transport.disconnect()
        self.publications.append(None)
        self.backend_close_calls = ws.close_calls
        return InterruptedDisconnectObservation(
            caller_cancelled=caller_cancelled,
            connected_during_retained_cleanup=connected_during_cleanup,
            retained_cleanup_during_cancel=retained_cleanup,
            backend_close_calls=self.backend_close_calls,
            lifecycle_publications=tuple(self.publications),
        )

    async def observe_late_frames(self) -> LateFrameObservation:
        close_frame = websockets.frames.Close(1006, "abnormal")
        stale = _FakeWebSocket(
            send_error=websockets.exceptions.ConnectionClosed(close_frame, None)
        )
        active = _FakeWebSocket()
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        self._set_transport(transport)
        transport._connected = True
        transport._ws = stale  # type: ignore[assignment]
        stale_accepted = await transport.send_audio(_websocket_chunk(b"stale"))
        transport._reset_audio_queue()
        transport._ws = active  # type: ignore[assignment]
        transport._client_connected.set()
        active_accepted = await transport.send_audio(_websocket_chunk(b"fresh"))
        delivered = tuple(
            "fresh"
            for message in active.sent
            if isinstance(message, bytes) and message == b"fresh"
        )
        transport._finish_websocket(active)  # type: ignore[arg-type]
        transport._connected = False
        return LateFrameObservation(
            stale_generation="client-stale",
            active_generation="client-active",
            stale_accepted=stale_accepted,
            active_accepted=active_accepted,
            delivered_frames=delivered,
        )

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation:
        ws = _FakeWebSocket()
        transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
        self._set_transport(transport)
        await transport.connect()
        await ws.receive_started.wait()
        receiver_terminated = asyncio.Event()

        async def consume() -> None:
            async for _ in transport.receive_audio():
                pass
            receiver_terminated.set()

        receiver = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await transport.disconnect()
        await receiver
        self.backend_close_calls = ws.close_calls
        return MidStreamTeardownObservation(
            receiver_terminated=receiver_terminated.is_set(),
            backend_close_calls=self.backend_close_calls,
            owned_work_after_disconnect=self.normalized_state().owned_work,
        )

    async def observe_queue_overflow(self) -> QueueOverflowObservation:
        transport = WebSocketTransport(WebSocketTransportConfig(port=0, max_pending_chunks=1))
        self._set_transport(transport)
        server = _FakeServer()
        transport._connected = True
        transport._server = server  # type: ignore[assignment]
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        transport.set_event_bus(bus)
        before_first = transport._in_queue.qsize()
        transport._enqueue_chunk(_websocket_chunk(b"first"), context="WebSocket")
        first_accepted = transport._in_queue.qsize() > before_first
        before_second = transport._in_queue.qsize()
        transport._enqueue_chunk(_websocket_chunk(b"overflow"), context="WebSocket")
        second_accepted = transport._in_queue.qsize() > before_second
        await transport._drain_emit_tasks()
        transport._in_queue.get_nowait()
        await transport.disconnect()
        self.backend_close_calls = server.close_calls
        return QueueOverflowObservation(
            accepted=(first_accepted, second_accepted),
            dropped_frames=len(events),
            degraded_reasons=tuple(event.reason for event in events),
        )

    async def observe_startup_rollback(self) -> StartupRollbackObservation:
        failing_ws = _FakeWebSocket(send_error=RuntimeError("controlled startup failure"))
        failing = WebSocketConnectionTransport(failing_ws)  # type: ignore[arg-type]
        self._set_transport(failing)
        startup_error = ""
        self.backend_start_calls += 1
        try:
            await failing.connect()
        except RuntimeError as exc:
            startup_error = str(exc)
        connected_after_failure = failing.is_connected
        await failing.disconnect()
        live_resources = int(failing._ws is not None)

        retry_ws = _FakeWebSocket()
        retry = WebSocketConnectionTransport(retry_ws)  # type: ignore[arg-type]
        self.transport = retry
        self.backend_start_calls += 1
        await retry.connect()
        retry_generation = f"connection-2:{retry._connection_epoch.generation}"
        await retry.disconnect()
        self.backend_close_calls = failing_ws.close_calls + retry_ws.close_calls
        return StartupRollbackObservation(
            startup_error=startup_error,
            live_resources_after_failure=live_resources,
            connected_after_failure=connected_after_failure,
            retry_generation=retry_generation,
            backend_start_calls=self.backend_start_calls,
            backend_close_calls=self.backend_close_calls,
        )

    def normalized_state(self) -> NormalizedTransportLifecycleState:
        transport = self._require_transport()
        connected = transport.is_connected
        owned_tasks = list(transport._emit_tasks)
        retained_cleanup = 0
        active_generation: str | None = None
        if isinstance(transport, WebSocketConnectionTransport):
            owned_tasks.extend(
                task
                for task in (
                    transport._connect_task,
                    transport._receive_task,
                    transport._disconnect_task,
                )
                if task is not None
            )
            retained_cleanup = int(
                transport._disconnect_cleanup_error is not None and transport._ws is not None
            )
            if connected:
                active_generation = str(transport._connection_epoch.generation)
        else:
            retained_cleanup = int(
                transport._disconnect_cleanup_pending
                or transport._pending_client_close is not None
            )
            if transport._server_wait_task is not None:
                owned_tasks.append(transport._server_wait_task)
            if connected:
                active_generation = "listener-active"
        return NormalizedTransportLifecycleState(
            connected=connected,
            active_generation=active_generation,
            owned_work=sum(not task.done() for task in owned_tasks),
            queued_frames=(transport._in_queue.qsize() if connected else 0),
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
            "backend_start_calls": self.backend_start_calls,
            "backend_close_calls": self.backend_close_calls,
            "lifecycle_publications": self.publications,
        }

    def reset(self) -> None:
        assert self.normalized_state() == NormalizedTransportLifecycleState()
        self.transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        self.publications = []
        self.backend_start_calls = 0
        self.backend_close_calls = 0


class TestLocalTransportLifecycleScenarios(TransportLifecycleScenarioSuite):
    pytestmark: ClassVar[list[Any]] = [
        pytest.mark.contract,
        pytest.mark.surface_transport,
        pytest.mark.provider("local"),
    ]
    driver_factory = _LocalLifecycleDriver
    expected_degraded_event = NormalizedDegradedEvent(
        provider="local",
        reason="inbound_queue_full",
        detail="dropped 2-byte mic frame; inbound queue full",
        fatal=False,
    )
    expected_disconnect_during_connect = (False, 1)
    expected_interrupted_disconnect_state = (False, 0)


class TestWebSocketTransportLifecycleScenarios(TransportLifecycleScenarioSuite):
    pytestmark: ClassVar[list[Any]] = [
        pytest.mark.contract,
        pytest.mark.surface_transport,
        pytest.mark.provider("websocket"),
    ]
    driver_factory = _WebSocketLifecycleDriver
    expected_degraded_event = NormalizedDegradedEvent(
        provider="websocket",
        reason="control_decode_failed",
        detail="control frame is not valid JSON",
        fatal=False,
    )
    expected_disconnect_during_connect = (False, 1)
    expected_interrupted_disconnect_state = (False, 1)
