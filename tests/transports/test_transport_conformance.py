"""Shared transport protocol and teardown conformance tests."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import EventBus
from easycat.transports.local import LocalTransport
from easycat.transports.twilio_media import TwilioTransport
from easycat.transports.webrtc import WebRTCTransport
from easycat.transports.websocket import WebSocketTransport

from .conftest import make_chunk

_make_chunk = make_chunk


class TestEmitTaskDrain:
    """Fire-and-forget _emit_degraded tasks are drained on disconnect."""

    @pytest.mark.asyncio
    async def test_disconnect_drains_pending_emit_tasks(self):
        from easycat.events import TransportDegraded

        transport = WebSocketTransport()
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


class TestTransportConformance:
    """Verify all transports satisfy the Transport protocol shape."""

    def _assert_has_protocol_methods(self, t: object) -> None:
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
