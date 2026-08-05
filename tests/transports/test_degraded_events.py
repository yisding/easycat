"""TransportDegraded emission across transports.

Covers the shared ``AudioQueueMixin`` emit seam (inbound queue-full +
provider tagging + no-bus / no-loop no-ops) and the WebSocket-specific
degradation paths.  WebRTC-specific paths live in
``test_webrtc_lifecycle_server.py`` (they need the fake-aiortc harness);
WebTransport's are in ``test_webtransport_degraded_events.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets.exceptions
import websockets.frames

from easycat.events import EventBus, TransportDegraded
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.transports._base import (
    _DEGRADED_EMIT_MIN_INTERVAL_SECONDS,
    _DEGRADED_INBOUND_QUEUE_FULL,
    _DEGRADED_MAX_DETAIL_CHARS,
    AudioQueueMixin,
)
from easycat.transports.websocket import (
    _DEGRADED_CONTROL_DECODE_FAILED,
    _DEGRADED_EXTRA_CLIENT_REJECTED,
    _DEGRADED_INVALID_SAMPLE_RATE,
    WebSocketConnectionTransport,
    WebSocketTransport,
    WebSocketTransportConfig,
)

from .conftest import make_chunk


async def _drain_scheduled_emits() -> None:
    """Let the fire-and-forget ``bus.emit`` tasks run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


def _bus_with_collector() -> tuple[EventBus, list[TransportDegraded]]:
    bus = EventBus()
    received: list[TransportDegraded] = []
    bus.subscribe(TransportDegraded, lambda e: received.append(e))
    return bus, received


# ── Shared AudioQueueMixin seam ──────────────────────────────────


class _MixinHarness(AudioQueueMixin):
    transport_kind = "harness"

    def __init__(self, max_pending: int, max_pending_bytes: int | None = None) -> None:
        if max_pending_bytes is None:
            self._init_audio_queue(max_pending)
        else:
            self._init_audio_queue(max_pending, max_pending_bytes)


class TestSharedEmitSeam:
    @pytest.mark.asyncio
    async def test_attached_runtime_scope_finishes_transport_events(self) -> None:
        h = _MixinHarness(max_pending=1)
        root = RuntimeScope.create_root(
            name="session",
            root_id="test-root:transport-events",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        h.set_runtime_scope(root, name="transport-runtime")
        bus = EventBus()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _handler(_event: TransportDegraded) -> None:
            entered.set()
            await release.wait()

        bus.subscribe(TransportDegraded, _handler)
        h._event_bus = bus
        h._emit_degraded("test", "scope ownership")

        await entered.wait()
        assert h._emit_scope is not None
        assert h._emit_scope.parent is root
        assert h._emit_scope.name == "transport-runtime"
        assert root.tasks("transport_event_emit")

        closing = asyncio.create_task(root.close(phases=("transport-events",)))
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing

        assert not h._emit_tasks

    @pytest.mark.asyncio
    async def test_standalone_drain_releases_local_runtime_root(self) -> None:
        h = _MixinHarness(max_pending=1)
        bus, received = _bus_with_collector()
        h._event_bus = bus

        h._emit_degraded("test", "standalone ownership")
        assert h._owns_emit_root is True
        await h._drain_emit_tasks()

        assert [event.reason for event in received] == ["test"]
        assert h._emit_scope is None
        assert h._owns_emit_root is False

    @pytest.mark.asyncio
    async def test_enqueue_chunk_full_emits_inbound_queue_full(self) -> None:
        h = _MixinHarness(max_pending=1)
        bus, received = _bus_with_collector()
        h._event_bus = bus
        h._enqueue_chunk(make_chunk(), context="Harness")  # fills the 1 slot
        h._enqueue_chunk(make_chunk(), context="Harness")  # dropped
        await _drain_scheduled_emits()
        assert [e.reason for e in received] == [_DEGRADED_INBOUND_QUEUE_FULL]
        assert received[0].provider == "harness"  # from transport_kind
        assert received[0].fatal is False
        assert "Harness" in received[0].detail

    @pytest.mark.asyncio
    async def test_enqueue_chunk_byte_limit_drops_before_count_limit(self) -> None:
        h = _MixinHarness(max_pending=10, max_pending_bytes=5)
        bus, received = _bus_with_collector()
        h._event_bus = bus

        first = make_chunk(n_bytes=4)
        h._enqueue_chunk(first, context="Harness")
        h._enqueue_chunk(make_chunk(n_bytes=2), context="Harness")
        await _drain_scheduled_emits()

        assert h._in_queue.qsize() == 1
        assert h._in_queue.pending_bytes == 4
        assert [e.reason for e in received] == [_DEGRADED_INBOUND_QUEUE_FULL]

        assert await h._in_queue.get() is first
        assert h._in_queue.pending_bytes == 0

        h._enqueue_chunk(make_chunk(n_bytes=2), context="Harness")
        assert h._in_queue.qsize() == 1
        assert h._in_queue.pending_bytes == 2

    @pytest.mark.asyncio
    async def test_single_chunk_larger_than_byte_limit_is_dropped(self) -> None:
        h = _MixinHarness(max_pending=10, max_pending_bytes=5)
        bus, received = _bus_with_collector()
        h._event_bus = bus

        h._enqueue_chunk(make_chunk(n_bytes=6), context="Harness")
        await _drain_scheduled_emits()

        assert h._in_queue.empty()
        assert h._in_queue.pending_bytes == 0
        assert [e.reason for e in received] == [_DEGRADED_INBOUND_QUEUE_FULL]

    @pytest.mark.parametrize(
        ("max_pending", "max_pending_bytes"),
        [
            (0, 1),
            (-1, 1),
            (True, 1),
            (1, 0),
            (1, -1),
            (1, True),
        ],
    )
    def test_audio_queue_limits_must_be_positive_integers(
        self,
        max_pending: int,
        max_pending_bytes: int,
    ) -> None:
        with pytest.raises(ValueError, match="must be an integer >= 1"):
            _MixinHarness(max_pending, max_pending_bytes)

    @pytest.mark.asyncio
    async def test_no_bus_is_silent_noop(self) -> None:
        h = _MixinHarness(max_pending=1)
        h._enqueue_chunk(make_chunk(), context="Harness")
        h._enqueue_chunk(make_chunk(), context="Harness")  # would emit if a bus existed
        await _drain_scheduled_emits()
        assert not h._emit_tasks  # nothing scheduled

    def test_no_running_loop_is_silent_noop(self) -> None:
        # Synchronous context: get_running_loop() raises, emit must not.
        h = _MixinHarness(max_pending=1)
        bus, received = _bus_with_collector()
        h._event_bus = bus
        h._emit_degraded("anything", "no loop here")
        assert received == []
        assert not h._emit_tasks

    @pytest.mark.asyncio
    async def test_degraded_events_are_coalesced_per_reason(self) -> None:
        h = _MixinHarness(max_pending=1)
        bus, received = _bus_with_collector()
        h._event_bus = bus

        h._emit_degraded("inbound_queue_full", "first drop")
        h._emit_degraded("inbound_queue_full", "second drop")
        h._emit_degraded("control_decode_failed", "bad json")
        await _drain_scheduled_emits()
        assert [e.reason for e in received] == ["inbound_queue_full", "control_decode_failed"]
        assert h._degraded_suppressed[("inbound_queue_full", False)] == 1

        h._degraded_last_emit[("inbound_queue_full", False)] -= (
            _DEGRADED_EMIT_MIN_INTERVAL_SECONDS + 0.1
        )
        h._emit_degraded("inbound_queue_full", "later drop")
        await _drain_scheduled_emits()
        assert [e.reason for e in received] == [
            "inbound_queue_full",
            "control_decode_failed",
            "inbound_queue_full",
        ]
        assert "suppressed 1 similar events" in received[-1].detail

    @pytest.mark.asyncio
    async def test_degraded_detail_is_truncated_before_emit(self) -> None:
        h = _MixinHarness(max_pending=1)
        bus, received = _bus_with_collector()
        h._event_bus = bus

        h._emit_degraded("invalid_sample_rate", "x" * (_DEGRADED_MAX_DETAIL_CHARS + 25))
        await _drain_scheduled_emits()

        assert len(received) == 1
        assert len(received[0].detail) < _DEGRADED_MAX_DETAIL_CHARS + 50
        assert "truncated 25 chars" in received[0].detail

    @pytest.mark.asyncio
    async def test_suppression_count_survives_attacker_padded_detail(self) -> None:
        # A long, attacker-controlled detail must not evict the suppression
        # summary: truncate the detail first, then append the bounded count so
        # the "suppressed N similar events" tally always reaches the emit.
        h = _MixinHarness(max_pending=1)
        bus, received = _bus_with_collector()
        h._event_bus = bus

        padded = "x" * (_DEGRADED_MAX_DETAIL_CHARS * 4)
        # First emit goes through; subsequent emits inside the interval are
        # coalesced and bump the suppression counter.
        h._emit_degraded("invalid_sample_rate", padded)
        h._emit_degraded("invalid_sample_rate", padded)
        h._emit_degraded("invalid_sample_rate", padded)
        await _drain_scheduled_emits()

        assert len(received) == 1
        assert h._degraded_suppressed.get(("invalid_sample_rate", False), 0) == 2

        # Move the last-emit timestamp back so the next emit is allowed and
        # flushes the suppressed count alongside another padded detail.
        h._degraded_last_emit[("invalid_sample_rate", False)] -= (
            _DEGRADED_EMIT_MIN_INTERVAL_SECONDS + 0.1
        )
        h._emit_degraded("invalid_sample_rate", padded)
        await _drain_scheduled_emits()

        assert len(received) == 2
        emitted = received[-1].detail
        assert "suppressed 2 similar events" in emitted
        assert "truncated" in emitted

    @pytest.mark.asyncio
    async def test_emit_subscriber_can_drain_its_own_diagnostic_task(self) -> None:
        """A diagnostic callback may synchronously initiate local teardown."""
        h = _MixinHarness(max_pending=1)
        bus = EventBus()
        h._event_bus = bus
        drained = asyncio.Event()

        async def _handler(_event: TransportDegraded) -> None:
            await h._drain_emit_tasks()
            drained.set()

        bus.subscribe(TransportDegraded, _handler)
        h._emit_degraded("test", "self-drain")

        await asyncio.wait_for(drained.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert h._emit_tasks == set()


class _RaceServerWS:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.fail_sends = False

    async def send(self, message: str | bytes) -> None:
        if self.fail_sends:
            close_frame = websockets.frames.Close(1006, "abnormal")
            raise websockets.exceptions.ConnectionClosed(close_frame, None)
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class _RaceWebSocketTransport(WebSocketTransport):
    def __init__(self, old_ws: _RaceServerWS, new_ws: _RaceServerWS) -> None:
        super().__init__(WebSocketTransportConfig(port=0))
        self.old_ws = old_ws
        self.new_ws = new_ws
        self.old_receive_entered = asyncio.Event()
        self.new_receive_entered = asyncio.Event()
        self.release_old_receive = asyncio.Event()
        self.release_new_receive = asyncio.Event()

    async def _receive_loop(
        self,
        ws: object,
        _connection_generation: int | None = None,
    ) -> None:
        if ws is self.old_ws:
            self.old_receive_entered.set()
            await self.release_old_receive.wait()
        elif ws is self.new_ws:
            self.new_receive_entered.set()
            await self.release_new_receive.wait()
        else:  # pragma: no cover - defensive test harness guard
            raise AssertionError("unexpected websocket")


# ── WebSocket-specific paths ──────────────────────────────────────


class _FakeServerWS:
    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _ClosedServer:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        return None


class TestWebSocketDegradedEvents:
    @pytest.mark.asyncio
    async def test_extra_client_rejected_emits(self) -> None:
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        bus, received = _bus_with_collector()
        transport._event_bus = bus
        transport._ws = object()  # type: ignore[assignment] — pretend one client is live
        ws = _FakeServerWS()
        await transport._handle_connection(ws)  # type: ignore[arg-type]
        await _drain_scheduled_emits()
        assert ws.closed == (4000, "Only one session at a time")
        assert [e.reason for e in received] == [_DEGRADED_EXTRA_CLIENT_REJECTED]
        assert received[0].provider == "websocket"
        assert received[0].fatal is False

    @pytest.mark.asyncio
    async def test_control_decode_failed_emits(self) -> None:
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        bus, received = _bus_with_collector()
        transport._event_bus = bus
        transport._handle_control_message("}{ not json")
        await _drain_scheduled_emits()
        assert [e.reason for e in received] == [_DEGRADED_CONTROL_DECODE_FAILED]
        assert received[0].provider == "websocket"

    @pytest.mark.asyncio
    async def test_invalid_sample_rate_emits(self) -> None:
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        bus, received = _bus_with_collector()
        transport._event_bus = bus
        transport._handle_control_message(json.dumps({"type": "config", "sample_rate": -1}))
        await _drain_scheduled_emits()
        assert [e.reason for e in received] == [_DEGRADED_INVALID_SAMPLE_RATE]
        assert "-1" in received[0].detail

    @pytest.mark.asyncio
    async def test_emit_subscriber_can_disconnect_server_transport(self) -> None:
        """The diagnostic cleanup child must not await its initiating emitter."""
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        bus = EventBus()
        transport._event_bus = bus
        transport._connected = True
        transport._server = _ClosedServer()  # type: ignore[assignment]
        disconnected = asyncio.Event()

        async def _handler(_event: TransportDegraded) -> None:
            await transport.disconnect()
            disconnected.set()

        bus.subscribe(TransportDegraded, _handler)
        transport._emit_degraded("test", "disconnect from event observer")

        await asyncio.wait_for(disconnected.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert transport._emit_tasks == set()
        assert transport._disconnect_cleanup_pending is False

    @pytest.mark.asyncio
    async def test_concurrent_emitters_can_both_disconnect_server_transport(self) -> None:
        transport = WebSocketTransport(WebSocketTransportConfig(port=0))
        bus = EventBus()
        transport._event_bus = bus
        transport._connected = True
        transport._server = _ClosedServer()  # type: ignore[assignment]
        both_entered = asyncio.Event()
        finished = asyncio.Event()
        entered = 0
        completed = 0

        async def _handler(_event: TransportDegraded) -> None:
            nonlocal entered, completed
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await transport.disconnect()
            completed += 1
            if completed == 2:
                finished.set()

        bus.subscribe(TransportDegraded, _handler)
        transport._emit_degraded("first", "first concurrent observer")
        transport._emit_degraded("second", "second concurrent observer")

        await asyncio.wait_for(finished.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert transport._emit_tasks == set()
        assert transport._disconnect_cleanup_pending is False

    @pytest.mark.asyncio
    async def test_stale_connection_cleanup_does_not_clear_new_client(self) -> None:
        old_ws = _RaceServerWS()
        new_ws = _RaceServerWS()
        transport = _RaceWebSocketTransport(old_ws, new_ws)

        old_task = asyncio.create_task(transport._handle_connection(old_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(transport.old_receive_entered.wait(), timeout=1.0)

        old_ws.fail_sends = True
        assert await transport.send_audio(make_chunk()) is False
        assert transport._ws is None

        new_task = asyncio.create_task(transport._handle_connection(new_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(transport.new_receive_entered.wait(), timeout=1.0)
        assert transport._ws is new_ws

        transport.release_old_receive.set()
        await asyncio.wait_for(old_task, timeout=1.0)

        assert transport._ws is new_ws
        assert transport._client_connected.is_set()
        assert await transport.send_audio(make_chunk()) is True
        assert isinstance(new_ws.sent[-1], bytes)

        transport.release_new_receive.set()
        await asyncio.wait_for(new_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_replacement_client_resets_negotiated_format(self) -> None:
        # Same disconnect race as above, but the old client first negotiates a
        # non-default sample rate. When ``send_audio`` wins the race and clears
        # ``_ws``, the old handler's ``finally`` no longer owns the slot (the new
        # client has claimed it) so it cannot reset ``_audio_format``. The accept
        # path must reset it instead, otherwise the new client inherits the old
        # client's stale negotiated format.
        old_ws = _RaceServerWS()
        new_ws = _RaceServerWS()
        transport = _RaceWebSocketTransport(old_ws, new_ws)
        configured_format = transport._config.audio_format

        old_task = asyncio.create_task(transport._handle_connection(old_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(transport.old_receive_entered.wait(), timeout=1.0)

        # Old client negotiates a non-default rate.
        transport._handle_control_message(json.dumps({"type": "config", "sample_rate": 48000}))
        assert transport._audio_format.sample_rate == 48000
        assert transport._audio_format != configured_format

        old_ws.fail_sends = True
        assert await transport.send_audio(make_chunk()) is False
        assert transport._ws is None

        new_task = asyncio.create_task(transport._handle_connection(new_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(transport.new_receive_entered.wait(), timeout=1.0)
        assert transport._ws is new_ws
        # The replacement client must start from the configured default, not the
        # rate the orphaned old client negotiated.
        assert transport._audio_format == configured_format

        transport.release_old_receive.set()
        await asyncio.wait_for(old_task, timeout=1.0)

        # The stale old-handler teardown must not have clobbered the new client.
        assert transport._ws is new_ws
        assert transport._audio_format == configured_format

        transport.release_new_receive.set()
        await asyncio.wait_for(new_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_connection_transport_inbound_queue_full_emits(self) -> None:
        # The server-owns-accept-loop variant inherits the same seam.
        transport = WebSocketConnectionTransport(
            object(),  # type: ignore[arg-type] — ws unused on this path
            WebSocketTransportConfig(max_pending_chunks=1),
        )
        bus, received = _bus_with_collector()
        transport._event_bus = bus
        transport._enqueue_chunk(make_chunk(), context="WebSocket")
        transport._enqueue_chunk(make_chunk(), context="WebSocket")
        await _drain_scheduled_emits()
        assert [e.reason for e in received] == [_DEGRADED_INBOUND_QUEUE_FULL]
        assert received[0].provider == "websocket"
