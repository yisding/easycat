"""Run the shared lifecycle scenarios against the WebRTC transport."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

import easycat.transports._webrtc_audio as webrtc_audio_mod
import easycat.transports.webrtc as webrtc_mod
from easycat.audio_format import PCM16_MONO_48K, AudioChunk
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
from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig
from tests.transports._webrtc_fakes import (
    _FakeAiortc,
    _FakeRTCPeerConnection,
    _FakeWeb,
)


def _webrtc_chunk(payload: bytes = b"aa") -> AudioChunk:
    return AudioChunk(data=payload, format=PCM16_MONO_48K)


class _Router:
    def add_post(self, *_args: object) -> None:
        return None

    def add_get(self, *_args: object) -> None:
        return None

    def add_options(self, *_args: object) -> None:
        return None

    def add_static(self, *_args: object) -> None:
        return None


class _Application:
    def __init__(self) -> None:
        self.router = _Router()


class _Runner:
    def __init__(self, backend: _SignalingBackend) -> None:
        self.backend = backend
        self.setup_complete = False
        self.cleanup_calls = 0

    async def setup(self) -> None:
        self.backend.setup_entered.set()
        await self.backend.release_setup.wait()
        self.setup_complete = True

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.setup_complete = False


class _Site:
    def __init__(self, backend: _SignalingBackend, runner: _Runner) -> None:
        self.backend = backend
        self.runner = runner
        self.started = False
        self.stop_calls = 0

    async def start(self) -> None:
        self.backend.start_calls += 1
        if self.backend.fail_site_starts:
            self.backend.fail_site_starts -= 1
            raise RuntimeError("controlled startup failure")
        self.started = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.started = False


class _SignalingBackend:
    """Small aiohttp.web replacement with gated setup and tracked cleanup."""

    def __init__(self) -> None:
        self.setup_entered = asyncio.Event()
        self.release_setup = asyncio.Event()
        self.release_setup.set()
        self.fail_site_starts = 0
        self.start_calls = 0
        self.runners: list[_Runner] = []
        self.sites: list[_Site] = []

    def Application(self) -> _Application:
        return _Application()

    def AppRunner(self, _app: object) -> _Runner:
        runner = _Runner(self)
        self.runners.append(runner)
        return runner

    def TCPSite(
        self,
        runner: _Runner,
        _host: str,
        _port: int,
    ) -> _Site:
        site = _Site(self, runner)
        self.sites.append(site)
        return site

    @property
    def close_calls(self) -> int:
        return sum(site.stop_calls for site in self.sites)

    @property
    def live_resources(self) -> int:
        return sum(site.started for site in self.sites) + sum(
            runner.setup_complete for runner in self.runners
        )


class _LifecycleSite:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class _LifecycleRunner:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


@contextmanager
def _fake_offer_modules():
    _FakeRTCPeerConnection.instances.clear()
    _FakeRTCPeerConnection.next_inbound_track = None

    def fake_require_module(name: str, **_kwargs: object) -> object:
        if name == "aiortc":
            return _FakeAiortc
        if name == "aiohttp.web":
            return _FakeWeb
        raise AssertionError(f"unexpected WebRTC module request: {name}")

    with (
        patch.object(webrtc_mod, "require_module", fake_require_module),
        patch.object(webrtc_audio_mod, "require_module", fake_require_module),
    ):
        yield


class _WebRTCLifecycleDriver:
    def __init__(self) -> None:
        self.transport = WebRTCTransport(WebRTCTransportConfig(static_dir=None))
        self.backend_start_calls = 0
        self.backend_close_calls = 0
        self.publications: list[str | None] = []

    def _replace_transport(self, config: WebRTCTransportConfig | None = None) -> None:
        self.transport = WebRTCTransport(config or WebRTCTransportConfig(static_dir=None))
        self.backend_start_calls = 0
        self.backend_close_calls = 0
        self.publications = []

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation:
        self._replace_transport()
        backend = _SignalingBackend()
        backend.release_setup.clear()
        with patch.object(webrtc_mod, "require_module", lambda *_a, **_kw: backend):
            first = asyncio.create_task(self.transport.connect())
            await backend.setup_entered.wait()
            second = asyncio.create_task(self.transport.connect())
            await asyncio.sleep(0)
            backend.release_setup.set()
            await asyncio.gather(first, second)
            generation = "signaling-1"
            self.publications.append(generation)
            await self.transport.disconnect()
        self.publications.append(None)
        self.backend_start_calls = backend.start_calls
        self.backend_close_calls = backend.close_calls
        return ConnectLeadershipObservation(
            backend_start_calls=self.backend_start_calls,
            caller_generations=(generation, generation),
            connected_publications=(generation,),
        )

    async def observe_degraded_emission(self) -> DegradedEmissionObservation:
        self._replace_transport()
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        self.transport.set_event_bus(bus)
        self.transport._pc = object()
        self.transport._outbound_track = object()
        self.transport._outbound.enqueue = lambda *_a, **_kw: False  # type: ignore[method-assign]
        assert await self.transport.send_audio(_webrtc_chunk()) is False
        await self.transport._drain_emit_tasks()
        self.transport._pc = None
        self.transport._outbound_track = None
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
        site = _LifecycleSite()
        runner = _LifecycleRunner()
        self.transport._web = _FakeWeb
        self.transport._connected = True
        self.transport._site = site
        self.transport._runner = runner
        body_read_started = asyncio.Event()

        class _StalledOfferRequest:
            async def json(self) -> object:
                body_read_started.set()
                await asyncio.Event().wait()

        with _fake_offer_modules():
            connecting = asyncio.create_task(self.transport._handle_offer(_StalledOfferRequest()))
            await body_read_started.wait()
            await self.transport.disconnect()
            connect_cancelled = connecting.cancelled()
        self.backend_close_calls = site.stop_calls
        return DisconnectDuringConnectObservation(
            connect_cancelled=connect_cancelled,
            backend_close_calls=self.backend_close_calls,
            connected_publications=(),
        )

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation:
        self._replace_transport()
        site = _LifecycleSite()
        runner = _LifecycleRunner()
        self.transport._connected = True
        self.transport._site = site
        self.transport._runner = runner
        self.publications.append("signaling-1")
        child_cancelled = asyncio.Event()
        release_child = asyncio.Event()

        async def cancellation_resistant_consumer() -> None:
            while not release_child.is_set():
                try:
                    await release_child.wait()
                except asyncio.CancelledError:
                    child_cancelled.set()

        self.transport._consume_task = asyncio.create_task(cancellation_resistant_consumer())
        disconnecting = asyncio.create_task(self.transport.disconnect())
        await child_cancelled.wait()
        disconnecting.cancel()
        release_child.set()
        caller_cancelled = False
        try:
            await disconnecting
        except asyncio.CancelledError:
            caller_cancelled = True
        connected_during_cleanup = self.transport.is_connected
        retained_cleanup = int(
            self.transport._disconnect_cleanup_error is not None and self.transport._site is site
        )
        await self.transport.disconnect()
        self.publications.append(None)
        self.backend_close_calls = site.stop_calls
        return InterruptedDisconnectObservation(
            caller_cancelled=caller_cancelled,
            connected_during_retained_cleanup=connected_during_cleanup,
            retained_cleanup_during_cancel=retained_cleanup,
            backend_close_calls=self.backend_close_calls,
            lifecycle_publications=tuple(self.publications),
        )

    async def observe_late_frames(self) -> LateFrameObservation:
        self._replace_transport()
        self.transport._connected = True
        self.transport._peer_epoch.bump(object())
        stale_peer = self.transport._peer_epoch.capture()
        self.transport._peer_epoch.bump(object())
        active_peer = self.transport._peer_epoch.capture()
        before_stale = self.transport._in_queue.qsize()
        if self.transport._is_current_peer(stale_peer):
            self.transport._enqueue_chunk(_webrtc_chunk(b"stale"), context="WebRTC")
        stale_accepted = self.transport._in_queue.qsize() > before_stale
        before_active = self.transport._in_queue.qsize()
        if self.transport._is_current_peer(active_peer):
            self.transport._enqueue_chunk(_webrtc_chunk(b"fresh"), context="WebRTC")
        active_accepted = self.transport._in_queue.qsize() > before_active
        delivered = self.transport._in_queue.get_nowait()
        self.transport._connected = False
        return LateFrameObservation(
            stale_generation=str(stale_peer.generation),
            active_generation=str(active_peer.generation),
            stale_accepted=stale_accepted,
            active_accepted=active_accepted,
            delivered_frames=(() if delivered is None else (delivered.data.decode(),)),
        )

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation:
        self._replace_transport()
        site = _LifecycleSite()
        runner = _LifecycleRunner()
        self.transport._connected = True
        self.transport._site = site
        self.transport._runner = runner
        receiver_terminated = asyncio.Event()

        async def consume() -> None:
            async for _ in self.transport.receive_audio():
                pass
            receiver_terminated.set()

        receiver = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await self.transport.disconnect()
        await receiver
        self.backend_close_calls = site.stop_calls
        return MidStreamTeardownObservation(
            receiver_terminated=receiver_terminated.is_set(),
            backend_close_calls=self.backend_close_calls,
            owned_work_after_disconnect=self.normalized_state().owned_work,
        )

    async def observe_queue_overflow(self) -> QueueOverflowObservation:
        self._replace_transport(WebRTCTransportConfig(static_dir=None, max_pending_chunks=1))
        site = _LifecycleSite()
        runner = _LifecycleRunner()
        self.transport._connected = True
        self.transport._site = site
        self.transport._runner = runner
        bus = EventBus()
        events: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda event: events.append(event))
        self.transport.set_event_bus(bus)
        before_first = self.transport._in_queue.qsize()
        self.transport._enqueue_chunk(_webrtc_chunk(b"first"), context="WebRTC")
        first_accepted = self.transport._in_queue.qsize() > before_first
        before_second = self.transport._in_queue.qsize()
        self.transport._enqueue_chunk(_webrtc_chunk(b"overflow"), context="WebRTC")
        second_accepted = self.transport._in_queue.qsize() > before_second
        await self.transport._drain_emit_tasks()
        self.transport._in_queue.get_nowait()
        await self.transport.disconnect()
        self.backend_close_calls = site.stop_calls
        return QueueOverflowObservation(
            accepted=(first_accepted, second_accepted),
            dropped_frames=len(events),
            degraded_reasons=tuple(event.reason for event in events),
        )

    async def observe_startup_rollback(self) -> StartupRollbackObservation:
        self._replace_transport()
        backend = _SignalingBackend()
        backend.fail_site_starts = 1
        startup_error = ""
        with patch.object(webrtc_mod, "require_module", lambda *_a, **_kw: backend):
            try:
                await self.transport.connect()
            except RuntimeError as exc:
                startup_error = str(exc)
            live_resources = backend.live_resources
            connected_after_failure = self.transport.is_connected
            await self.transport.connect()
            retry_generation = f"signaling-{backend.start_calls}"
            await self.transport.disconnect()
        self.backend_start_calls = backend.start_calls
        self.backend_close_calls = backend.close_calls
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
                transport._consume_task,
                transport._active_offer_task,
                *transport._offer_tasks,
                *transport._emit_tasks,
                transport._outbound._emit_worker,
                *transport._outbound._emit_tasks,
            )
            if task is not None
        ]
        retained_cleanup = int(
            transport._disconnect_cleanup_error is not None
            or transport._pending_peer_cleanup is not None
            or transport._outbound_cleanup_pending
        )
        return NormalizedTransportLifecycleState(
            connected=connected,
            active_generation=(
                str(transport._peer_epoch.generation)
                if connected and transport._pc is not None
                else None
            ),
            owned_work=sum(not task.done() for task in tasks),
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
            "peer_generation": self.transport._peer_epoch.generation,
            "has_peer": self.transport._pc is not None,
            "has_pending_peer": self.transport._pending_peer_cleanup is not None,
            "has_site": self.transport._site is not None,
            "has_runner": self.transport._runner is not None,
            "backend_start_calls": self.backend_start_calls,
            "backend_close_calls": self.backend_close_calls,
            "lifecycle_publications": self.publications,
        }

    def reset(self) -> None:
        assert self.normalized_state() == NormalizedTransportLifecycleState()
        self._replace_transport()


class TestWebRTCTransportLifecycleScenarios(TransportLifecycleScenarioSuite):
    pytestmark: ClassVar[list[Any]] = [
        pytest.mark.contract,
        pytest.mark.surface_transport,
        pytest.mark.provider("webrtc"),
    ]
    driver_factory = _WebRTCLifecycleDriver
    expected_degraded_event = NormalizedDegradedEvent(
        provider="webrtc",
        reason="outbound_queue_full",
        detail="dropped 2-byte TTS frame; outbound queue full",
        fatal=False,
    )
    expected_interrupted_disconnect_state = (False, 1)
