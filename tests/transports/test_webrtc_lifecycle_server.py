"""WebRTC signaling server, lifecycle, receive, and degraded-event tests."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import EventBus, TransportDegraded
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.server.webrtc_routes import serve_webrtc_config_sessions
from easycat.transports.webrtc import (
    _DEGRADED_INBOUND_CONSUME_ERROR,
    _DEGRADED_NEGOTIATION_FAILED,
    _DEGRADED_OUTBOUND_QUEUE_FULL,
    ICEServer,
    WebRTCTransport,
    WebRTCTransportConfig,
)

from ._webrtc_fakes import (
    _HAS_AIOHTTP,
    _HAS_WEBRTC_DEPS,
    _FakeAudioFrame,
    _FakeInboundTrack,
    _FakeJsonRequest,
    _FakeOfferRequest,
    _FakeRTCPeerConnection,
    _FakeSessionDescription,
    _FakeWeb,
    _install_fake_webrtc_modules,
    _UsesPytestTcpPortFactory,
)
from .conftest import make_chunk


class _FakeSameOriginJsonRequest(_FakeJsonRequest):
    scheme = "http"
    host = "127.0.0.1:8080"

    def __init__(self, payload: object) -> None:
        super().__init__(payload)
        self.headers = {"Origin": "http://127.0.0.1:8080"}


class TestWebRTCIngressQueueOwnership:
    @pytest.mark.asyncio
    async def test_repeated_offer_keeps_active_receive_audio_on_same_queue(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        # The signaling server is live (an /offer can only reach the handler
        # once connect() has started it); offers received after teardown begins
        # are rejected with 503 instead.
        transport._connected = True
        original_queue = transport._in_queue

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        first_response = await transport._handle_offer(_FakeOfferRequest())
        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert first_response.status == 200
        assert second_response.status == 200
        assert transport._in_queue is original_queue
        await asyncio.sleep(0)
        assert not pending.done()

        new_chunk = make_chunk(8)
        transport._enqueue_chunk(new_chunk, context="test")
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_repeated_offer_drains_stale_audio_without_replacing_queue(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see test above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200

        original_queue = transport._in_queue
        stale_chunk = make_chunk(8)
        transport._enqueue_chunk(stale_chunk, context="test")
        transport._enqueue_sentinel()

        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert second_response.status == 200
        assert transport._in_queue is original_queue
        assert transport._in_queue.empty()

        new_chunk = make_chunk(10)
        transport._enqueue_chunk(new_chunk, context="test")
        audio_iter = transport.receive_audio()
        received = await asyncio.wait_for(anext(audio_iter), timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    async def test_repeated_offer_closes_previous_outbound_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Peer replacement must retire the old outbound source via aclose():
        ``disconnect()`` only closes the *current* source, so a delivery worker
        owned by the replaced source would otherwise survive indefinitely."""
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200
        first_outbound = transport._outbound

        closed = asyncio.Event()
        close_calls = 0
        original_aclose = first_outbound.aclose

        async def _tracking_aclose() -> None:
            nonlocal close_calls
            close_calls += 1
            closed.set()
            await original_aclose()

        monkeypatch.setattr(first_outbound, "aclose", _tracking_aclose)

        second_response = await transport._handle_offer(_FakeOfferRequest())
        assert second_response.status == 200
        assert transport._outbound is not first_outbound
        assert closed.is_set()
        assert close_calls == 1

    async def test_disconnected_then_failed_counts_one_peer_drop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        record_disconnect = Mock()
        transport._record_transport_disconnect = record_disconnect  # type: ignore[method-assign]

        assert (await transport._handle_offer(_FakeOfferRequest())).status == 200
        pc = _FakeRTCPeerConnection.instances[0]

        pc.connectionState = "disconnected"
        await pc._handlers["connectionstatechange"]()
        pc.connectionState = "failed"
        await pc._handlers["connectionstatechange"]()

        record_disconnect.assert_called_once_with("webrtc peer disconnected")

        # A genuine recovery followed by another drop is a distinct incident.
        pc.connectionState = "connected"
        await pc._handlers["connectionstatechange"]()
        pc.connectionState = "disconnected"
        await pc._handlers["connectionstatechange"]()
        assert record_disconnect.call_count == 2

    @pytest.mark.asyncio
    async def test_disconnect_does_not_hold_offer_lock_during_http_cleanup(self):
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        offer_task: asyncio.Task[object] | None = None

        class _OfferDuringStopSite:
            async def stop(self) -> None:
                nonlocal offer_task
                offer_task = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
                await asyncio.sleep(0)

        class _CleanupWaitsForHandlersRunner:
            async def cleanup(self) -> None:
                assert offer_task is not None
                response = await asyncio.wait_for(offer_task, timeout=1.0)
                assert response.status == 503

        transport._site = _OfferDuringStopSite()
        transport._runner = _CleanupWaitsForHandlersRunner()

        await asyncio.wait_for(transport.disconnect(), timeout=1.0)

        assert transport._site is None
        assert transport._runner is None
        assert offer_task is not None
        assert offer_task.done()

    @pytest.mark.asyncio
    async def test_disconnect_cancels_active_offer_before_waiting_for_offer_lock(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        body_read_started = asyncio.Event()
        body_read_cancelled = asyncio.Event()

        class _StalledOfferRequest:
            async def json(self) -> object:
                body_read_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    body_read_cancelled.set()
                    raise

        active_offer = asyncio.create_task(transport._handle_offer(_StalledOfferRequest()))
        await body_read_started.wait()
        queued_offer = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
        await asyncio.sleep(0)
        assert not queued_offer.done()

        await asyncio.wait_for(transport.disconnect(), timeout=1.0)

        assert active_offer.cancelled()
        assert body_read_cancelled.is_set()
        queued_response = await asyncio.wait_for(queued_offer, timeout=1.0)
        assert queued_response.status == 503
        assert transport._active_offer_task is None
        assert not transport._offer_lock.locked()
        assert transport._connected is False

    @pytest.mark.asyncio
    async def test_disconnect_bounds_uncooperative_offer_and_allows_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import easycat.transports.webrtc as webrtc_module

        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        offer_started = asyncio.Event()
        offer_cancelled = asyncio.Event()
        release_offer = asyncio.Event()
        runner = SimpleNamespace(cleanup=AsyncMock())
        transport._runner = runner
        monkeypatch.setattr(webrtc_module, "_OFFER_CANCEL_DRAIN_TIMEOUT_S", 0.01)

        async def permanently_resistant_offer(_request: object) -> object:
            offer_started.set()
            while not release_offer.is_set():
                try:
                    await release_offer.wait()
                except asyncio.CancelledError:
                    offer_cancelled.set()
            return object()

        monkeypatch.setattr(transport, "_handle_offer_locked", permanently_resistant_offer)
        active_offer = asyncio.create_task(transport._handle_offer(object()))
        await offer_started.wait()
        queued_offer = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
        await asyncio.sleep(0)

        with pytest.raises(TimeoutError, match="offer handler did not stop"):
            await asyncio.wait_for(transport.disconnect(), timeout=0.25)

        assert offer_cancelled.is_set()
        assert queued_offer.cancelled()
        assert (await transport._handle_offer(_FakeOfferRequest())).status == 503
        runner.cleanup.assert_not_awaited()
        assert isinstance(transport._disconnect_cleanup_error, TimeoutError)

        release_offer.set()
        await active_offer
        await transport.disconnect()

        runner.cleanup.assert_awaited_once()
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_disconnect_discards_candidate_when_sdp_await_consumes_cancellation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        negotiation_started = asyncio.Event()

        async def cancellation_resistant_remote_description(
            self: _FakeRTCPeerConnection,
            _offer: _FakeSessionDescription,
        ) -> None:
            negotiation_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

        monkeypatch.setattr(
            _FakeRTCPeerConnection,
            "setRemoteDescription",
            cancellation_resistant_remote_description,
        )
        active_offer = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
        await negotiation_started.wait()

        await asyncio.wait_for(transport.disconnect(), timeout=1.0)

        response = active_offer.result()
        assert response.status == 503
        assert len(_FakeRTCPeerConnection.instances) == 1
        assert _FakeRTCPeerConnection.instances[0].closed is True
        assert transport._pc is None
        assert transport._pending_peer_cleanup is None

    @pytest.mark.asyncio
    async def test_disconnect_discards_candidate_when_retirement_consumes_cancellation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True

        assert (await transport._handle_offer(_FakeOfferRequest())).status == 200
        old_peer = _FakeRTCPeerConnection.instances[0]
        retirement_started = asyncio.Event()

        async def cancellation_resistant_close() -> None:
            retirement_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                old_peer.closed = True
                old_peer.connectionState = "closed"

        monkeypatch.setattr(old_peer, "close", cancellation_resistant_close)
        replacement = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
        await retirement_started.wait()

        await asyncio.wait_for(transport.disconnect(), timeout=1.0)

        response = replacement.result()
        candidate = _FakeRTCPeerConnection.instances[1]
        assert response.status == 503
        assert candidate.closed is True
        assert transport._pc is None
        assert transport._retiring_peer_generation is None

    @pytest.mark.asyncio
    async def test_disconnect_from_active_offer_fails_instead_of_self_deadlocking(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = WebRTCTransport()
        transport._connected = True

        async def disconnect_from_offer(_request: object) -> object:
            await transport.disconnect()
            raise AssertionError("disconnect should reject offer-task reentrancy")

        monkeypatch.setattr(transport, "_handle_offer_locked", disconnect_from_offer)

        with pytest.raises(RuntimeError, match="cannot run from the active offer handler"):
            await asyncio.wait_for(transport._handle_offer(object()), timeout=1.0)

        assert transport._connected is True
        assert transport._active_offer_task is None
        assert not transport._offer_lock.locked()

    @pytest.mark.asyncio
    async def test_cancelled_disconnect_reaps_active_offer_and_remains_retryable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = WebRTCTransport()
        transport._connected = True
        offer_cancelled = asyncio.Event()
        release_offer = asyncio.Event()

        async def cancellation_resistant_offer(_request: object) -> object:
            while not release_offer.is_set():
                try:
                    await release_offer.wait()
                except asyncio.CancelledError:
                    offer_cancelled.set()
            return object()

        monkeypatch.setattr(transport, "_handle_offer_locked", cancellation_resistant_offer)
        active_offer = asyncio.create_task(transport._handle_offer(object()))
        await asyncio.sleep(0)

        disconnecting = asyncio.create_task(transport.disconnect())
        await offer_cancelled.wait()
        disconnecting.cancel()
        release_offer.set()

        with pytest.raises(asyncio.CancelledError):
            await disconnecting

        assert active_offer.done()
        assert transport._active_offer_task is None
        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)

        await transport.disconnect()

        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_disconnect_cleanup_failures_are_best_effort_and_retryable(self):
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        pc = SimpleNamespace(
            close=AsyncMock(side_effect=[RuntimeError("peer close failed"), None])
        )
        site = SimpleNamespace(
            stop=AsyncMock(side_effect=[RuntimeError("site stop failed"), None])
        )
        runner = SimpleNamespace(cleanup=AsyncMock())
        transport._pc = pc
        transport._site = site
        transport._runner = runner
        transport._outbound_track = object()

        with pytest.raises(RuntimeError, match="peer close failed"):
            await transport.disconnect()

        # The first error wins, but later cleanup stages still run. Only the
        # resources whose cleanup failed remain reachable for a retry.
        pc.close.assert_awaited_once()
        site.stop.assert_awaited_once()
        runner.cleanup.assert_awaited_once()
        assert transport._pc is pc
        assert transport._site is site
        assert transport._runner is None
        assert transport._connected is False
        assert transport._outbound_track is None
        assert transport._peer_closed.is_set()
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await transport.connect()

        await transport.disconnect()

        assert pc.close.await_count == 2
        assert site.stop.await_count == 2
        runner.cleanup.assert_awaited_once()
        assert transport._pc is None
        assert transport._site is None
        assert transport._runner is None
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_cancelled_connect_rolls_back_partial_signaling_stack(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import easycat.transports.webrtc as webrtc_module

        start_entered = asyncio.Event()

        async def block_start() -> None:
            start_entered.set()
            await asyncio.Event().wait()

        router = SimpleNamespace(
            add_post=Mock(),
            add_get=Mock(),
            add_options=Mock(),
            add_static=Mock(),
        )
        app = SimpleNamespace(router=router)
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(
            start=AsyncMock(side_effect=block_start),
            stop=AsyncMock(),
        )
        web = SimpleNamespace(
            Application=Mock(return_value=app),
            AppRunner=Mock(return_value=runner),
            TCPSite=Mock(return_value=site),
        )
        monkeypatch.setattr(webrtc_module, "require_module", lambda *_a, **_kw: web)
        transport = WebRTCTransport(WebRTCTransportConfig(static_dir=None))

        connecting = asyncio.create_task(transport.connect())
        await start_entered.wait()
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting

        site.stop.assert_awaited_once()
        runner.cleanup.assert_awaited_once()
        assert transport._connected is False
        assert transport._site is None
        assert transport._runner is None
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel_count", [1, 2])
    async def test_connect_rollback_preserves_new_caller_cancellation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cancel_count: int,
    ) -> None:
        import easycat.transports.webrtc as webrtc_module

        startup_error = RuntimeError("setup failed")
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def block_cleanup() -> None:
            cleanup_entered.set()
            await release_cleanup.wait()

        router = SimpleNamespace(
            add_post=Mock(),
            add_get=Mock(),
            add_options=Mock(),
            add_static=Mock(),
        )
        app = SimpleNamespace(router=router)
        runner = SimpleNamespace(
            setup=AsyncMock(side_effect=startup_error),
            cleanup=AsyncMock(side_effect=block_cleanup),
        )
        web = SimpleNamespace(
            Application=Mock(return_value=app),
            AppRunner=Mock(return_value=runner),
            TCPSite=Mock(),
        )
        monkeypatch.setattr(webrtc_module, "require_module", lambda *_a, **_kw: web)
        transport = WebRTCTransport(WebRTCTransportConfig(static_dir=None))

        connecting = asyncio.create_task(transport.connect())
        await cleanup_entered.wait()
        for _ in range(cancel_count):
            connecting.cancel()
            await asyncio.sleep(0)
        release_cleanup.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await connecting

        assert exc_info.value.__cause__ is startup_error
        runner.cleanup.assert_awaited_once()
        assert transport._runner is None

    @pytest.mark.asyncio
    async def test_concurrent_connects_publish_one_signaling_stack(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import easycat.transports.webrtc as webrtc_module

        setup_entered = asyncio.Event()
        release_setup = asyncio.Event()

        async def block_setup() -> None:
            setup_entered.set()
            await release_setup.wait()

        router = SimpleNamespace(
            add_post=Mock(),
            add_get=Mock(),
            add_options=Mock(),
            add_static=Mock(),
        )
        app = SimpleNamespace(router=router)
        runner = SimpleNamespace(
            setup=AsyncMock(side_effect=block_setup),
            cleanup=AsyncMock(),
        )
        site = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        web = SimpleNamespace(
            Application=Mock(return_value=app),
            AppRunner=Mock(return_value=runner),
            TCPSite=Mock(return_value=site),
        )
        monkeypatch.setattr(webrtc_module, "require_module", lambda *_a, **_kw: web)
        transport = WebRTCTransport(WebRTCTransportConfig(static_dir=None))

        first = asyncio.create_task(transport.connect())
        await setup_entered.wait()
        second = asyncio.create_task(transport.connect())
        await asyncio.sleep(0)
        web.AppRunner.assert_called_once()

        release_setup.set()
        await asyncio.gather(first, second)

        web.AppRunner.assert_called_once()
        web.TCPSite.assert_called_once()
        runner.setup.assert_awaited_once()
        site.start.assert_awaited_once()
        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cancelled_disconnect_blocks_reconnect_until_cleanup_retry(
        self,
    ) -> None:
        transport = WebRTCTransport(WebRTCTransportConfig(static_dir=None))
        stop_entered = asyncio.Event()

        async def block_stop() -> None:
            stop_entered.set()
            await asyncio.Event().wait()

        site = SimpleNamespace(
            stop=AsyncMock(side_effect=block_stop),
        )
        runner = SimpleNamespace(cleanup=AsyncMock())
        transport._connected = True
        transport._site = site
        transport._runner = runner

        disconnecting = asyncio.create_task(transport.disconnect())
        await stop_entered.wait()
        disconnecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await disconnecting

        assert transport._site is site
        assert transport._runner is runner
        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await transport.connect()

        site.stop = AsyncMock()
        await transport.disconnect()
        assert transport._site is None
        assert transport._runner is None
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_disconnect_preserves_caller_cancellation_while_reaping_consumer(
        self,
    ) -> None:
        transport = WebRTCTransport(WebRTCTransportConfig(static_dir=None))
        child_cancelled = asyncio.Event()
        release_child = asyncio.Event()

        async def cancellation_resistant_consumer() -> None:
            while not release_child.is_set():
                try:
                    await release_child.wait()
                except asyncio.CancelledError:
                    child_cancelled.set()

        transport._connected = True
        transport._consume_task = asyncio.create_task(cancellation_resistant_consumer())

        disconnecting = asyncio.create_task(transport.disconnect())
        await child_cancelled.wait()
        disconnecting.cancel()
        release_child.set()

        with pytest.raises(asyncio.CancelledError):
            await disconnecting

        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)
        assert transport._consume_task is None
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await transport.connect()

        await transport.disconnect()

        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_failed_replacement_offer_keeps_existing_peer_and_receiver(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see tests above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200
        first_pc = _FakeRTCPeerConnection.instances[0]
        first_generation = transport._peer_generation

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        async def _boom(self) -> _FakeSessionDescription:
            raise RuntimeError("sdp boom")

        monkeypatch.setattr(_FakeRTCPeerConnection, "createAnswer", _boom)

        failed_response = await transport._handle_offer(_FakeOfferRequest())

        assert failed_response.status == 400
        assert transport._peer_generation == first_generation
        assert transport._pc is first_pc
        assert not first_pc.closed
        assert _FakeRTCPeerConnection.instances[1].closed
        await asyncio.sleep(0)
        assert not pending.done()

        new_chunk = make_chunk(12)
        transport._enqueue_chunk(new_chunk, context="test")
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_cancelled_offer_closes_unpublished_candidate_peer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        remote_description_started = asyncio.Event()

        async def block_remote_description(
            self: _FakeRTCPeerConnection,
            _offer: _FakeSessionDescription,
        ) -> None:
            remote_description_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            _FakeRTCPeerConnection,
            "setRemoteDescription",
            block_remote_description,
        )
        handling = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
        await remote_description_started.wait()
        handling.cancel()

        with pytest.raises(asyncio.CancelledError):
            await handling

        assert len(_FakeRTCPeerConnection.instances) == 1
        assert _FakeRTCPeerConnection.instances[0].closed is True
        assert transport._pc is None
        assert transport._peer_generation == 0

    @pytest.mark.asyncio
    async def test_unpublished_peer_close_settles_before_repeated_cancellation(self) -> None:
        transport = WebRTCTransport()
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def block_close() -> None:
            close_entered.set()
            await release_close.wait()

        pc = SimpleNamespace(close=AsyncMock(side_effect=block_close))
        closing = asyncio.create_task(transport._close_unpublished_peer(pc))
        await close_entered.wait()
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        release_close.set()

        with pytest.raises(asyncio.CancelledError):
            await closing

        pc.close.assert_awaited_once()
        assert transport._pending_peer_cleanup is None

    @pytest.mark.asyncio
    async def test_cancelled_replacement_retirement_closes_candidate_and_is_retryable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True

        assert (await transport._handle_offer(_FakeOfferRequest())).status == 200
        first_generation = transport._peer_generation
        old_outbound = transport._outbound
        original_aclose = old_outbound.aclose
        retirement_started = asyncio.Event()

        async def blocking_aclose() -> None:
            retirement_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(old_outbound, "aclose", blocking_aclose)
        handling = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
        await retirement_started.wait()
        candidate = _FakeRTCPeerConnection.instances[-1]
        handling.cancel()

        with pytest.raises(asyncio.CancelledError):
            await handling

        assert candidate.closed is True
        assert transport._peer_generation == first_generation
        assert transport._pc is None
        assert transport._connected is False
        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)

        monkeypatch.setattr(old_outbound, "aclose", original_aclose)
        await transport.disconnect()
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_failed_replacement_retirement_closes_candidate_and_blocks_offers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True

        assert (await transport._handle_offer(_FakeOfferRequest())).status == 200
        first_generation = transport._peer_generation
        old_outbound = transport._outbound
        original_aclose = old_outbound.aclose
        close_calls = 0

        async def fail_once() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise RuntimeError("old outbound cleanup failed")
            await original_aclose()

        monkeypatch.setattr(old_outbound, "aclose", fail_once)

        with pytest.raises(RuntimeError, match="old outbound cleanup failed"):
            await transport._handle_offer(_FakeOfferRequest())

        candidate = _FakeRTCPeerConnection.instances[-1]
        assert candidate.closed is True
        assert transport._peer_generation == first_generation
        assert transport._connected is False
        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)
        assert (await transport._handle_offer(_FakeOfferRequest())).status == 503

        await transport.disconnect()
        assert close_calls == 2
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_track_event_during_set_remote_description_starts_consumer(self, monkeypatch):
        # aiortc fires the synchronous ``track`` event during
        # setRemoteDescription, before the offer handler commits the new peer
        # generation. A successful offer must still start ``_consume_task`` and
        # forward the captured track's frames to receive_audio().
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see tests above)

        # Frame already at the pipeline target rate (16 kHz mono) so the consume
        # path forwards the raw PCM without resampling/downmixing.
        target_rate = transport._config.audio_format.sample_rate
        frame_pcm = bytes(range(40))
        inbound = _FakeInboundTrack(frames=[_FakeAudioFrame(frame_pcm, sample_rate=target_rate)])
        _FakeRTCPeerConnection.next_inbound_track = inbound

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        response = await transport._handle_offer(_FakeOfferRequest())
        assert response.status == 200

        # The deferred consumer must be created and running post-commit.
        assert transport._consume_task is not None
        assert not transport._consume_task.done()
        assert transport._receive_tasks.owns_root
        root = RuntimeScope.create_root(
            name="session",
            root_id="test-root:webrtc-receive",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        transport.set_runtime_scope(root, name="transport-runtime")
        assert root.tasks("webrtc_receive") == (transport._consume_task,)
        assert "transport-receive" in root.cohorts(force=False)
        # The ``ended`` handler must be registered on the captured track.
        assert "ended" in inbound._handlers

        # The frame the track delivered must reach receive_audio().
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received.data == frame_pcm
        assert received.format == transport._config.audio_format

        await audio_iter.aclose()
        await transport.disconnect()
        assert not root.tasks("webrtc_receive")

    @pytest.mark.asyncio
    async def test_inbound_consume_ignores_pyav_plane_padding(self):
        transport = WebRTCTransport()
        target_format = transport._config.audio_format

        class _StereoLayout:
            channels = (object(), object())

        class _PackedFormat:
            is_planar = False
            bytes = 2

        class _PaddedDecodedFrame:
            sample_rate = 48_000
            samples = 960
            layout = _StereoLayout()
            format = _PackedFormat()

            def __init__(self) -> None:
                # 20 ms of valid packed stereo PCM at 48 kHz. The fake plane
                # above mirrors PyAV's padded decoded aiortc buffers; the
                # transport must use only these valid samples.
                valid_pcm = bytes(960 * 2 * 2)
                self.planes = [valid_pcm + (b"\xff" * (23_040 - len(valid_pcm)))]

            def to_ndarray(self):  # pragma: no cover - must not be used
                raise AssertionError("WebRTC inbound extraction must not require NumPy")

        class _OneFrameTrack:
            def __init__(self) -> None:
                self._delivered = False

            async def recv(self) -> object:
                if self._delivered:
                    raise StopAsyncIteration
                self._delivered = True
                return _PaddedDecodedFrame()

        await transport._consume_audio(_OneFrameTrack())

        chunks = [chunk async for chunk in transport.receive_audio()]
        assert len(chunks) == 1
        assert chunks[0].format == target_format
        assert len(chunks[0].data) == int(target_format.bytes_per_second * 0.02)
        assert chunks[0].duration_ms == 20

    @pytest.mark.asyncio
    async def test_replacing_connected_peer_clears_wait_for_client(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see test above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200
        first_pc = _FakeRTCPeerConnection.instances[0]
        first_pc.connectionState = "connected"
        first_connected = first_pc._handlers["connectionstatechange"]()
        if asyncio.iscoroutine(first_connected):
            await first_connected
        assert transport.has_client
        assert transport._client_connected.is_set()

        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert second_response.status == 200
        assert first_pc.closed
        assert not transport.has_client
        assert not transport._client_connected.is_set()

        second_pc = _FakeRTCPeerConnection.instances[1]
        second_pc.connectionState = "connected"
        second_connected = second_pc._handlers["connectionstatechange"]()
        if asyncio.iscoroutine(second_connected):
            await second_connected
        assert transport.has_client
        assert transport._client_connected.is_set()


class TestWebRTCStatsArtifact:
    @pytest.mark.asyncio
    async def test_stats_endpoint_requires_same_origin_for_unauthenticated_stats_path(
        self, tmp_path
    ):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(WebRTCTransportConfig(stats_path=str(stats_path)))
        transport._web = _FakeWeb

        response = await transport._handle_stats(_FakeJsonRequest({"kind": "webrtc_client_stats"}))

        assert response.status == 403
        assert not stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_endpoint_requires_token_for_non_loopback_stats_path(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(host="0.0.0.0", stats_path=str(stats_path))
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats"})
        )

        assert response.status == 403
        assert not stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_endpoint_caps_records(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_records=1)
        )
        transport._web = _FakeWeb

        first = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 1})
        )
        second = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 2})
        )

        assert first.status == 200
        assert second.status == 429
        assert len(stats_path.read_text(encoding="utf-8").splitlines()) == 1

    @pytest.mark.asyncio
    async def test_stats_endpoint_rate_limits_requests(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_requests_per_minute=1)
        )
        transport._web = _FakeWeb

        first = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 1})
        )
        second = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 2})
        )

        assert first.status == 200
        assert second.status == 429
        assert len(stats_path.read_text(encoding="utf-8").splitlines()) == 1

    @pytest.mark.asyncio
    async def test_stats_endpoint_caps_file_size(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_file_bytes=10)
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 1})
        )

        assert response.status == 429
        assert not stats_path.exists()


class TestWebRTCOutboundNormalization:
    @pytest.mark.asyncio
    async def test_send_audio_downmixes_stereo_before_webrtc_enqueue(self) -> None:
        transport = WebRTCTransport()
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        source_format = AudioFormat(sample_rate=48_000, channels=2, sample_width=2)
        stereo = struct.pack("<1920h", *([1_000, 3_000] * 960))
        chunk = AudioChunk(data=stereo, format=source_format)

        assert await transport.send_audio(chunk) is True

        queued = transport._outbound._queue.get_nowait()
        assert len(queued.transport_data) == 960 * 2
        assert struct.unpack("<960h", queued.transport_data) == (2_000,) * 960
        assert queued.original_chunk is chunk

    @pytest.mark.asyncio
    async def test_send_audio_resamples_mono_before_webrtc_enqueue(self) -> None:
        transport = WebRTCTransport()
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        source_format = AudioFormat(sample_rate=24_000, channels=1, sample_width=2)
        chunk = AudioChunk(
            data=struct.pack("<480h", *([1_000] * 480)),
            format=source_format,
        )

        assert await transport.send_audio(chunk) is True

        queued = transport._outbound._queue.get_nowait()
        assert len(queued.transport_data) == 960 * 2
        assert queued.original_chunk is chunk

    @pytest.mark.asyncio
    async def test_send_audio_rejects_non_pcm16_input(self) -> None:
        transport = WebRTCTransport()
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        chunk = AudioChunk(
            data=b"\x80" * 960,
            format=AudioFormat(sample_rate=48_000, channels=1, sample_width=1),
        )

        with pytest.raises(ValueError, match="chunk.format must be PCM16"):
            await transport.send_audio(chunk)

    @pytest.mark.asyncio
    async def test_send_audio_rejects_partial_stereo_frames(self) -> None:
        transport = WebRTCTransport()
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        chunk = AudioChunk(
            data=b"\x00\x01\x02\x03\x04\x05",
            format=AudioFormat(sample_rate=48_000, channels=2, sample_width=2),
        )

        with pytest.raises(ValueError, match="complete PCM frames"):
            await transport.send_audio(chunk)

        assert transport._outbound._queue.empty()


@pytest.mark.integration_socket
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
class TestWebRTCTransportLifecycle(_UsesPytestTcpPortFactory):
    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        assert transport.is_connected

        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_default_host_serves_health_on_loopback(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(port=port, static_dir=None)
        transport = WebRTCTransport(config)

        await transport.connect()
        try:
            async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "ok"
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        transport = WebRTCTransport()
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        await transport.connect()  # Should not raise.
        assert transport.is_connected

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_redirects_to_bundled_client_when_present(self, tmp_path):
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
            async with session.get(f"http://127.0.0.1:{port}/", allow_redirects=False) as resp:
                assert resp.status == 302
                assert resp.headers["Location"] == "/webrtc_client.html"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_redirect_preserves_sanitized_webrtc_base(self, tmp_path):
        # The standalone transport serves FLAT routes; a clean same-origin
        # ``?webrtc=/proxy`` (e.g. a reverse-proxy path prefix) must survive the
        # redirect so the bundled client targets ``/proxy/offer``.
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/?webrtc=/proxy/&token=sekrit"
            async with session.get(url, allow_redirects=False) as resp:
                assert resp.status == 302
                assert resp.headers["Location"] == "/webrtc_client.html?token=sekrit&webrtc=/proxy"

        await transport.disconnect()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            "/../x",
            "//evil.test",
            "https://evil.test",
            ".attacker.test",
        ],
    )
    async def test_root_redirect_strips_untrusted_webrtc_base(self, tmp_path, raw):
        # Untrusted / cross-origin / traversal ``?webrtc=`` values must be
        # dropped server-side rather than echoed into the redirect location.
        from urllib.parse import urlencode

        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)
        await transport.connect()

        query = urlencode({"webrtc": raw, "token": "sekrit"})
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/?{query}", allow_redirects=False) as resp,
        ):
            assert resp.status == 302
            location = resp.headers["Location"]

        assert "webrtc=" not in location
        assert location == "/webrtc_client.html?token=sekrit"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_returns_endpoint_hint_without_static_client(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=None)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "easycat-webrtc-signaling"
                assert "/offer" in data["endpoints"]
                assert "/stats" in data["endpoints"]
                assert "Access-Control-Allow-Origin" not in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_failed_connect_does_not_leave_stale_bundled_client_state(
        self,
        tmp_path,
        monkeypatch,
    ):
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)

        async def broken_start(_self):
            raise RuntimeError("port busy")

        monkeypatch.setattr(aiohttp.web.TCPSite, "start", broken_start)

        with pytest.raises(RuntimeError, match="port busy"):
            await transport.connect()

        monkeypatch.undo()

        assert transport._has_bundled_client is False
        assert transport._app is None
        assert transport._runner is None
        assert transport._site is None

        # Retry on same instance without static files should not keep stale
        # redirect behavior from the failed attempt.
        transport._config.static_dir = None
        await transport.connect()

        async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "easycat-webrtc-signaling"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                assert "Access-Control-Allow-Origin" not in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_without_valid_sdp_returns_error(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            # Send invalid JSON.
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                data="not json",
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 400

            # Send valid JSON but invalid schema.
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json={"type": "answer", "sdp": "dummy"},
            ) as resp:
                assert resp.status == 400

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_endpoint_omits_turn_credentials_by_default(self):
        import aiohttp

        port = self._unused_port()
        servers = [
            ICEServer(urls="stun:stun.example.com:3478"),
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, ice_servers=servers)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
            async with session.get(f"http://127.0.0.1:{port}/config") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert "iceServers" in data
                assert len(data["iceServers"]) == 2
                # Public config should include URLs but should not leak TURN credentials by
                # default.
                turn = data["iceServers"][1]
                assert turn["urls"] == ["turn:turn.example.com:3478"]
                assert "username" not in turn
                assert "credential" not in turn

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_endpoint_can_expose_turn_credentials(self):
        import aiohttp

        port = self._unused_port()
        servers = [
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(
            host="127.0.0.1",
            port=port,
            ice_servers=servers,
            expose_ice_credentials=True,
        )
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:  # noqa: SIM117 nested scopes clarify setup and cleanup
            async with session.get(f"http://127.0.0.1:{port}/config") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["iceServers"] == [
                    {
                        "urls": ["turn:turn.example.com:3478"],
                        "username": "user",
                        "credential": "pass",
                    }
                ]

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_uses_full_ice_credentials_for_server_peer(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        servers = [
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            )
        ]
        transport = WebRTCTransport(WebRTCTransportConfig(ice_servers=servers))
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeOfferRequest())

        assert response.status == 200
        pc = _FakeRTCPeerConnection.instances[0]
        assert pc.config.iceServers[0].kwargs == {
            "urls": ["turn:turn.example.com:3478"],
            "username": "user",
            "credential": "pass",
        }

    @pytest.mark.asyncio
    async def test_cors_preflight_allows_same_origin(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        origin = f"http://127.0.0.1:{port}"
        async with (
            aiohttp.ClientSession() as session,
            session.options(
                f"http://127.0.0.1:{port}/offer",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                },
            ) as resp,
        ):
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == origin
            assert resp.headers["Access-Control-Allow-Methods"] == "POST, GET, OPTIONS"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_preflight_denies_unknown_cross_origin_by_default(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with (
            aiohttp.ClientSession() as session,
            session.options(
                f"http://127.0.0.1:{port}/offer",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            ) as resp,
        ):
            assert resp.status == 200
            assert "Access-Control-Allow-Origin" not in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_allows_configured_origin(self):
        import aiohttp

        port = self._unused_port()
        origin = "https://voice.example.com"
        config = WebRTCTransportConfig(
            host="127.0.0.1",
            port=port,
            cors_allowed_origins=(origin,),
        )
        transport = WebRTCTransport(config)
        await transport.connect()

        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"http://127.0.0.1:{port}/config",
                headers={"Origin": origin},
            ) as resp,
        ):
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == origin

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_wildcard_requires_explicit_opt_in(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(
            host="127.0.0.1",
            port=port,
            cors_allowed_origins=("*",),
        )
        transport = WebRTCTransport(config)
        await transport.connect()

        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"http://127.0.0.1:{port}/config",
                headers={"Origin": "https://voice.example.com"},
            ) as resp,
        ):
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == "*"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_receive_audio_ends_on_disconnect(self):
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                chunks.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await transport.disconnect()
        await asyncio.wait_for(task, timeout=2.0)
        # Should have exited cleanly.

    @pytest.mark.asyncio
    async def test_send_audio_no_peer(self):
        """send_audio reports False when no peer is connected."""
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunk = make_chunk()
        delivered = await transport.send_audio(chunk)
        assert delivered is False

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_reports_drop_after_peer_disconnect(self):
        """After the peer connection drops, send_audio must return False so
        the session stops emitting AudioOut for audio no one will hear."""
        transport = WebRTCTransport()
        # Pretend a peer connected: populate the fields that gate send_audio.
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()

        chunk = make_chunk()
        # With a live track, send_audio accepts the chunk.
        delivered_while_live = await transport.send_audio(chunk)
        assert delivered_while_live is True

        # Simulate the connectionstatechange handler's "disconnected" branch.
        transport._outbound_track = None

        delivered_after_drop = await transport.send_audio(chunk)
        assert delivered_after_drop is False


class TestConsumeAudioSentinel:
    """Verify that _consume_audio enqueues a sentinel when the track ends."""

    @pytest.mark.asyncio
    async def test_track_recv_raises_stops_receive_audio(self):
        """When track.recv() raises, _consume_audio's finally block enqueues
        a sentinel so that receive_audio() terminates instead of blocking."""
        transport = WebRTCTransport(WebRTCTransportConfig())
        transport._init_audio_queue(200)
        transport._connected = True

        # Fake track whose recv() signals end-of-stream immediately.
        class _FakeTrack:
            async def recv(self):
                raise StopAsyncIteration

        # Run _consume_audio — it should enqueue a sentinel via the finally block.
        await transport._consume_audio(_FakeTrack())

        # receive_audio() should now terminate promptly.
        chunks: list[AudioChunk] = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_sentinel_delivered_when_queue_is_full(self):
        """Even when the inbound queue is full, the sentinel must be delivered
        so that receive_audio() does not block forever."""
        transport = WebRTCTransport(WebRTCTransportConfig(max_pending_chunks=2))
        transport._init_audio_queue(2)
        transport._connected = True

        # Fill the queue completely.
        for _ in range(2):
            transport._enqueue_chunk(make_chunk(), context="test")

        # Fake track that ends immediately.
        class _FakeTrack:
            async def recv(self):
                raise StopAsyncIteration

        await transport._consume_audio(_FakeTrack())

        # receive_audio() must still terminate (sentinel was force-enqueued).
        chunks: list[AudioChunk] = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        # One chunk was dropped to make room for the sentinel; at most 1 chunk.
        assert len(chunks) <= 2


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *args: object, **kwargs: object) -> None:
        self.stopped.set()


def _fake_serve_web(
    *,
    start_error: Exception | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    router = SimpleNamespace(
        add_post=Mock(),
        add_get=Mock(),
        add_options=Mock(),
        add_static=Mock(),
    )
    app = SimpleNamespace(router=router)
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    site = SimpleNamespace(
        start=AsyncMock(side_effect=start_error),
        stop=AsyncMock(),
    )
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(return_value=site),
    )
    return web, runner, site


@pytest.mark.asyncio
async def test_serve_webrtc_config_sessions_cleans_runner_on_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat._extras as extras_module

    web, runner, site = _fake_serve_web(start_error=RuntimeError("port busy"))
    monkeypatch.setattr(extras_module, "require_module", lambda *_args, **_kwargs: web)

    with pytest.raises(RuntimeError, match="port busy"):
        await serve_webrtc_config_sessions(
            lambda _transport: {},
            WebRTCTransportConfig(static_dir=None),
            announce=False,
        )

    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
    site.stop.assert_not_awaited()
    runner.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_serve_webrtc_config_sessions_bounds_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import easycat._extras as extras_module
    import easycat.server.transports as transports_module
    import easycat.session_manager as manager_module

    web, runner, site = _fake_serve_web()
    monkeypatch.setattr(extras_module, "require_module", lambda *_args, **_kwargs: web)
    drain_calls: list[tuple[float, bool, float | None]] = []

    async def record_drain(
        self: object,
        sessions_for_keys: Callable[[], Iterable[tuple[object, object]]],
        *,
        drain_timeout_s: float,
        force_after: bool,
        force_timeout_s: float | None,
        stop_for_key: Callable[[object, bool], object] | None = None,
    ) -> None:
        assert tuple(sessions_for_keys()) == ()
        assert stop_for_key is not None
        drain_calls.append((drain_timeout_s, force_after, force_timeout_s))

    async def hanging_stop_all(_self: object, *, force: bool = False) -> None:
        assert force
        await asyncio.Event().wait()

    monkeypatch.setattr(transports_module.CapacityGate, "drain", record_drain)
    monkeypatch.setattr(manager_module.SessionManager, "stop_all", hanging_stop_all)
    stop_event = asyncio.Event()
    stop_event.set()

    with caplog.at_level(logging.WARNING):
        await serve_webrtc_config_sessions(
            lambda _transport: {},
            WebRTCTransportConfig(static_dir=None),
            stop_event=stop_event,
            announce=False,
            drain_timeout_s=0.25,
            force_shutdown_timeout_s=0.01,
        )

    assert drain_calls == [(0.25, True, 0.01)]
    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert "abandoning final sweep" in caplog.text


@pytest.mark.asyncio
async def test_serve_webrtc_config_sessions_drains_after_listener_stop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat._extras as extras_module
    import easycat.server.transports as transports_module

    web, runner, site = _fake_serve_web()
    site.stop = AsyncMock(side_effect=RuntimeError("listener stop failed"))
    monkeypatch.setattr(extras_module, "require_module", lambda *_args, **_kwargs: web)
    drain_called = False

    async def record_drain(
        self: object,
        sessions_for_keys: Callable[[], Iterable[tuple[object, object]]],
        **_kwargs: object,
    ) -> None:
        nonlocal drain_called
        assert tuple(sessions_for_keys()) == ()
        drain_called = True

    monkeypatch.setattr(transports_module.CapacityGate, "drain", record_drain)
    stop_event = asyncio.Event()
    stop_event.set()

    with pytest.raises(RuntimeError, match="listener stop failed"):
        await serve_webrtc_config_sessions(
            lambda _transport: {},
            WebRTCTransportConfig(static_dir=None),
            stop_event=stop_event,
            announce=False,
        )

    assert drain_called is True
    runner.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_shutdown_surfaces_failed_session_report_and_retains_ledger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from easycat.server.webrtc_routes import _shutdown_standalone_webrtc
    from easycat.session_manager import SessionStopFailure, SessionStopReport

    class _Gate:
        def start_draining(self) -> None:
            pass

        async def drain(self, *_args: object, **_kwargs: object) -> None:
            pass

    failure = RuntimeError("webrtc session teardown failed")
    report = SessionStopReport(
        attempted_keys=(41,),
        stopped_keys=(),
        failures=(SessionStopFailure(key=41, exception=failure),),
    )
    manager = SimpleNamespace(stop_all=AsyncMock(return_value=report))
    retained_session = object()
    active_sessions = {41: retained_session}

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(
            RuntimeError,
            match="Standalone WebRTC shutdown retained 1 session",
        ),
    ):
        await _shutdown_standalone_webrtc(
            site=SimpleNamespace(stop=AsyncMock()),
            runner=SimpleNamespace(cleanup=AsyncMock()),
            gate=_Gate(),  # type: ignore[arg-type]
            active_sessions=active_sessions,
            routes=SimpleNamespace(
                _stop_managed_session=AsyncMock(),
                cancel_cleanup_tasks=AsyncMock(),
            ),
            manager=manager,
            drain_timeout_s=0.0,
            force_shutdown_timeout_s=0.1,
        )

    assert "Standalone WebRTC session shutdown failed to stop 1 of 1 session" in caplog.text
    assert "webrtc session teardown failed" in caplog.text
    assert active_sessions == {41: retained_session}
    manager.stop_all.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_standalone_shutdown_preserves_drain_cancellation_after_listener_error() -> None:
    from easycat.server.webrtc_routes import _shutdown_standalone_webrtc

    class _Gate:
        def start_draining(self) -> None:
            pass

        async def drain(self, *_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError

    site = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("listener failed")))
    runner = SimpleNamespace(cleanup=AsyncMock())
    routes = SimpleNamespace(
        _stop_managed_session=AsyncMock(),
        cancel_cleanup_tasks=AsyncMock(),
    )
    manager = SimpleNamespace(stop_all=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await _shutdown_standalone_webrtc(
            site=site,
            runner=runner,
            gate=_Gate(),  # type: ignore[arg-type]
            active_sessions={},
            routes=routes,
            manager=manager,
            drain_timeout_s=0.0,
            force_shutdown_timeout_s=0.1,
        )

    routes.cancel_cleanup_tasks.assert_awaited_once()
    manager.stop_all.assert_awaited_once_with(force=True)


@pytest.mark.integration_socket
@pytest.mark.skipif(not _HAS_AIOHTTP, reason="aiohttp not installed")
class TestWebRTCConfigServer(_UsesPytestTcpPortFactory):
    @pytest.mark.asyncio
    async def test_non_loopback_without_token_is_rejected(self) -> None:
        """Binding beyond loopback without a token raises before any I/O setup."""
        with pytest.raises(ValueError, match="without a token"):
            await serve_webrtc_config_sessions(
                lambda transport: {},
                WebRTCTransportConfig(host="0.0.0.0", auth_token=None),
            )

    @pytest.mark.asyncio
    async def test_unsafe_allow_no_auth_passes_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``unsafe_allow_no_auth=True`` lets a non-loopback unauthenticated bind
        get past the guard (proven by reaching the telephony-extra import seam)."""
        import easycat._extras as extras_module

        def _reached(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("reached require_module past the auth guard")

        monkeypatch.setattr(extras_module, "require_module", _reached)
        with pytest.raises(RuntimeError, match="reached require_module"):
            await serve_webrtc_config_sessions(
                lambda transport: {},
                WebRTCTransportConfig(host="0.0.0.0", auth_token=None),
                unsafe_allow_no_auth=True,
            )

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_creates_session_per_offer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        sessions: list[_FakeSession] = []
        transports: list[WebRTCTransport] = []

        def create_session(config: dict[str, object]) -> _FakeSession:
            session = _FakeSession()
            sessions.append(session)
            return session

        def config_factory(transport: WebRTCTransport) -> dict[str, object]:
            transports.append(transport)
            return {"transport": transport, "agent": object()}

        monkeypatch.setattr(config_module, "create_session", create_session)
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                config_factory,
                WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=None),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for _ in range(2):
                    for attempt in range(20):
                        try:
                            async with client.post(
                                f"http://127.0.0.1:{port}/offer",
                                json={"sdp": "v=0\r\n", "type": "offer"},
                            ) as resp:
                                assert resp.status == 200
                                data = await resp.json()
                                assert data == {"sdp": "fake-answer", "type": "answer"}
                                break
                        except aiohttp.ClientConnectorError:
                            if attempt == 19:
                                raise
                            await asyncio.sleep(0.05)
            assert len(sessions) == 2
            assert len(transports) == 2
            assert transports[0] is not transports[1]
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
            await asyncio.wait_for(sessions[1].started.wait(), timeout=1)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        assert all(session.stopped.is_set() for session in sessions)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_enforces_session_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        sessions: list[_FakeSession] = []

        def create_session(config: dict[str, object]) -> _FakeSession:
            session = _FakeSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(config_module, "create_session", create_session)
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(
                    host="127.0.0.1", port=port, static_dir=None, max_sessions=1
                ),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.post(
                            f"http://127.0.0.1:{port}/offer",
                            json={"sdp": "v=0\r\n", "type": "offer"},
                        ) as resp:
                            assert resp.status == 200
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
                async with client.post(
                    f"http://127.0.0.1:{port}/offer",
                    json={"sdp": "v=0\r\n", "type": "offer"},
                ) as resp:
                    assert resp.status == 503
                    data = await resp.json()
                    assert "session limit" in data["error"]
            assert len(sessions) == 1
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_rejects_bad_offer_before_session_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        sessions: list[_FakeSession] = []

        def create_session(config: dict[str, object]) -> _FakeSession:
            session = _FakeSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(config_module, "create_session", create_session)
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=None),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.post(
                            f"http://127.0.0.1:{port}/offer",
                            json={"sdp": "", "type": "offer"},
                        ) as resp:
                            assert resp.status == 400
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
            assert sessions == []
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_health_reports_capacity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        monkeypatch.setattr(config_module, "create_session", lambda _config: _FakeSession())
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(
                    host="127.0.0.1", port=port, static_dir=None, max_sessions=7
                ),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.get(f"http://127.0.0.1:{port}/health") as resp:
                            assert resp.status == 200
                            data = await resp.json()
                            assert data == {
                                "status": "ok",
                                "active_sessions": 0,
                                "max_sessions": 7,
                            }
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_allows_config_cors_preflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        monkeypatch.setattr(config_module, "create_session", lambda _config: _FakeSession())
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(
                    host="127.0.0.1",
                    port=port,
                    static_dir=None,
                    auth_token="secret",
                    cors_allowed_origins=("https://app.example.com",),
                ),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.options(
                            f"http://127.0.0.1:{port}/config",
                            headers={
                                "Origin": "https://app.example.com",
                                "Access-Control-Request-Headers": "authorization",
                            },
                        ) as resp:
                            assert resp.status == 200
                            assert resp.headers["Access-Control-Allow-Origin"] == (
                                "https://app.example.com"
                            )
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_non_loopback_requires_token(self) -> None:
        # The serve helper now routes through the shared structured bind guard
        # (closing the asymmetry: it previously raised unconditionally with no
        # escape hatch). A non-loopback bind with no token raises with the
        # unified wording (host + ``unsafe_allow_no_auth``).
        with pytest.raises(ValueError) as exc:
            await serve_webrtc_config_sessions(
                lambda _t: {"agent": object()},
                WebRTCTransportConfig(host="0.0.0.0", auth_token=None),
                runtime_feedback=False,
                announce=False,
            )
        message = str(exc.value)
        assert "0.0.0.0" in message
        assert "unsafe_allow_no_auth" in message

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_non_loopback_unsafe_escape_hatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``unsafe_allow_no_auth=True`` lets a non-loopback unauthenticated bind
        # through (mirrors the WebSocket serve helper's escape hatch).
        _install_fake_webrtc_modules(monkeypatch)
        stop_event = asyncio.Event()
        stop_event.set()

        # Should not raise even though host is non-loopback and there is no token.
        await serve_webrtc_config_sessions(
            lambda _t: {"agent": object()},
            WebRTCTransportConfig(host="0.0.0.0", port=0, static_dir=None, auth_token=None),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
            unsafe_allow_no_auth=True,
        )


class TestWebRTCDegradedEvents:
    """SDP negotiation failure and inbound-track crash must surface a
    ``TransportDegraded`` so they land in the journal, not just the log."""

    @pytest.mark.asyncio
    async def test_negotiation_failure_emits_non_fatal_and_coalesces(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)

        async def _boom(self) -> None:
            raise RuntimeError("sdp boom")

        monkeypatch.setattr(_FakeRTCPeerConnection, "createAnswer", _boom)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see ingress tests)
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        # A client looping malformed SDP must not flood the journal with one
        # fatal event per request: the failure is recoverable, so the emit is
        # non-fatal and subject to the 1.0s coalescing window.
        for _ in range(3):
            resp = await transport._handle_offer(_FakeOfferRequest())
            assert resp.status == 400
            for _ in range(5):
                await asyncio.sleep(0)

        assert [e.reason for e in received] == [_DEGRADED_NEGOTIATION_FAILED]
        assert received[0].provider == "webrtc"
        assert received[0].fatal is False
        assert transport._degraded_suppressed.get((_DEGRADED_NEGOTIATION_FAILED, False), 0) == 2

    @pytest.mark.asyncio
    async def test_inbound_consume_error_emits_degraded(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        class _BadTrack:
            async def recv(self):
                raise RuntimeError("decode boom")

        await transport._consume_audio(_BadTrack(), peer_generation=transport._peer_generation)

        for _ in range(5):
            await asyncio.sleep(0)
        evt = next(e for e in received if e.reason == _DEGRADED_INBOUND_CONSUME_ERROR)
        assert evt.provider == "webrtc"
        assert evt.fatal is False

    @pytest.mark.asyncio
    async def test_outbound_queue_full_emits_degraded(self):
        """A dropped outbound TTS frame must surface a ``TransportDegraded`` so
        backpressure is visible in the journal, not just a logger.debug line."""
        transport = WebRTCTransport()
        # Pretend a peer connected so send_audio reaches the enqueue path.
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        # Force the outbound source to always reject the frame as if full.
        transport._outbound.enqueue = lambda *a, **k: False  # type: ignore[method-assign]

        delivered = await transport.send_audio(make_chunk())
        assert delivered is False

        for _ in range(5):
            await asyncio.sleep(0)
        evt = next(e for e in received if e.reason == _DEGRADED_OUTBOUND_QUEUE_FULL)
        assert evt.provider == "webrtc"
        assert evt.fatal is False
