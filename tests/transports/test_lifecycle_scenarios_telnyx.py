"""Run the shared lifecycle scenarios against Telnyx's accepted-socket transport."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

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
from easycat.transports.telnyx_media import (
    TelnyxConnectionTransport,
    TelnyxTransportConfig,
)


def _start_message(stream_id: str, call_control_id: str) -> str:
    return json.dumps(
        {
            "event": "start",
            "start": {
                "stream_id": stream_id,
                "call_control_id": call_control_id,
                "media_format": {
                    "encoding": "L16",
                    "sample_rate": 16000,
                    "channels": 1,
                },
                "from": "+15550001111",
                "to": "+15550002222",
            },
        }
    )


def _media_message(sequence_number: str, payload: bytes) -> str:
    return json.dumps(
        {
            "event": "media",
            "media": {
                "payload": base64.b64encode(payload).decode("ascii"),
                "track": "inbound",
                "sequence_number": sequence_number,
            },
        }
    )


def _stop_message(stream_id: str) -> str:
    return json.dumps({"event": "stop", "stop": {"stream_id": stream_id}})


def _telnyx_chunk(payload: bytes = b"fresh") -> AudioChunk:
    return AudioChunk(data=payload, format=PCM16_MONO_16K)


class _FakeTelnyxWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_calls = 0
        self.receive_started = asyncio.Event()
        self.receive_release = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *_args: object) -> None:
        self.close_calls += 1
        self.receive_release.set()

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        self.receive_started.set()
        await self.receive_release.wait()
        raise StopAsyncIteration


class _ControlledStartTelnyx(TelnyxConnectionTransport):
    """Accepted-socket transport with a gated deferred-start observer."""

    def __init__(
        self,
        ws: _FakeTelnyxWebSocket,
        *,
        fail_start: bool = False,
    ) -> None:
        super().__init__(ws)  # type: ignore[arg-type]
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.fail_start = fail_start
        self.start_calls = 0
        self._pending_start_message = {"event": "start"}

    async def _accept_start(self, *_args: object, **_kwargs: object) -> bool:
        self.start_calls += 1
        self.start_entered.set()
        await self.release_start.wait()
        if self.fail_start:
            raise RuntimeError("controlled startup failure")
        self._stream_id = "STREAM-CONTROLLED"
        self._call_control_id = "CALL-CONTROLLED"
        return True


class _TelnyxLifecycleDriver:
    def __init__(self) -> None:
        self.ws = _FakeTelnyxWebSocket()
        self.transport: TelnyxConnectionTransport = TelnyxConnectionTransport(
            self.ws  # type: ignore[arg-type]
        )
        self.backend_start_calls = 0
        self.backend_close_calls = 0
        self.publications: list[str | None] = []

    def _set_transport(
        self,
        transport: TelnyxConnectionTransport,
        ws: _FakeTelnyxWebSocket,
    ) -> None:
        self.transport = transport
        self.ws = ws
        self.backend_start_calls = 0
        self.backend_close_calls = 0
        self.publications = []

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation:
        ws = _FakeTelnyxWebSocket()
        transport = _ControlledStartTelnyx(ws)
        self._set_transport(transport, ws)
        first = asyncio.create_task(transport.connect())
        await transport.start_entered.wait()
        second = asyncio.create_task(transport.connect())
        await asyncio.sleep(0)
        transport.release_start.set()
        await asyncio.gather(first, second)
        generation = str(transport._connection_epoch.generation)
        self.publications.append(generation)
        self.backend_start_calls = transport.start_calls
        await transport.disconnect()
        self.publications.append(None)
        self.backend_close_calls = ws.close_calls
        return ConnectLeadershipObservation(
            backend_start_calls=self.backend_start_calls,
            caller_generations=(generation, generation),
            connected_publications=(generation,),
        )

    async def observe_degraded_emission(self) -> DegradedEmissionObservation:
        ws = _FakeTelnyxWebSocket()
        transport = TelnyxConnectionTransport(
            ws,  # type: ignore[arg-type]
            config=TelnyxTransportConfig(max_pending_chunks=1),
        )
        self._set_transport(transport, ws)
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        transport.set_event_bus(bus)
        transport._enqueue_chunk(_telnyx_chunk(b"aa"), context="Telnyx")
        transport._enqueue_chunk(_telnyx_chunk(b"bb"), context="Telnyx")
        await transport._drain_emit_tasks()
        transport._in_queue.get_nowait()
        await transport.disconnect()
        self.backend_close_calls = ws.close_calls
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
        ws = _FakeTelnyxWebSocket()
        transport = _ControlledStartTelnyx(ws)
        self._set_transport(transport, ws)
        connecting = asyncio.create_task(transport.connect())
        await transport.start_entered.wait()
        await transport.disconnect()
        transport.release_start.set()
        connect_cancelled = False
        try:
            await connecting
        except ConnectionError:
            connect_cancelled = True
        self.backend_start_calls = transport.start_calls
        self.backend_close_calls = ws.close_calls
        return DisconnectDuringConnectObservation(
            connect_cancelled=connect_cancelled,
            backend_close_calls=self.backend_close_calls,
            connected_publications=(),
        )

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation:
        ws = _FakeTelnyxWebSocket()
        transport = TelnyxConnectionTransport(ws)  # type: ignore[arg-type]
        self._set_transport(transport, ws)
        transport._connected = True
        transport._socket_consumed = True
        transport._connection_epoch.bump(ws)  # type: ignore[arg-type]
        self.publications.append("1")
        child_cancelled = asyncio.Event()
        release_child = asyncio.Event()

        async def cancellation_resistant_receiver() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                await release_child.wait()

        transport._receive_task = asyncio.create_task(cancellation_resistant_receiver())
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
            transport._disconnect_cleanup_error is not None and transport._socket_close_pending
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
        ws = _FakeTelnyxWebSocket()
        transport = TelnyxConnectionTransport(ws)  # type: ignore[arg-type]
        self._set_transport(transport, ws)
        await transport._handle_message(_start_message("STREAM-STALE", "CALL-STALE"))
        # Telnyx media frames carry no stream id, so the staleness boundary is
        # the stop event: it ends the stream generation (queueing its terminal
        # sentinel) and late untagged media for the ended stream is dropped.
        await transport._handle_message(_stop_message("STREAM-STALE"))
        assert transport._in_queue.get_nowait() is None

        stale_before = transport._in_queue.qsize()
        await transport._handle_message(_media_message("2", bytes(320)))
        stale_accepted = transport._in_queue.qsize() > stale_before
        await transport._handle_message(_start_message("STREAM-ACTIVE", "CALL-ACTIVE"))
        active_before = transport._in_queue.qsize()
        for index in range(4):
            await transport._handle_message(_media_message(str(index + 3), bytes(320)))
        active_accepted = transport._in_queue.qsize() > active_before
        delivered = transport._in_queue.get_nowait()
        await transport.disconnect()
        self.backend_close_calls = ws.close_calls
        return LateFrameObservation(
            stale_generation="STREAM-STALE",
            active_generation="STREAM-ACTIVE",
            stale_accepted=stale_accepted,
            active_accepted=active_accepted,
            delivered_frames=(() if delivered is None else ("fresh",)),
        )

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation:
        ws = _FakeTelnyxWebSocket()
        transport = TelnyxConnectionTransport(ws)  # type: ignore[arg-type]
        self._set_transport(transport, ws)
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
        ws = _FakeTelnyxWebSocket()
        transport = TelnyxConnectionTransport(
            ws,  # type: ignore[arg-type]
            config=TelnyxTransportConfig(max_pending_chunks=1),
        )
        self._set_transport(transport, ws)
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        transport.set_event_bus(bus)
        await transport.connect()
        await ws.receive_started.wait()
        before_first = transport._in_queue.qsize()
        transport._enqueue_chunk(_telnyx_chunk(b"first"), context="Telnyx")
        first_accepted = transport._in_queue.qsize() > before_first
        before_second = transport._in_queue.qsize()
        transport._enqueue_chunk(_telnyx_chunk(b"overflow"), context="Telnyx")
        second_accepted = transport._in_queue.qsize() > before_second
        await transport._drain_emit_tasks()
        transport._in_queue.get_nowait()
        await transport.disconnect()
        self.backend_close_calls = ws.close_calls
        return QueueOverflowObservation(
            accepted=(first_accepted, second_accepted),
            dropped_frames=len(events),
            degraded_reasons=tuple(event.reason for event in events),
        )

    async def observe_startup_rollback(self) -> StartupRollbackObservation:
        failing_ws = _FakeTelnyxWebSocket()
        failing = _ControlledStartTelnyx(failing_ws, fail_start=True)
        self._set_transport(failing, failing_ws)
        failing.release_start.set()
        startup_error = ""
        try:
            await failing.connect()
        except RuntimeError as exc:
            startup_error = str(exc)
        self.backend_start_calls += failing.start_calls
        live_resources = int(failing._socket_close_pending)
        connected_after_failure = failing.is_connected

        retry_ws = _FakeTelnyxWebSocket()
        retry = _ControlledStartTelnyx(retry_ws)
        retry.release_start.set()
        self.transport = retry
        self.ws = retry_ws
        await retry.connect()
        self.backend_start_calls += retry.start_calls
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
        transport = self.transport
        connected = transport.is_connected
        tasks = [
            task
            for task in (
                transport._connect_task,
                transport._receive_task,
                *transport._emit_tasks,
            )
            if task is not None
        ]
        return NormalizedTransportLifecycleState(
            connected=connected,
            active_generation=(str(transport._connection_epoch.generation) if connected else None),
            owned_work=sum(not task.done() for task in tasks),
            queued_frames=(transport._in_queue.qsize() if connected else 0),
            retained_cleanup=int(
                transport._disconnect_cleanup_error is not None and transport._socket_close_pending
            ),
        )

    def snapshot_state(self) -> dict[str, object]:
        state = self.normalized_state()
        return {
            "connected": state.connected,
            "active_generation": state.active_generation,
            "owned_work": state.owned_work,
            "queued_frames": state.queued_frames,
            "retained_cleanup": state.retained_cleanup,
            "socket_close_pending": self.transport._socket_close_pending,
            "stream_id": self.transport.stream_id,
            "call_control_id": self.transport.call_control_id,
            "backend_start_calls": self.backend_start_calls,
            "backend_close_calls": self.backend_close_calls,
            "lifecycle_publications": self.publications,
        }

    def reset(self) -> None:
        assert self.normalized_state() == NormalizedTransportLifecycleState()
        ws = _FakeTelnyxWebSocket()
        self._set_transport(
            TelnyxConnectionTransport(ws),  # type: ignore[arg-type]
            ws,
        )


class TestTelnyxTransportLifecycleScenarios(TransportLifecycleScenarioSuite):
    pytestmark: ClassVar[list[Any]] = [
        pytest.mark.contract,
        pytest.mark.surface_transport,
        pytest.mark.provider("telnyx"),
    ]
    driver_factory = _TelnyxLifecycleDriver
    expected_degraded_event = NormalizedDegradedEvent(
        provider="telephony",
        reason="inbound_queue_full",
        detail="dropped 2-byte Telnyx frame; inbound queue full",
        fatal=False,
    )
    expected_interrupted_disconnect_state = (False, 1)
