"""Shared transport protocol and teardown conformance tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from easycat.events import CallAnswered, EventBus, TransportDegraded
from easycat.transports.local import LocalTransport
from easycat.transports.twilio_media import TwilioConnectionTransport, TwilioTransport
from easycat.transports.webrtc import WebRTCTransport
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketTransport,
)
from easycat.transports.webtransport import WebTransportConnectionTransport

from .conftest import make_chunk

_make_chunk = make_chunk


class _FakeServerWS:
    """Minimal stand-in for a live server WebSocket connection."""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class _RemoteEOFWebSocket:
    """Minimal connection that reaches remote EOF without local teardown."""

    def __init__(self, messages: tuple[str, ...] = ()) -> None:
        self._messages = iter(messages)
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None

    async def send(self, _message: str | bytes) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls += 1


def _twilio_start_msg() -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": "MZ123",
            "start": {"streamSid": "MZ123", "callSid": "CA456"},
        }
    )


def _make_connection_transports() -> dict[str, Any]:
    """Build one bare instance of every transport that owns a ``disconnect``."""

    return {
        "websocket_server": WebSocketTransport(),
        "websocket_connection": WebSocketConnectionTransport(
            _FakeServerWS(),  # type: ignore[arg-type]
        ),
        "webrtc": WebRTCTransport(),
        "webtransport_connection": WebTransportConnectionTransport(),
        "local": LocalTransport(),
    }


class TestEmitTaskDrain:
    """Fire-and-forget _emit_degraded tasks are drained on disconnect."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport_name", sorted(_make_connection_transports()))
    async def test_disconnect_drains_pending_emit_tasks(self, transport_name):
        from easycat.events import TransportDegraded

        transport = _make_connection_transports()[transport_name]
        bus = EventBus()
        seen: list[TransportDegraded] = []

        async def slow_handler(event):
            await asyncio.sleep(0)
            seen.append(event)

        bus.subscribe(TransportDegraded, slow_handler)
        transport._event_bus = bus
        transport._connected = True  # let disconnect() run its teardown

        transport._emit_degraded("inbound_queue_full", "dropped a frame")
        assert transport._emit_tasks  # task is scheduled but not yet awaited

        await transport.disconnect()

        # disconnect() awaited the pending emit, so nothing dangles.
        assert transport._emit_tasks == set()
        assert len(seen) == 1


class TestRemoteFirstDisconnect:
    @pytest.mark.asyncio
    async def test_websocket_disconnect_releases_resources_after_remote_eof(self):
        ws = _RemoteEOFWebSocket()
        bus = EventBus()
        degraded: list[TransportDegraded] = []

        async def capture_degraded(event: TransportDegraded) -> None:
            await asyncio.sleep(0)
            degraded.append(event)

        bus.subscribe(TransportDegraded, capture_degraded)
        transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]
        transport._event_bus = bus
        await transport.connect()
        receive_task = transport._receive_task
        assert receive_task is not None
        await receive_task
        assert not transport.is_connected

        forwarder = transport._browser_event_forwarder
        assert forwarder is not None
        transport._emit_degraded("audit_remote_eof")
        assert transport._emit_tasks

        await transport.disconnect()

        assert transport._receive_task is None
        assert transport._browser_event_forwarder is None
        assert not forwarder._subscriptions
        assert ws.close_calls == 1
        assert transport._emit_tasks == set()
        assert len(degraded) == 1

        await transport.disconnect()
        assert ws.close_calls == 1

    @pytest.mark.asyncio
    async def test_twilio_disconnect_releases_resources_after_remote_eof(self):
        ws = _RemoteEOFWebSocket((_twilio_start_msg(),))
        bus = EventBus()
        degraded: list[TransportDegraded] = []

        async def capture_degraded(event: TransportDegraded) -> None:
            await asyncio.sleep(0)
            degraded.append(event)

        bus.subscribe(TransportDegraded, capture_degraded)
        transport = TwilioConnectionTransport(  # type: ignore[arg-type]
            ws,
            event_bus=bus,
        )
        await transport.connect()
        receive_task = transport._receive_task
        assert receive_task is not None
        await receive_task
        assert not transport.is_connected
        assert transport._call_identity is not None
        assert transport._call_ended_emitted

        transport._emit_degraded("audit_remote_eof")
        assert transport._emit_tasks

        await transport.disconnect()

        assert transport._receive_task is None
        assert transport._call_identity is None
        assert not transport._call_ended_emitted
        assert ws.close_calls == 1
        assert transport._emit_tasks == set()
        assert len(degraded) == 1

        await transport.disconnect()
        assert ws.close_calls == 1

    @pytest.mark.asyncio
    async def test_twilio_self_disconnect_does_not_rearm_call_ended_latch(self):
        ws = _RemoteEOFWebSocket((_twilio_start_msg(),))
        bus = EventBus()
        transport = TwilioConnectionTransport(  # type: ignore[arg-type]
            ws,
            event_bus=bus,
        )

        async def disconnect_on_answered(_event: CallAnswered) -> None:
            await asyncio.sleep(0)
            await transport.disconnect()

        bus.subscribe(CallAnswered, disconnect_on_answered)
        await transport.connect()
        receive_task = transport._receive_task
        assert receive_task is not None
        await receive_task

        assert transport._receive_task is None
        assert not transport.is_connected
        assert not transport._call_ended_emitted
        assert ws.close_calls == 1

        await transport.disconnect()
        assert ws.close_calls == 1


class TestTransportConformance:
    """Verify all transports satisfy the Transport protocol shape."""

    def _assert_has_protocol_methods(self, t: Any) -> None:
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    def test_local_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(LocalTransport())

    def test_websocket_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(WebSocketTransport())

    def test_twilio_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(TwilioTransport())

    def test_webrtc_transport_has_protocol_methods(self):
        self._assert_has_protocol_methods(WebRTCTransport())

    def test_local_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(LocalTransport(), Transport)

    def test_websocket_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(WebSocketTransport(), Transport)

    def test_twilio_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(TwilioTransport(), Transport)

    def test_webrtc_transport_is_transport(self):
        from easycat.providers import Transport

        assert isinstance(WebRTCTransport(), Transport)
