"""Placeholder for the M5 shared capacity/draining collaborator.

This module is intentionally behavior-free in M4. M5 LIFTS the inline
``asyncio.Semaphore`` / active-session-set / draining state out of the two
serve helpers (``transports/webrtc.py`` and ``transports/websocket.py``) into
a shared collaborator that lives here, so capacity and draining behave
identically across transports. The serve helpers then delegate to it.

M4 must NOT put the real ``Semaphore`` / active-set / draining lift here: the
M4 ``/health/ready`` capacity check reads a MINIMAL active-session counter
that lives inline in :class:`~easycat.server.voice_server.VoiceServer`. M5
replaces that counter with the lifted collaborator without changing the
readiness contract.

It also holds the small per-transport helper types (one per route mode:
``WebRTCTransport`` / ``WebSocketConnectionTransport`` /
``WebTransportConnectionTransport`` / ``TwilioConnectionTransport``); there is
NO unified ``ConnectionContext`` type — the per-connection seam is a
per-transport ``Callable[[TransportT], EasyConfig | Session]`` factory.
"""

from __future__ import annotations
