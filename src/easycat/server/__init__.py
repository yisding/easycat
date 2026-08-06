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
  WebSocket gap. (M5 shipped a metrics SKELETON in ``server/metrics.py``; M8
  registers and emits the metrics — see below.)
* M6a/M6b add the manifest loader and provider planner.
* M7 mounts the WebRTC routes under ``/webrtc/*`` via the
  lifted transport-instance-free :class:`~easycat.server.webrtc_routes.WebRTCRoutes`
  unit (the route handlers no longer require a throwaway ``WebRTCTransport``
  shim). Capacity / draining now span WebRTC offers AND ``/ws`` connections
  through the SAME shared :class:`CapacityGate`. The standalone serve helpers
  (``serve_webrtc_config_sessions`` / ``run_webrtc_config_server``) delegate to
  the same unit with flat (``""``) routes.
* M8 (this milestone) registers the five ``easycat.server.*`` metrics in
  ``METRIC_DEFINITIONS`` and the three new labels in
  ``LOW_CARDINALITY_ATTRIBUTE_KEYS`` (in ``_observability.py``, in the SAME
  change that emits them), wires emission through ``server/metrics.py`` (the
  ``sanitize_attributes`` path; ``easycat.route`` asserted in an enumerated
  route-template set), and completes the read-only endpoints ``GET /metrics``,
  ``/manifest``, ``/plan``, and ``/capabilities``. No resolved token ever
  appears in a ``/manifest`` or ``/plan`` dump.

These are *submodule* exports (``easycat.server.*``); they do NOT count against
the top-level ``easycat.__all__`` cap. Imports here stay light: aiohttp is gated
inside :meth:`VoiceServer.start` (via :func:`easycat._extras.require_module`)
and ``auth.py`` pulls only ``hmac``/``dataclasses``/``typing``/``os`` plus the
leaf ``is_loopback_host`` (from :mod:`easycat._net`), so importing this package
never pulls aiohttp and never touches the planner or observability allow-lists.
:class:`WebRTCRoutes` is
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
    authorized_bind,
    bearer_auth_from_env,
    enforce_bind_guard,
)
from easycat.server.config import VoiceServerConfig
from easycat.server.health import VoiceServerHealth
from easycat.server.transports import CapacityGate
from easycat.server.voice_server import VoiceServer

if TYPE_CHECKING:
    from easycat.server.webrtc_routes import (
        WebRTCRoutes,
        run_webrtc_config_server,
        serve_webrtc_config_sessions,
    )
    from easycat.server.websocket import (
        run_websocket_config_server,
        serve_websocket_config_sessions,
        serve_websocket_sessions,
    )
    from easycat.server.webtransport import (
        run_webtransport_config_server,
        serve_webtransport_config_sessions,
    )

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
    "authorized_bind",
    "bearer_auth_from_env",
    "enforce_bind_guard",
    "run_webrtc_config_server",
    "run_websocket_config_server",
    "run_webtransport_config_server",
    "serve_webrtc_config_sessions",
    "serve_websocket_config_sessions",
    "serve_websocket_sessions",
    "serve_webtransport_config_sessions",
]

_LAZY_ATTRS = {
    "WebRTCRoutes": "easycat.server.webrtc_routes",
    "run_webrtc_config_server": "easycat.server.webrtc_routes",
    "serve_webrtc_config_sessions": "easycat.server.webrtc_routes",
    "run_websocket_config_server": "easycat.server.websocket",
    "serve_websocket_config_sessions": "easycat.server.websocket",
    "serve_websocket_sessions": "easycat.server.websocket",
    "run_webtransport_config_server": "easycat.server.webtransport",
    "serve_webtransport_config_sessions": "easycat.server.webtransport",
}


def __getattr__(name: str) -> object:
    """Lazily resolve server adapters without importing optional transports.

    Keeps ``import easycat.server`` from pulling WebRTC/WebTransport modules at
    package load; each adapter is imported only when accessed.
    """
    try:
        module_name = _LAZY_ATTRS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
