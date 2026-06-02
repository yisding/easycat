"""TransportDegraded emission across transports.

Covers the shared ``_AudioQueueMixin`` emit seam (inbound queue-full +
provider tagging + no-bus / no-loop no-ops) and the WebSocket-specific
degradation paths.  WebRTC-specific paths live in ``test_webrtc.py`` (they
need the fake-aiortc harness); WebTransport's are in
``test_webtransport.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from easycat.events import EventBus, TransportDegraded
from easycat.transports._base import (
    _DEGRADED_EMIT_MIN_INTERVAL_SECONDS,
    _DEGRADED_INBOUND_QUEUE_FULL,
    _DEGRADED_MAX_DETAIL_CHARS,
    _AudioQueueMixin,
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


# ── Shared _AudioQueueMixin seam ──────────────────────────────────


class _MixinHarness(_AudioQueueMixin):
    transport_kind = "harness"

    def __init__(self, max_pending: int) -> None:
        self._init_audio_queue(max_pending)


class TestSharedEmitSeam:
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


# ── WebSocket-specific paths ──────────────────────────────────────


class _FakeServerWS:
    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


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
