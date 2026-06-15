"""``VoiceServer`` — the M5 production process layer (aiohttp + raw WS).

Scope: lifecycle (``start`` / ``serve`` / ``stop`` / ``run`` / ``health``),
the three health endpoints, and a WebSocket ``/ws`` route co-hosted as a raw
:func:`websockets.serve` listener on its own port. M5 replaces the M4 minimal
inline counter with the shared :class:`~easycat.server.transports.CapacityGate`
collaborator (capacity + draining), applies the unified ``AuthPolicy`` to the
``/ws`` path (closing the ``0.0.0.0`` unauthenticated gap), and implements
graceful shutdown with force escalation. There is NO planner import and NO
metric emission yet (M6b / M8 respectively).

Event-loop ownership (one rule across ``VoiceApp`` and ``VoiceServer``):

* :meth:`run` is the ONLY method that calls :func:`asyncio.run` (sole loop
  owner).
* :meth:`serve` is the async verb; it never calls :func:`asyncio.run`.
* :meth:`from_app` composes a mounted :class:`~easycat.VoiceApp` via its
  per-transport ``config_factory`` ONLY; it NEVER calls ``VoiceApp.run()``
  (which would nest :func:`asyncio.run`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from easycat._extras import require_module
from easycat._signals import create_shutdown_event
from easycat.server.auth import from_websocket
from easycat.server.config import VoiceServerConfig
from easycat.server.health import VoiceServerHealth
from easycat.server.routes import register_health_routes, register_plan_route
from easycat.server.transports import CapacityGate
from easycat.session_manager import SessionManager

if TYPE_CHECKING:
    from pathlib import Path

    from easycat.config import EasyConfig
    from easycat.session import Session
    from easycat.voice_app import VoiceApp

logger = logging.getLogger(__name__)

# The per-connection seam is a per-transport factory. ``TransportT`` is the
# concrete per-route transport (``WebRTCTransport`` /
# ``WebSocketConnectionTransport`` / ``WebTransportConnectionTransport`` /
# ``TwilioConnectionTransport``) selected by the route's mode. There is NO
# unified ``ConnectionContext`` type. ``Any`` here stands in for the per-route
# ``TransportT``; M5+ narrow it per route.
SessionFactory = Callable[[Any], "EasyConfig | Session"]

# Close code mirroring ``serve_websocket_sessions``' "at the configured session
# limit" rejection (RFC 6455 1013 "Try Again Later").
_WS_OVER_CAPACITY_CLOSE_CODE = 1013
_WS_OVER_CAPACITY_CLOSE_REASON = "Server is at the configured session limit"
_WS_DRAINING_CLOSE_REASON = "Server is draining"
# Auth rejection uses 1008 (RFC 6455 "Policy Violation").
_WS_UNAUTHORIZED_CLOSE_CODE = 1008
_WS_UNAUTHORIZED_CLOSE_REASON = "Missing or invalid bearer token"


class VoiceServer:
    """A production process layer over one or more per-connection factories.

    M5 supports a single WebSocket ``/ws`` route built from a per-transport
    ``session_factory`` and co-hosts the aiohttp health endpoints. Capacity and
    draining are owned by the shared
    :class:`~easycat.server.transports.CapacityGate` collaborator (lifted out of
    the transport serve helpers); the unified ``AuthPolicy`` guards the bind and
    each ``/ws`` connection.
    """

    def __init__(
        self,
        config: VoiceServerConfig | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.config = config or VoiceServerConfig()
        self._session_factory = session_factory

        # Optional manifest source for the read-only ``/plan`` route and the M6b
        # readiness checks (the ``from_manifest`` path sets these; a factory-only
        # server leaves them ``None`` and reports an empty plan / skipped checks).
        self._manifest: Any = None
        self._manifest_load_error: str | None = None

        # Bare session registry only: add/remove/stop_all/connection. Capacity
        # and draining are NOT attributed to it (it has neither).
        self._manager: SessionManager[int] = SessionManager()

        # Shared capacity + draining collaborator (the M5 lift). It owns the
        # reservation counter, the active-connection set, and the draining flag
        # — identical to the WebRTC/WebSocket serve helpers.
        self._gate: CapacityGate[int] = CapacityGate(self.config.max_sessions)
        # Map active gate keys -> live session objects so the drain step can call
        # ``session.stop(force=True)`` directly (the collaborator drives this;
        # ``SessionManager`` has no draining state).
        self._active_session_objs: dict[int, Any] = {}

        # Route-stack references spanning BOTH listener kinds: the aiohttp
        # runner/site and the raw ``websockets.serve`` listener.
        self._runner: Any = None
        self._site: Any = None
        self._ws_server: Any = None
        self._started = False

    # ── Compatibility accessors (minimal-counter shims) ──────────────
    # The M4 minimal counter is replaced by the shared ``CapacityGate``; these
    # thin shims preserve the names the M4 tests read so the readiness contract
    # and accessors are unchanged.

    @property
    def _active_sessions(self) -> int:
        """Active-connection count (reads the shared gate's reservation count)."""
        return self._gate.reserved_count

    @property
    def _draining(self) -> bool:
        return self._gate.is_draining

    @_draining.setter
    def _draining(self, value: bool) -> None:
        # Tests flip this directly; route it onto the shared gate so the
        # readiness check and the ``/ws`` handler observe it identically.
        if value:
            self._gate.start_draining()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the aiohttp app + (optionally) the raw ``/ws`` listener.

        aiohttp is gated here via :func:`require_module` (the ``webrtc`` extra
        supplies it; there is no dedicated ``server`` extra) so a missing extra
        surfaces a clear, actionable error rather than an ``ImportError`` at
        package import time.
        """
        if self._started:
            return

        # Apply the unified non-loopback bind guard BEFORE binding any listener
        # (the same structured guard the transport serve helpers use). This is
        # the property that closes the ``0.0.0.0`` unauthenticated WebSocket gap:
        # a non-loopback bind with no token raises unless
        # ``unsafe_allow_no_auth=True``.
        from easycat.server.auth import enforce_bind_guard

        enforce_bind_guard(
            self.config.host,
            auth=self.config.auth,
            unsafe_allow_no_auth=self.config.unsafe_allow_no_auth,
        )

        web = require_module("aiohttp.web", extra="webrtc", purpose="VoiceServer")

        app = web.Application()
        if self.config.enable_health:
            register_health_routes(app, self)
        # The read-only ``/plan`` route (M6b). Registered unconditionally with
        # health so a factory-only server still answers with a documented empty
        # plan; the handler imports the planner LAZILY so registration pulls no
        # planner at module load.
        register_plan_route(app, self)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        self._runner = runner
        self._site = site

        if self.config.enable_websocket:
            self._ws_server = await self._start_websocket_listener()

        self._started = True
        logger.info(
            "VoiceServer ready: http://%s:%s (websocket=%s)",
            self.config.host,
            self.config.port,
            self.config.enable_websocket,
        )

    async def serve(self, stop_event: asyncio.Event | None = None) -> None:
        """Start, then run until ``stop_event`` fires, then stop.

        This is the async verb. It NEVER calls :func:`asyncio.run`; drive it
        from an existing loop (or from :meth:`run`, the sole loop owner). When
        ``stop_event`` is omitted a SIGINT/SIGTERM-backed shutdown event is
        created.
        """
        await self.start()
        event = stop_event or create_shutdown_event()
        try:
            await event.wait()
        finally:
            await self.stop()

    def run(self) -> None:
        """Synchronous entry point — the only :func:`asyncio.run` caller.

        ``run()`` is the sole loop owner across ``VoiceApp`` and
        ``VoiceServer``.
        """
        asyncio.run(self.serve())

    async def stop(self, *, force: bool = False) -> None:
        """Gracefully drain, then tear down both listener kinds.

        The graceful sequence (spanning the aiohttp listeners AND the raw-ws
        listener), driven by the shared :class:`CapacityGate`, NOT by
        ``SessionManager`` (which has no draining state):

        1. Set the draining flag (``gate.start_draining()``) so the readiness
           check and the ``/ws`` handler reject new connections.
        2. Stop accepting — ``gate.try_acquire()`` already rejects while
           draining and ``_handle_websocket_connection`` checks draining.
        3. Close the raw-ws listener AND the aiohttp site/runner (both kinds).
        4. Wait for active connection tasks up to ``drain_timeout_s``; each
           handler that ends in time calls ``session.stop()`` itself.
        5. Escalate any session still active after the grace window with
           ``session.stop(force=True)`` (driven by the gate's active set).
        6. ``SessionManager.stop_all()`` ONLY as the final hard sweep, once the
           listeners are closed and no handler can still add/remove sessions.

        ``force=True`` collapses the grace window to zero so remaining sessions
        are force-stopped immediately.
        """
        # (1) draining flag.
        self._gate.start_draining()

        # (3) close the listeners so no new handler task can start AND in-flight
        # ``/ws`` connections are closed (each handler's ``ws.wait_closed()``
        # returns). Do NOT ``await ws_server.wait_closed()`` yet: a hung handler
        # (graceful ``session.stop()`` that blocks) would deadlock it before the
        # drain can force-escalate. The aiohttp site/runner are torn down now;
        # the raw-ws server is closed now and awaited AFTER the drain.
        ws_server = self._ws_server
        if ws_server is not None:
            ws_server.close()
            self._ws_server = None
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

        # (4)+(5) wait for active connection tasks up to ``drain_timeout_s``,
        # then force-escalate the remainder. ``force=True`` skips the grace
        # window entirely. Running BEFORE ``ws_server.wait_closed()`` lets the
        # force-stop unblock any handler whose graceful stop is hung.
        drain_timeout = 0.0 if force else self.config.drain_timeout_s
        await self._gate.drain(
            self._active_session_pairs,
            drain_timeout_s=drain_timeout,
            force_after=True,
        )

        # Now all handlers can return (closed connections + forced sessions), so
        # awaiting the raw-ws server completes promptly.
        if ws_server is not None:
            await ws_server.wait_closed()

        # (6) final hard sweep of the bare registry.
        await self._manager.stop_all()
        self._active_session_objs.clear()
        self._started = False

    def _active_session_pairs(self) -> list[tuple[int, Any]]:
        """Return the ``(key, session)`` pairs still active (for the drain step)."""
        return [
            (key, self._active_session_objs[key])
            for key in self._gate.active_keys()
            if key in self._active_session_objs
        ]

    async def health(self) -> VoiceServerHealth:
        """Build a :class:`VoiceServerHealth` snapshot from live state.

        M4 owns the serving/draining/capacity/route-ready half. The M6b
        manifest-loaded + plan-no-blocking-errors checks are layered on ONLY for
        a ``from_manifest`` server (a factory-only server keeps the M4 ``skipped``
        placeholders). The planner (``easycat.planning``) is imported LAZILY
        inside :meth:`_manifest_readiness` — never at module load — so the M4
        boundary (``import easycat.server`` pulls no planner) is preserved.
        """
        draining = self._gate.is_draining
        manifest_loaded, plan_blocking_errors = self._manifest_readiness()
        return VoiceServerHealth(
            state="draining" if draining else "serving",
            active_sessions=self._gate.reserved_count,
            max_sessions=self.config.max_sessions,
            draining=draining,
            route_stack_ready=self._route_stack_ready(),
            manifest_loaded=manifest_loaded,
            plan_blocking_errors=plan_blocking_errors,
        )

    def _manifest_readiness(self) -> tuple[bool | None, tuple[str, ...] | None]:
        """Compute the M6b readiness sub-checks (manifest-loaded + plan blocking).

        Returns ``(None, None)`` for a factory-only server (no manifest/profile)
        so :class:`VoiceServerHealth` keeps the M4 ``skipped`` placeholders.
        Otherwise returns ``(manifest_loaded, plan_blocking_errors)``: a manifest
        that failed to load is ``(False, None)``; a loaded manifest returns its
        plan's blocking-error reasons (empty tuple when the plan is clean).

        The planner is imported LAZILY here so ``import easycat.server`` never
        pulls it (the M4/M6b boundary). This is the parity-gated wiring: it only
        ever trusts a planner verdict because the planner-vs-``create_session``
        parity test is green.
        """
        if self._manifest is None:
            if self._manifest_load_error is not None:
                # A manifest was configured but failed to load.
                return False, None
            return None, None
        from easycat.planning import build_provider_plan

        profile = self.config.profile
        plan = build_provider_plan(self._manifest.profile(profile), profile=profile)
        return True, plan.blocking_errors()

    def plan_payload(self) -> dict[str, Any]:
        """Return the read-only ``/plan`` JSON payload (redacted, no token).

        For a ``from_manifest`` server, resolves the selected profile's provider
        plan across all seven roles. For a factory-only server, returns a
        documented empty plan (``selected={}``) — there is no manifest profile to
        plan. No resolved token can appear: the planner reads only provider
        metadata (names/extras/env-var NAMES), never secret values.
        """
        if self._manifest is None:
            return {
                "profile": self.config.profile,
                "selected": {},
                "missing_env": [],
                "missing_extras": [],
                "warnings": [],
                "blocking_errors": [],
                "has_blocking_errors": False,
                "manifest_loaded": self._manifest_load_error is None,
            }
        from easycat.planning import build_provider_plan

        profile = self.config.profile
        plan = build_provider_plan(self._manifest.profile(profile), profile=profile)
        return {
            "profile": plan.profile,
            "selected": {
                role: {
                    "role": selection.role,
                    "provider": selection.provider,
                    "model": selection.model,
                    "config_type": selection.config_type,
                    "extra": selection.extra,
                    "required_env": selection.required_env,
                    "capabilities": sorted(selection.capabilities),
                }
                for role, selection in plan.selected.items()
            },
            "missing_env": list(plan.missing_env),
            "missing_extras": list(plan.missing_extras),
            "warnings": list(plan.warnings),
            "blocking_errors": list(plan.blocking_errors()),
            "has_blocking_errors": plan.has_blocking_errors,
            "manifest_loaded": True,
        }

    # ── Constructors ─────────────────────────────────────────────────

    @classmethod
    def from_app(cls, app: VoiceApp, config: VoiceServerConfig | None = None) -> VoiceServer:
        """Build a server from a :class:`~easycat.VoiceApp`'s factory only.

        Ownership rule (MF-4e): the mounted app contributes ONLY its
        per-transport ``config_factory``; ``VoiceServerConfig`` owns all process
        policy. ``from_app`` therefore lifts the app's WebSocket factory into a
        ``session_factory`` and applies the server's process policy on top — it
        never inherits ``WebSocketSessionServerConfig``'s divergent defaults and
        never calls ``VoiceApp.run()`` (M4 is WebSocket-only; WebRTC mounting is
        M7).

        The app's WebSocket ``config_factory`` returns an ``EasyConfig`` (or, in
        tests, a ``Session``-like object); ``_build_session`` routes either
        through ``create_session`` as needed, so the factory is lifted verbatim.
        """
        return cls(config or VoiceServerConfig(), session_factory=app._websocket_factory())

    @classmethod
    def from_manifest(
        cls,
        path: str | Path = "easycat.toml",
        *,
        profile: str = "default",
    ) -> VoiceServer:
        """Build a server from an ``easycat.toml`` manifest (M6a).

        Loads + validates the manifest (enforcing the ``bearer-env:NAME`` secret
        contract), maps ``[server]`` to :class:`VoiceServerConfig` process policy
        (host/port/max_sessions + the resolved :class:`BearerTokenAuth`), and
        builds a per-connection ``session_factory`` that converts the selected
        ``[voice.<profile>]`` to a fresh ``EasyConfig`` per connection (resolving
        the ``python:module:function`` agent reference). The resolved token is
        read from the environment at load time and never lives on the manifest.

        This does NOT import the planner (M6b owns ``/health/ready`` manifest/plan
        wiring); ``from_manifest`` only constructs the server.
        """
        from easycat.project import load_manifest

        manifest = load_manifest(path)
        manifest.profile(profile)  # validate the profile exists up front

        auth = manifest.resolve_auth()
        config = VoiceServerConfig(
            host=manifest.server.host,
            port=manifest.server.port,
            max_sessions=manifest.server.max_sessions,
            auth=auth,
            manifest_path=manifest.source_path,
            profile=profile,
        )

        def _factory(_transport: Any) -> EasyConfig:
            # A fresh EasyConfig per connection (no shared grouped sub-configs).
            return manifest.to_easyconfig(profile)

        server = cls(config, session_factory=_factory)
        # Retain the loaded manifest so the read-only ``/plan`` route and the M6b
        # readiness checks can build the plan from the selected profile. The
        # resolved token never lives on the manifest, so retaining it leaks nothing.
        server._manifest = manifest
        return server

    # ── Internals ────────────────────────────────────────────────────

    def _route_stack_ready(self) -> bool:
        """Return ``True`` when the route stack spanning both listeners is up.

        The aiohttp runner/site must be started and, when WebSocket is enabled,
        the raw-ws listener must be bound. (When ``enable_websocket=False`` the
        ws listener is intentionally absent and does not gate readiness.)
        """
        if not self._started or self._runner is None or self._site is None:
            return False
        if self.config.enable_websocket and self._ws_server is None:
            return False
        return not self._draining

    async def _start_websocket_listener(self) -> Any:
        """Start the raw ``/ws`` ``websockets.serve`` listener on its own port.

        ``/ws`` is NOT an aiohttp route (the endpoint table is a logical surface
        listing): it is a raw ``websockets.serve`` listener co-hosted exactly
        like ``telephony/server.py`` and ``serve_websocket_sessions``. The
        listener binds the same host as the aiohttp app on ``port + 1`` so the
        two listeners do not collide on a single port. ``health()``/draining
        span this listener and the aiohttp runner/site.
        """
        import websockets

        return await websockets.serve(
            self._handle_websocket_connection,
            self.config.host,
            self._websocket_port(),
            compression=None,
        )

    def _websocket_port(self) -> int:
        """Return the raw-ws listener port (``port + 1`` of the aiohttp port).

        Port ``0`` (ephemeral) stays ``0`` so tests can bind both listeners on
        OS-assigned ports without collision.
        """
        return self.config.port + 1 if self.config.port != 0 else 0

    @property
    def http_address(self) -> tuple[str, int] | None:
        """Return the bound ``(host, port)`` of the aiohttp listener, if any.

        Useful for callers/tests that bind on an ephemeral port (``port=0``)
        and then need the actual port to reach the health endpoints.
        """
        if self._site is None:
            return None
        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if not sockets:
            return None
        host, port = sockets[0].getsockname()[:2]
        return host, port

    @property
    def websocket_address(self) -> tuple[str, int] | None:
        """Return the bound ``(host, port)`` of the raw-ws listener, if any.

        Useful for tests that bind on an ephemeral port and then need the
        actual port to connect a client.
        """
        if self._ws_server is None:
            return None
        sockets = getattr(self._ws_server, "sockets", None)
        if not sockets:
            return None
        host, port = sockets[0].getsockname()[:2]
        return host, port

    async def _handle_websocket_connection(self, ws: Any) -> None:
        """Accept one ``/ws`` connection, gated by auth + the shared capacity gate.

        Reject when draining or at ``max_sessions`` (both owned by the shared
        :class:`CapacityGate`), and authorize via the unified ``AuthPolicy`` so
        the ``/ws`` path is no longer unauthenticated (closing the ``0.0.0.0``
        gap). On accept, build a per-connection ``WebSocketConnectionTransport``
        + session via the ``session_factory`` and drive it through
        ``SessionManager.connection``; the gate tracks the key + session so the
        graceful-drain step can force-stop it if the handler hangs.
        """
        from easycat.transports.websocket import WebSocketConnectionTransport

        if self._gate.is_draining:
            await ws.close(code=_WS_OVER_CAPACITY_CLOSE_CODE, reason=_WS_DRAINING_CLOSE_REASON)
            return

        if not self._authorize_websocket(ws):
            await ws.close(
                code=_WS_UNAUTHORIZED_CLOSE_CODE,
                reason=_WS_UNAUTHORIZED_CLOSE_REASON,
            )
            return

        if self._session_factory is None:
            # No factory configured (e.g. a health-only server); reject cleanly
            # rather than crashing the listener task.
            await ws.close(
                code=_WS_OVER_CAPACITY_CLOSE_CODE,
                reason="No session factory configured",
            )
            return

        if not self._gate.try_acquire():
            await ws.close(
                code=_WS_OVER_CAPACITY_CLOSE_CODE,
                reason=_WS_OVER_CAPACITY_CLOSE_REASON,
            )
            return

        key = id(ws)
        try:
            transport = WebSocketConnectionTransport(ws)
            session = self._build_session(transport)
            self._gate.track(key)
            self._active_session_objs[key] = session
            async with self._manager.connection(key, session, runtime_feedback=False):
                await ws.wait_closed()
        finally:
            self._gate.untrack(key)
            self._active_session_objs.pop(key, None)
            self._gate.release()

    def _authorize_websocket(self, ws: Any) -> bool:
        """Authorize a ``/ws`` handshake through the unified ``AuthPolicy``.

        Returns ``True`` when no auth policy is configured (the loopback/dev
        default). When a policy is set, the handshake ``Authorization`` header
        and ``?token=`` query are read from the accepted ``ServerConnection``'s
        request and run through :meth:`AuthPolicy.authorize`. Never logs tokens.
        """
        auth = self.config.auth
        if auth is None:
            return True
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
        path = getattr(request, "path", "") or ""
        result = auth.authorize(from_websocket(headers, path))
        return result.allowed

    def _build_session(self, transport: Any) -> Session:
        """Build a :class:`Session` from the per-transport ``session_factory``.

        The factory may return either an ``EasyConfig`` (passed through
        ``create_session``) or an already-built ``Session``.
        """
        from easycat.config import create_session

        assert self._session_factory is not None  # guarded by the caller
        result = self._session_factory(transport)
        # A ``Session`` exposes ``start``/``stop``; an ``EasyConfig`` does not.
        if hasattr(result, "start") and hasattr(result, "stop"):
            return result  # type: ignore[return-value]
        return create_session(result)  # type: ignore[arg-type]

    async def _try_acquire_slot(self) -> bool:
        """Reserve a capacity slot via the shared gate; ``False`` when full/draining.

        Kept as a thin compatibility shim over :meth:`CapacityGate.try_acquire`
        so existing callers/tests that drive readiness through the slot API stay
        green after the lift.
        """
        return self._gate.try_acquire()

    async def _release_slot(self) -> None:
        """Return a reserved capacity slot via the shared gate."""
        self._gate.release()
