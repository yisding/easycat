"""aiohttp health-route handlers and the enumerated route-template set.

The route handlers read live :class:`VoiceServer` state (via
:meth:`VoiceServer.health`) and never include session IDs, IPs, or tokens in
any response body.

``ROUTE_TEMPLATES`` is the single source of truth for the enumerated set of
route TEMPLATES that M8 will assert against before recording the
``easycat.route`` metric label (a raw path with user content must never be
recorded). M4 defines the set but records NO metric — there are no
``_record_metric`` / ``sanitize_attributes`` calls in this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from easycat.server.voice_server import VoiceServer

# Enumerated route TEMPLATES (never resolved/raw paths). ``/ws`` is a logical
# entry that is served by a raw ``websockets.serve`` listener, not an aiohttp
# route, but it belongs to the server's logical surface so it lives here too.
# M8 asserts a value is in this set before recording ``easycat.route``.
ROUTE_TEMPLATES: frozenset[str] = frozenset(
    {
        "/health/live",
        "/health/ready",
        "/health",
        "/ws",
    }
)


async def _handle_health_live(_request: Any) -> Any:
    """``GET /health/live`` — 200 if the loop can respond."""
    from aiohttp import web

    return web.json_response({"status": "ok"})


def _make_health_ready_handler(server: VoiceServer) -> Any:
    async def _handle_health_ready(_request: Any) -> Any:
        from aiohttp import web

        health = await server.health()
        if health.is_ready():
            return web.json_response({"status": "ok"})
        # Carry only the content-free failing reasons; never IDs/IPs/tokens.
        return web.json_response(
            {"status": "not_ready", "reasons": list(health.readiness_failures())},
            status=503,
        )

    return _handle_health_ready


def _make_health_handler(server: VoiceServer) -> Any:
    async def _handle_health(_request: Any) -> Any:
        from aiohttp import web

        health = await server.health()
        return web.json_response(health.to_payload())

    return _handle_health


def register_health_routes(app: Any, server: VoiceServer) -> None:
    """Wire the ``/health/live``, ``/health/ready``, and ``/health`` routes.

    ``app`` is an :class:`aiohttp.web.Application`. The handlers close over
    ``server`` so they read the live minimal capacity counter and draining
    flag at request time.
    """
    app.router.add_get("/health/live", _handle_health_live)
    app.router.add_get("/health/ready", _make_health_ready_handler(server))
    app.router.add_get("/health", _make_health_handler(server))
