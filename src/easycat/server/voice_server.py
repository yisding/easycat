"""``VoiceServer`` — the M4 production process skeleton (aiohttp + raw WS).

M4 scope: lifecycle (``start`` / ``serve`` / ``stop`` / ``run`` / ``health``),
the three health endpoints, and a WebSocket ``/ws`` route co-hosted as a raw
:func:`websockets.serve` listener on its own port. Capacity is a MINIMAL
inline active-session counter (M5 lifts the shared ``Semaphore``/draining
collaborator out of the serve helpers). There is NO planner import and NO
metric emission in M4.

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
from easycat.server.config import VoiceServerConfig
from easycat.server.health import VoiceServerHealth
from easycat.server.routes import register_health_routes
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


class VoiceServer:
    """A production process layer over one or more per-connection factories.

    M4 supports a single WebSocket ``/ws`` route built from a per-transport
    ``session_factory`` and co-hosts the aiohttp health endpoints. Capacity and
    draining are tracked by a minimal inline counter + flag (the M5 deliverable
    replaces them with the lifted shared collaborator).
    """

    def __init__(
        self,
        config: VoiceServerConfig | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.config = config or VoiceServerConfig()
        self._session_factory = session_factory

        # Bare session registry only: add/remove/stop_all/connection. Capacity
        # and draining are NOT attributed to it (it has neither).
        self._manager: SessionManager[int] = SessionManager()

        # Minimal capacity counter + draining flag held INLINE (the M5
        # collaborator lifts the real Semaphore/active-set/draining here).
        self._active_sessions = 0
        self._active_lock = asyncio.Lock()
        self._draining = False

        # Route-stack references spanning BOTH listener kinds: the aiohttp
        # runner/site and the raw ``websockets.serve`` listener.
        self._runner: Any = None
        self._site: Any = None
        self._ws_server: Any = None
        self._started = False

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
        web = require_module("aiohttp.web", extra="webrtc", purpose="VoiceServer")

        app = web.Application()
        if self.config.enable_health:
            register_health_routes(app, self)

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
        """Stop accepting connections and tear down both listener kinds.

        M4 minimal teardown: set the draining flag (so the readiness check and
        the ``/ws`` handler reject new connections), close the raw-ws listener
        AND the aiohttp site/runner (spanning both listeners), then run
        ``SessionManager.stop_all()`` as the final hard sweep. The full
        graceful drain/escalation (``drain_timeout_s`` + ``force=True``
        escalation through the lifted active set) is the M5 deliverable.
        """
        self._draining = True

        if self._ws_server is not None:
            self._ws_server.close()
            try:
                await self._ws_server.wait_closed()
            finally:
                self._ws_server = None

        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

        # SessionManager is a bare registry; ``stop_all`` is the final hard
        # sweep once no connection handler can still add/remove sessions (the
        # raw-ws listener is closed above).
        await self._manager.stop_all()
        self._started = False

    async def health(self) -> VoiceServerHealth:
        """Build a :class:`VoiceServerHealth` snapshot from live state."""
        async with self._active_lock:
            active = self._active_sessions
        return VoiceServerHealth(
            state="draining" if self._draining else "serving",
            active_sessions=active,
            max_sessions=self.config.max_sessions,
            draining=self._draining,
            route_stack_ready=self._route_stack_ready(),
        )

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
        """Build a server from an ``easycat.toml`` manifest (M6a owns this)."""
        raise NotImplementedError(
            "VoiceServer.from_manifest is implemented in M6a (manifest loader). "
            "Use VoiceServer.from_app(...) or construct with a session_factory."
        )

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
        """Accept one ``/ws`` connection, gated by the minimal capacity counter.

        Reject new connections when draining or when the minimal counter is at
        ``max_sessions`` (the M4 stand-in that M5 replaces with the lifted
        ``Semaphore`` without changing the readiness contract). On accept, build
        a per-connection ``WebSocketConnectionTransport`` + session via the
        ``session_factory`` and drive it through ``SessionManager.connection``;
        the counter is incremented on accept and decremented in cleanup so
        ``/health/ready`` reads an accurate count.
        """
        from easycat.transports.websocket import WebSocketConnectionTransport

        if self._draining:
            await ws.close(code=_WS_OVER_CAPACITY_CLOSE_CODE, reason=_WS_DRAINING_CLOSE_REASON)
            return

        if self._session_factory is None:
            # No factory configured (e.g. a health-only server); reject cleanly
            # rather than crashing the listener task.
            await ws.close(
                code=_WS_OVER_CAPACITY_CLOSE_CODE,
                reason="No session factory configured",
            )
            return

        if not await self._try_acquire_slot():
            await ws.close(
                code=_WS_OVER_CAPACITY_CLOSE_CODE,
                reason=_WS_OVER_CAPACITY_CLOSE_REASON,
            )
            return

        try:
            transport = WebSocketConnectionTransport(ws)
            session = self._build_session(transport)
            async with self._manager.connection(id(ws), session, runtime_feedback=False):
                await ws.wait_closed()
        finally:
            await self._release_slot()

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
        """Increment the minimal counter if below capacity; else return False."""
        async with self._active_lock:
            if self._draining or self._active_sessions >= self.config.max_sessions:
                return False
            self._active_sessions += 1
            return True

    async def _release_slot(self) -> None:
        """Decrement the minimal counter, never below zero."""
        async with self._active_lock:
            if self._active_sessions > 0:
                self._active_sessions -= 1
