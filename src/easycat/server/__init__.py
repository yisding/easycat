"""``easycat.server`` — the production process layer for ``VoiceApp``.

This package turns one or more :class:`~easycat.VoiceApp` instances into a
deployable server with health/readiness endpoints, capacity, graceful
shutdown, auth, and manifest/provider planning. It is a *submodule* export
(``import easycat.server``) and deliberately does NOT count against the
top-level ``easycat.__all__`` cap — only top-level ``VoiceApp`` does.

Milestone boundaries (Phase 2 "Neo"):

* M4 (this milestone) ships :class:`VoiceServer`, :class:`VoiceServerConfig`,
  and :class:`VoiceServerHealth`: the aiohttp skeleton, the three health
  endpoints, a WebSocket route co-hosted as a raw :func:`websockets.serve`
  listener, and a minimal capacity counter for ``/health/ready``. It uses
  :class:`~easycat.session_manager.SessionManager` as a bare registry only.
* M5 lifts the shared capacity/draining collaborator out of the transport
  serve helpers (``server/transports.py``) and adds the unified ``AuthPolicy``.
* M6a/M6b add the manifest loader and provider planner.
* M8 registers and emits ``easycat.server.*`` metrics.

Imports here stay light: aiohttp is gated inside :meth:`VoiceServer.start`
(via :func:`easycat._extras.require_module`) so importing this package never
pulls a heavy optional extra and never touches the planner or observability
allow-lists.
"""

from __future__ import annotations

from easycat.server.config import VoiceServerConfig
from easycat.server.health import VoiceServerHealth
from easycat.server.voice_server import VoiceServer

__all__ = [
    "VoiceServer",
    "VoiceServerConfig",
    "VoiceServerHealth",
]
