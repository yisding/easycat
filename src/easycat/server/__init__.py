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
* M5 adds the unified ``AuthPolicy`` layer (:class:`AuthPolicy` /
  :class:`AuthResult` / :class:`NoAuth` / :class:`BearerTokenAuth`) shared by
  WebSocket AND WebRTC, LIFTS the shared
  :class:`~easycat.server.transports.CapacityGate` (capacity + draining) out of
  the transport serve helpers, and implements graceful shutdown with force
  escalation. The unified bind guard closes the ``0.0.0.0`` unauthenticated
  WebSocket gap. Metrics are a SKELETON ONLY (``server/metrics.py``) — no
  ``easycat.server.*`` name is registered or emitted yet.
* M6a/M6b add the manifest loader and provider planner.
* M7 (this milestone) mounts the WebRTC routes under ``/webrtc/*`` via the
  lifted transport-instance-free :class:`~easycat.server.webrtc_routes.WebRTCRoutes`
  unit (the route handlers no longer require a throwaway ``WebRTCTransport``
  shim). Capacity / draining now span WebRTC offers AND ``/ws`` connections
  through the SAME shared :class:`CapacityGate`. The standalone serve helpers
  (``serve_webrtc_config_sessions`` / ``run_webrtc_config_server``) delegate to
  the same unit with flat (``""``) routes.
* M8 registers and emits ``easycat.server.*`` metrics.

These are *submodule* exports (``easycat.server.*``); they do NOT count against
the top-level ``easycat.__all__`` cap. Imports here stay light: aiohttp is gated
inside :meth:`VoiceServer.start` (via :func:`easycat._extras.require_module`)
and ``auth.py`` pulls only ``hmac``/``dataclasses``/``typing``/``os`` plus the
leaf ``_is_loopback_host``, so importing this package never pulls aiohttp and
never touches the planner or observability allow-lists. :class:`WebRTCRoutes` is
exported via a LAZY :func:`__getattr__` so importing it does not eagerly pull
``easycat.transports.webrtc`` (and thus aiohttp's siblings) at package load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from easycat.server.webrtc_routes import WebRTCRoutes

__all__ = [
    "AuthPolicy",
    "AuthResult",
    "BearerTokenAuth",
    "CapacityGate",
    "NoAuth",
    "VoiceServer",
    "VoiceServerConfig",
    "VoiceServerHealth",
    "WebRTCRoutes",
    "bearer_auth_from_env",
    "enforce_bind_guard",
]


def __getattr__(name: str) -> object:
    """Lazily resolve :class:`WebRTCRoutes` without eager-importing transports.

    Keeps ``import easycat.server`` from pulling ``easycat.transports.webrtc``
    (and aiohttp's transitive siblings) at package load; the route unit is only
    imported when actually accessed.
    """
    if name == "WebRTCRoutes":
        from easycat.server.webrtc_routes import WebRTCRoutes

        return WebRTCRoutes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
