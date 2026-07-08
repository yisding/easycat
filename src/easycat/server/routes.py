"""aiohttp read-only-route handlers and the enumerated route-template set.

The route handlers read live :class:`VoiceServer` state (via
:meth:`VoiceServer.health` / :meth:`VoiceServer.plan_payload` /
:meth:`VoiceServer.manifest_payload` / :meth:`VoiceServer.capabilities_payload`)
and never include session IDs, IPs, or tokens in any response body.

``ROUTE_TEMPLATES`` is the single source of truth for the enumerated set of
route TEMPLATES that the M8 metrics layer asserts against before recording the
``easycat.route`` metric label (a raw path with user content must never be
recorded). :func:`metrics_middleware` records ``easycat.server.requests.total``
/ ``easycat.server.request.duration`` keyed by the matched route TEMPLATE — it
resolves the template from the matched resource's canonical path and validates
it against :data:`ROUTE_TEMPLATES`, never ``request.path`` (which can carry
``?token=`` or user content).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from easycat.server.health import ServerState
    from easycat.server.voice_server import VoiceServer

# Enumerated route TEMPLATES (never resolved/raw paths). ``/ws`` is a logical
# entry that is served by a raw ``websockets.serve`` listener, not an aiohttp
# route, but it belongs to the server's logical surface so it lives here too.
# The M8 metrics layer asserts a value is in this set before recording
# ``easycat.route``.
ROUTE_TEMPLATES: frozenset[str] = frozenset(
    {
        "/health/live",
        "/health/ready",
        "/health",
        "/plan",
        # M8 read-only endpoints (aiohttp routes on the health listener).
        "/metrics",
        "/manifest",
        "/capabilities",
        "/ws",
        # M7: the namespaced WebRTC routes mounted by ``VoiceServer`` (the
        # Endpoint Set strings). These are aiohttp routes on the health listener,
        # never raw paths. The M8 metrics layer asserts a value is in this set
        # before recording ``easycat.route``.
        "/webrtc/offer",
        "/webrtc/config",
        "/webrtc/stats",
        "/webrtc/health",
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


def _auth_failure_response() -> Any:
    """Return the shared 401 response for protected read-only endpoints."""
    from aiohttp import web

    return web.json_response({"error": "Missing or invalid bearer token"}, status=401)


def _authorized_readonly_request(server: VoiceServer, request: Any) -> bool:
    """Authorize a read-only aiohttp request with the server auth policy.

    A server without an auth policy remains open (the loopback/dev default). When
    an auth policy is configured, metadata endpoints must require the same
    bearer credential as WebRTC signaling instead of exposing deployment details
    to unauthenticated clients.
    """
    if server.config.auth is None:
        return True
    from easycat.server.auth import from_aiohttp_request

    return server.config.auth.authorize(from_aiohttp_request(request)).allowed


def _make_plan_handler(server: VoiceServer) -> Any:
    """Build the ``GET /plan`` handler (read-only, redacted, side-effect-free).

    The planner (``easycat.planning``) is imported LAZILY inside the handler so
    the route registration — and ``import easycat.server`` — never pulls the
    planner at module load (the M4/M6b boundary). For a factory-only server with
    no manifest/profile, returns a documented empty plan (``selected={}``).
    """

    async def _handle_plan(request: Any) -> Any:
        from aiohttp import web

        auth = server.config.auth
        if auth is not None:
            from easycat.server.auth import from_aiohttp_request

            result = auth.authorize(from_aiohttp_request(request))
            if not result.allowed:
                return web.json_response({"error": "Missing or invalid bearer token"}, status=401)

        payload = server.plan_payload()
        return web.json_response(payload)

    return _handle_plan


def register_plan_route(app: Any, server: VoiceServer) -> None:
    """Wire the read-only ``GET /plan`` route (M6b).

    ``/plan`` is part of the logical surface and is a member of
    :data:`ROUTE_TEMPLATES` (so the M8 metrics layer may assert it before
    recording ``easycat.route``). The planner is imported lazily inside the
    handler so registration pulls no planner at module load.
    """
    app.router.add_get("/plan", _make_plan_handler(server))


def _make_metrics_handler(server: VoiceServer) -> Any:
    """Build the ``GET /metrics`` handler (read-only, PII-safe JSON snapshot).

    Ships JSON first (the Prometheus text exposition is a deferred Cut Line). The
    payload is a snapshot of the in-process server-side counters/gauges
    :class:`VoiceServer` tracks WITHOUT OTel (the ``easycat._observability``
    instruments are write-only / no-op without an SDK), so the numbers are stable
    in CI. NEVER includes session IDs, IPs, tokens, or raw paths.
    """

    async def _handle_metrics(request: Any) -> Any:
        from aiohttp import web

        if not _authorized_readonly_request(server, request):
            return _auth_failure_response()
        return web.json_response(server.metrics_payload())

    return _handle_metrics


def register_metrics_route(app: Any, server: VoiceServer) -> None:
    """Wire the read-only ``GET /metrics`` route (M8)."""
    app.router.add_get("/metrics", _make_metrics_handler(server))


def _make_manifest_handler(server: VoiceServer) -> Any:
    """Build the ``GET /manifest`` handler (read-only, redacted, no token).

    The payload routes through :meth:`ProjectManifest.to_redacted_dict`, which
    (1) carries only the ``bearer-env:NAME`` reference under ``*_ref`` keys (the
    resolved token is never stored on the manifest) and (2) routes every value
    through ``redact_value``. The handler never calls ``resolve_auth`` (which
    reads the env token); only the redacted dump is exposed.
    """

    async def _handle_manifest(request: Any) -> Any:
        from aiohttp import web

        if not _authorized_readonly_request(server, request):
            return _auth_failure_response()
        return web.json_response(server.manifest_payload())

    return _handle_manifest


def register_manifest_route(app: Any, server: VoiceServer) -> None:
    """Wire the read-only ``GET /manifest`` route (M8)."""
    app.router.add_get("/manifest", _make_manifest_handler(server))


def _make_capabilities_handler(server: VoiceServer) -> Any:
    """Build the ``GET /capabilities`` handler (read-only, no token).

    The payload aggregates the parity-gated planner's declared capability strings
    across the seven roles. The planner reads only provider metadata (names /
    extras / env-var NAMES / declared capability strings) — never secret values
    — so no token can appear. The planner is imported lazily inside
    :meth:`VoiceServer.capabilities_payload` (the M4 import boundary).
    """

    async def _handle_capabilities(request: Any) -> Any:
        from aiohttp import web

        if not _authorized_readonly_request(server, request):
            return _auth_failure_response()
        return web.json_response(server.capabilities_payload())

    return _handle_capabilities


def register_capabilities_route(app: Any, server: VoiceServer) -> None:
    """Wire the read-only ``GET /capabilities`` route (M8)."""
    app.router.add_get("/capabilities", _make_capabilities_handler(server))


def _matched_route_template(request: Any) -> str | None:
    """Return the matched route TEMPLATE for ``request``, or ``None``.

    Resolves the template from the matched resource's canonical path (e.g.
    ``/health/ready``), NOT from ``request.path`` (which can carry ``?token=`` or
    user content). Returns ``None`` when no resource matched (a 404) or the
    canonical path is not an enumerated template — the caller then records
    nothing, so a raw/unknown path can never become an ``easycat.route`` label.
    """
    match_info = getattr(request, "match_info", None)
    route = getattr(match_info, "route", None) if match_info is not None else None
    resource = getattr(route, "resource", None) if route is not None else None
    canonical = getattr(resource, "canonical", None) if resource is not None else None
    if canonical is None:
        return None
    if canonical not in ROUTE_TEMPLATES:
        return None
    return canonical


def metrics_middleware(server: VoiceServer) -> Any:
    """Build an aiohttp middleware that records per-request server metrics (M8).

    Records :data:`easycat.server.requests.total` + ``easycat.server.request.duration``
    keyed by the matched route TEMPLATE (resolved via :func:`_matched_route_template`,
    validated against :data:`ROUTE_TEMPLATES` — never ``request.path``). Emission is
    a no-op when metrics are disabled or no OTel SDK is configured; the registered
    names/labels cannot raise in ``sanitize_attributes``, so the call is not wrapped
    in a broad try/except.
    """
    from aiohttp import web

    from easycat.server import metrics as server_metrics

    @web.middleware
    async def _middleware(request: Any, handler: Any) -> Any:
        start = time.perf_counter()
        # Record in a ``finally`` so a handler that RAISES (a 500, or an aiohttp
        # ``HTTPException`` such as a redirect/404 propagated through the stack)
        # is still counted. Recording only after ``await handler(request)``
        # returns would systematically undercount exactly the error requests an
        # operator most wants to see in ``/metrics``.
        try:
            return await handler(request)
        finally:
            if server.config.enable_metrics:
                template = _matched_route_template(request)
                if template is not None:
                    duration_s = time.perf_counter() - start
                    state: ServerState = "draining" if server._gate.is_draining else "serving"
                    # In-process snapshot for ``GET /metrics`` (stable without an
                    # OTel SDK) alongside the registered OTel emission.
                    server._requests_total += 1
                    server_metrics.record_request(
                        template,
                        duration_s=duration_s,
                        server_state=state,
                    )

    return _middleware
