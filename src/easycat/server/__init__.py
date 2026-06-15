"""``easycat.server`` — the production process layer for ``VoiceApp``.

This package turns one or more :class:`~easycat.VoiceApp` instances into a
deployable server with health/readiness endpoints, capacity, graceful
shutdown, auth, and manifest/provider planning. It is a *submodule* export
(``import easycat.server``) and deliberately does NOT count against the
top-level ``easycat.__all__`` cap — only top-level ``VoiceApp`` does.

Milestone boundaries (Phase 2 "Neo"):

* M4 shipped :class:`VoiceServer`, :class:`VoiceServerConfig`, and
  :class:`VoiceServerHealth`: the aiohttp skeleton, the three health endpoints,
  a WebSocket route co-hosted as a raw :func:`websockets.serve` listener, and a
  minimal capacity counter for ``/health/ready``. It uses
  :class:`~easycat.session_manager.SessionManager` as a bare registry only.
* M5 (this milestone) adds the unified ``AuthPolicy`` layer
  (:class:`AuthPolicy` / :class:`AuthResult` / :class:`NoAuth` /
  :class:`BearerTokenAuth`) shared by WebSocket AND WebRTC, LIFTS the shared
  :class:`~easycat.server.transports.CapacityGate` (capacity + draining) out of
  the transport serve helpers, and implements graceful shutdown with force
  escalation. The unified bind guard closes the ``0.0.0.0`` unauthenticated
  WebSocket gap. Metrics are a SKELETON ONLY (``server/metrics.py``) — no
  ``easycat.server.*`` name is registered or emitted yet.
* M6a/M6b add the manifest loader and provider planner.
* M8 registers and emits ``easycat.server.*`` metrics.

These are *submodule* exports (``easycat.server.*``); they do NOT count against
the top-level ``easycat.__all__`` cap. Imports here stay light: aiohttp is gated
inside :meth:`VoiceServer.start` (via :func:`easycat._extras.require_module`)
and ``auth.py`` pulls only ``hmac``/``dataclasses``/``typing``/``os`` plus the
leaf ``_is_loopback_host``, so importing this package never pulls aiohttp and
never touches the planner or observability allow-lists.
"""

from __future__ import annotations

from easycat.server.auth import (
    AuthPolicy,
    AuthResult,
    BearerTokenAuth,
    NoAuth,
    bearer_auth_from_env,
    enforce_bind_guard,
)
from easycat.server.config import VoiceServerConfig
from easycat.server.health import VoiceServerHealth
from easycat.server.transports import CapacityGate
from easycat.server.voice_server import VoiceServer

__all__ = [
    "AuthPolicy",
    "AuthResult",
    "BearerTokenAuth",
    "CapacityGate",
    "NoAuth",
    "VoiceServer",
    "VoiceServerConfig",
    "VoiceServerHealth",
    "bearer_auth_from_env",
    "enforce_bind_guard",
]
