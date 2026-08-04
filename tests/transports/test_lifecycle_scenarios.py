"""Run the internal transport lifecycle suite against a deterministic model."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

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

pytestmark = [
    pytest.mark.surface_transport,
    pytest.mark.provider("offline-lifecycle-model"),
]


class _ControlledBackend:
    """Two-resource backend with independently gated startup and cleanup."""

    def __init__(self) -> None:
        self.start_gate = asyncio.Event()
        self.start_gate.set()
        self.close_gate = asyncio.Event()
        self.close_gate.set()
        self.start_entered = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.fail_start = False
        self.start_calls = 0
        self.close_calls = 0
        self.live_resources = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.live_resources = 2
        self.start_entered.set()
        await self.start_gate.wait()
        if self.fail_start:
            raise RuntimeError("controlled startup failure")

    async def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        await self.close_gate.wait()
        self.live_resources = 0


class _OfflineLifecycleTransport:
    """Small lifecycle model with shared connect/disconnect task ownership."""

    def __init__(self) -> None:
        self.backend = _ControlledBackend()
        self.connected = False
        self.active_generation: int | None = None
        self._next_generation = 0
        self._connect_owner: asyncio.Task[int] | None = None
        self._disconnect_owner: asyncio.Task[None] | None = None
        self._receiver_count = 0
        self.receiver_entered = asyncio.Event()
        self._frames: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
        self.degraded: list[NormalizedDegradedEvent] = []
        self.lifecycle_publications: list[int | None] = []
        self.dropped_frames = 0

    async def connect(self) -> int:
        if self.connected:
            assert self.active_generation is not None
            return self.active_generation
        owner = self._connect_owner
        if owner is None:
            owner = asyncio.create_task(self._connect())
            self._connect_owner = owner
        try:
            return await asyncio.shield(owner)
        finally:
            if owner.done() and self._connect_owner is owner:
                self._connect_owner = None

    async def _connect(self) -> int:
        try:
            await self.backend.start()
        except BaseException:
            await self.backend.close()
            raise
        self._next_generation += 1
        self.active_generation = self._next_generation
        self.connected = True
        self.lifecycle_publications.append(self.active_generation)
        return self.active_generation

    async def disconnect(self) -> None:
        owner = self._disconnect_owner
        if owner is None:
            owner = asyncio.create_task(self._disconnect())
            self._disconnect_owner = owner
        try:
            await asyncio.shield(owner)
        finally:
            if owner.done() and self._disconnect_owner is owner:
                self._disconnect_owner = None

    async def _disconnect(self) -> None:
        connecting = self._connect_owner
        if connecting is not None and not connecting.done():
            connecting.cancel()
            with contextlib.suppress(BaseException):
                await connecting

        if self.connected or self.backend.live_resources:
            await self.backend.close()
        if self.connected:
            self.lifecycle_publications.append(None)
        self.connected = False
        self.active_generation = None

        while not self._frames.empty():
            self._frames.get_nowait()
        if self._receiver_count:
            self._frames.put_nowait(None)

    def emit_degraded(self, reason: str, detail: str, *, fatal: bool = False) -> None:
        self.degraded.append(
            NormalizedDegradedEvent(
                provider="offline-lifecycle-model",
                reason=reason,
                detail=detail,
                fatal=fatal,
            )
        )

    def feed_frame(self, generation: int, payload: str) -> bool:
        if not self.connected or generation != self.active_generation:
            return False
        try:
            self._frames.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped_frames += 1
            self.emit_degraded("inbound_queue_full", "controlled frame drop")
            return False
        return True

    async def receive_frames(self) -> AsyncIterator[str]:
        self._receiver_count += 1
        self.receiver_entered.set()
        try:
            while True:
                frame = await self._frames.get()
                if frame is None:
                    return
                yield frame
        finally:
            self._receiver_count -= 1

    def normalized_state(self) -> NormalizedTransportLifecycleState:
        owned_work = self._receiver_count
        owned_work += int(self._connect_owner is not None and not self._connect_owner.done())
        owned_work += int(self._disconnect_owner is not None and not self._disconnect_owner.done())
        return NormalizedTransportLifecycleState(
            connected=self.connected,
            active_generation=(
                str(self.active_generation) if self.active_generation is not None else None
            ),
            owned_work=owned_work,
            queued_frames=self._frames.qsize(),
            retained_cleanup=int(
                self._disconnect_owner is not None and not self._disconnect_owner.done()
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
            "backend_start_calls": self.backend.start_calls,
            "backend_close_calls": self.backend.close_calls,
            "degraded_reasons": [event.reason for event in self.degraded],
            "lifecycle_publications": self.lifecycle_publications,
        }


class _OfflineLifecycleDriver:
    def __init__(self) -> None:
        self.transport = _OfflineLifecycleTransport()

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation:
        self.transport.backend.start_gate.clear()
        first = asyncio.create_task(self.transport.connect())
        await self.transport.backend.start_entered.wait()
        second = asyncio.create_task(self.transport.connect())
        await asyncio.sleep(0)
        self.transport.backend.start_gate.set()
        generations = await asyncio.gather(first, second)
        publications = tuple(
            generation
            for generation in self.transport.lifecycle_publications
            if generation is not None
        )
        start_calls = self.transport.backend.start_calls
        await self.transport.disconnect()
        return ConnectLeadershipObservation(
            backend_start_calls=start_calls,
            caller_generations=tuple(str(generation) for generation in generations),
            connected_publications=tuple(str(generation) for generation in publications),
        )

    async def observe_degraded_emission(self) -> DegradedEmissionObservation:
        await self.transport.connect()
        self.transport.emit_degraded("model_fault", "controlled lifecycle fault")
        events = tuple(self.transport.degraded)
        await self.transport.disconnect()
        return DegradedEmissionObservation(events=events)

    async def observe_disconnect_during_connect(
        self,
    ) -> DisconnectDuringConnectObservation:
        self.transport.backend.start_gate.clear()
        connecting = asyncio.create_task(self.transport.connect())
        await self.transport.backend.start_entered.wait()
        await self.transport.disconnect()
        connect_cancelled = False
        try:
            await connecting
        except asyncio.CancelledError:
            connect_cancelled = True
        return DisconnectDuringConnectObservation(
            connect_cancelled=connect_cancelled,
            backend_close_calls=self.transport.backend.close_calls,
            connected_publications=tuple(
                str(generation)
                for generation in self.transport.lifecycle_publications
                if generation is not None
            ),
        )

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation:
        await self.transport.connect()
        self.transport.backend.close_gate.clear()
        disconnecting = asyncio.create_task(self.transport.disconnect())
        await self.transport.backend.close_entered.wait()
        disconnecting.cancel()
        caller_cancelled = False
        try:
            await disconnecting
        except asyncio.CancelledError:
            caller_cancelled = True
        connected_during_cleanup = self.transport.connected
        retained_cleanup = self.transport.normalized_state().retained_cleanup
        self.transport.backend.close_gate.set()
        await self.transport.disconnect()
        return InterruptedDisconnectObservation(
            caller_cancelled=caller_cancelled,
            connected_during_retained_cleanup=connected_during_cleanup,
            retained_cleanup_during_cancel=retained_cleanup,
            backend_close_calls=self.transport.backend.close_calls,
            lifecycle_publications=tuple(
                None if generation is None else str(generation)
                for generation in self.transport.lifecycle_publications
            ),
        )

    async def observe_late_frames(self) -> LateFrameObservation:
        stale_generation = await self.transport.connect()
        await self.transport.disconnect()
        active_generation = await self.transport.connect()
        stale_accepted = self.transport.feed_frame(stale_generation, "stale")
        active_accepted = self.transport.feed_frame(active_generation, "fresh")
        delivered_frame = self.transport._frames.get_nowait()
        delivered = () if delivered_frame is None else (delivered_frame,)
        await self.transport.disconnect()
        return LateFrameObservation(
            stale_generation=str(stale_generation),
            active_generation=str(active_generation),
            stale_accepted=stale_accepted,
            active_accepted=active_accepted,
            delivered_frames=delivered,
        )

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation:
        await self.transport.connect()
        receiver_terminated = asyncio.Event()

        async def consume() -> None:
            async for _ in self.transport.receive_frames():
                pass
            receiver_terminated.set()

        receiver = asyncio.create_task(consume())
        await self.transport.receiver_entered.wait()
        await self.transport.disconnect()
        await receiver
        return MidStreamTeardownObservation(
            receiver_terminated=receiver_terminated.is_set(),
            backend_close_calls=self.transport.backend.close_calls,
            owned_work_after_disconnect=self.transport.normalized_state().owned_work,
        )

    async def observe_queue_overflow(self) -> QueueOverflowObservation:
        generation = await self.transport.connect()
        accepted = (
            self.transport.feed_frame(generation, "first"),
            self.transport.feed_frame(generation, "overflow"),
        )
        self.transport._frames.get_nowait()
        dropped_frames = self.transport.dropped_frames
        degraded_reasons = tuple(event.reason for event in self.transport.degraded)
        await self.transport.disconnect()
        return QueueOverflowObservation(
            accepted=accepted,
            dropped_frames=dropped_frames,
            degraded_reasons=degraded_reasons,
        )

    async def observe_startup_rollback(self) -> StartupRollbackObservation:
        self.transport.backend.fail_start = True
        startup_error = ""
        try:
            await self.transport.connect()
        except RuntimeError as exc:
            startup_error = str(exc)
        live_resources = self.transport.backend.live_resources
        connected_after_failure = self.transport.connected
        self.transport.backend.fail_start = False
        retry_generation = await self.transport.connect()
        await self.transport.disconnect()
        return StartupRollbackObservation(
            startup_error=startup_error,
            live_resources_after_failure=live_resources,
            connected_after_failure=connected_after_failure,
            retry_generation=str(retry_generation),
            backend_start_calls=self.transport.backend.start_calls,
            backend_close_calls=self.transport.backend.close_calls,
        )

    def normalized_state(self) -> NormalizedTransportLifecycleState:
        return self.transport.normalized_state()

    def snapshot_state(self) -> dict[str, object]:
        return self.transport.snapshot_state()

    def reset(self) -> None:
        assert self.transport.normalized_state() == NormalizedTransportLifecycleState()
        self.transport = _OfflineLifecycleTransport()


class TestOfflineTransportLifecycleScenarios(TransportLifecycleScenarioSuite):
    """Run every WS4.1 lifecycle row without optional backends or devices."""

    driver_factory = _OfflineLifecycleDriver
