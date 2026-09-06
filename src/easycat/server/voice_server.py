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
from collections.abc import Awaitable, Callable
from dataclasses import is_dataclass, replace
from enum import Enum, auto
from functools import partial
from typing import TYPE_CHECKING, Any

from easycat._extras import require_module
from easycat._signals import create_shutdown_event
from easycat.runtime._event_tasks import RuntimeTaskScope, wait_for_owned_future
from easycat.server.auth import authorized_bind, from_websocket
from easycat.server.config import VoiceServerConfig
from easycat.server.health import VoiceServerHealth
from easycat.server.routes import (
    metrics_middleware,
    register_capabilities_route,
    register_health_routes,
    register_manifest_route,
    register_metrics_route,
    register_plan_route,
)
from easycat.server.transports import CapacityGate
from easycat.session_manager import (
    SessionManager,
    SessionStopReport,
    log_session_stop_failures,
)

if TYPE_CHECKING:
    from pathlib import Path

    from easycat.config import EasyConfig
    from easycat.server.health import ServerState
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
_WS_CLOSE_TASK = "voice_server_ws_close"
_WS_CLOSE_COHORT = "voice-server-ws-close"
_WS_HANDLER_TASK = "voice_server_ws_handler"
_WS_HANDLER_COHORT = "voice-server-ws-handler"
_LISTENER_CLEANUP_TASK = "voice_server_listener_cleanup"
_LISTENER_CLEANUP_COHORT = "voice-server-listener-cleanup"
_SESSION_SWEEP_TASK = "voice_server_session_sweep"
_SESSION_SWEEP_COHORT = "voice-server-session-sweep"


class _ListenerCleanupWaitResult(Enum):
    COMPLETED = auto()
    COOPERATIVELY_CANCELLED = auto()
    RETAINED = auto()


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
        self._session_sweep_task_scope = RuntimeTaskScope(
            owner_label="voice-server-session-sweep",
            member_name=_SESSION_SWEEP_TASK,
            cohort=_SESSION_SWEEP_COHORT,
            logger=logger,
            failure_message="VoiceServer SessionManager sweep task failed",
            drop_if_closed=False,
        )

        # Shared capacity + draining collaborator (the M5 lift). It owns the
        # reservation counter, the active-connection set, and the draining flag
        # — identical to the WebRTC/WebSocket serve helpers.
        self._gate: CapacityGate[int] = CapacityGate(self.config.max_sessions)
        # Map active gate keys -> live session objects so the drain step can call
        # ``session.stop(force=True)`` directly (the collaborator drives this;
        # ``SessionManager`` has no draining state).
        self._active_session_objs: dict[int, Any] = {}
        # Live ``/ws`` handler tasks. Tracked so :meth:`stop` can cancel a handler
        # that is hung in ``ws.wait_closed()`` (e.g. a client that never closes),
        # which would otherwise keep the raw-ws ``Server._close`` waiter (it
        # ``asyncio.wait``s on its handlers, it does NOT cancel them) — and thus
        # ``ws_server.wait_closed()`` — blocked forever.
        self._ws_handler_task_scope = RuntimeTaskScope(
            owner_label="voice-server-ws-handlers",
            member_name=_WS_HANDLER_TASK,
            cohort=_WS_HANDLER_COHORT,
            logger=logger,
            failure_message="VoiceServer raw-WebSocket handler task failed",
            drop_if_closed=False,
            release_standalone_when_idle=True,
        )
        self._ws_connections: dict[int, Any] = {}
        self._ws_close_task_scope = RuntimeTaskScope(
            owner_label="voice-server-ws-close",
            member_name=_WS_CLOSE_TASK,
            cohort=_WS_CLOSE_COHORT,
            logger=logger,
            failure_message="VoiceServer raw-WebSocket close task failed",
            drop_if_closed=False,
            release_standalone_when_idle=True,
        )
        self._await_natural_end_drain = False

        # In-process metric snapshot for ``GET /metrics`` (M8). The
        # ``easycat._observability`` instruments are write-only and no-op without
        # an OTel SDK, so they cannot be read back; these process-side counters
        # give ``/metrics`` stable numbers in CI independent of any SDK. They are
        # incremented alongside the OTel emission, never instead of it.
        self._requests_total = 0
        self._sessions_rejected_total = 0

        # Route-stack references spanning BOTH listener kinds: the aiohttp
        # runner/site and the raw ``websockets.serve`` listener.
        self._runner: Any = None
        self._site: Any = None
        self._ws_server: Any = None
        self._started = False
        # Public lifecycle transitions are serialized so concurrent starts
        # cannot publish different runner/site pairs into the same fields, and
        # stop cannot drain a half-published startup transaction.
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        # A failed teardown keeps the resource references that did not cleanly
        # close.  A later ``stop`` retries them; ``start`` must not overwrite
        # those references with a fresh listener stack in the meantime.
        self._lifecycle_cleanup_error: Exception | None = None
        # Listener cleanup itself can resist cancellation (for example, a
        # transport implementation stuck in ``site.stop()``). Keep one task per
        # listener stage so a bounded stop can continue draining sessions while
        # retaining the original cleanup ownership for a later retry. Reissuing
        # ``site.stop()`` concurrently with the first invocation is not a safe
        # retry strategy.
        self._listener_cleanup_task_scope = RuntimeTaskScope(
            owner_label="voice-server-listener-cleanup",
            member_name=_LISTENER_CLEANUP_TASK,
            cohort=_LISTENER_CLEANUP_COHORT,
            logger=logger,
            failure_message="VoiceServer listener cleanup task failed",
            drop_if_closed=False,
            release_standalone_when_idle=True,
        )
        self._listener_cleanup_tasks: dict[str, asyncio.Task[Any]] = {}

        # The mounted WebRTC route unit (M7). WebRTC ``/offer`` reserve through
        # the SAME shared ``_gate`` and register into the SAME
        # ``_active_session_objs`` as ``/ws`` so capacity / draining /
        # drain-on-stop span both transports uniformly. ``None`` until ``start``
        # mounts it (gated on ``enable_webrtc`` + a configured factory).
        self._webrtc_routes: Any = None

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
        # readiness check and the ``/ws`` handler observe it identically. Handle
        # BOTH values symmetrically — a write-only setter that silently dropped
        # ``= False`` would be a trap (assigning the inverse of ``= True`` would
        # be a no-op, leaving the gate draining).
        if value:
            self._gate.start_draining()
        else:
            self._gate.stop_draining()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the aiohttp app + (optionally) the raw ``/ws`` listener.

        aiohttp is gated here via :func:`require_module` (the ``webrtc`` extra
        supplies it; there is no dedicated ``server`` extra) so a missing extra
        surfaces a clear, actionable error rather than an ``ImportError`` at
        package import time.
        """
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "start":
                return
            raise RuntimeError("VoiceServer.start() cannot run reentrantly during stop()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "start"
            try:
                await self._start_unlocked()
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _start_unlocked(self) -> None:
        """Start while the caller owns ``_lifecycle_lock``."""
        if self._lifecycle_cleanup_error is not None:
            raise RuntimeError(
                "VoiceServer cannot start because previous teardown cleanup "
                "is incomplete; call stop() again to retry cleanup"
            ) from self._lifecycle_cleanup_error
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

        # Make ``config.allow_query_token`` LIVE: a ``BearerTokenAuth`` the
        # process layer authorizes against must honor the process-level
        # ``allow_query_token`` opt-in (the bundled browser WS client depends on
        # ``?token=`` because browsers cannot set handshake headers). Without
        # this the field was declared LIVE but never consumed — a policy built
        # from env / passed in kept its own (default-OFF) value regardless. The
        # reconciliation only ever OPTS IN (never forces OFF a policy that
        # already enabled it) so it spans both the ``/ws`` and ``/webrtc`` paths,
        # which both authorize through ``self.config.auth``.
        self._reconcile_query_token_opt_in()

        web = require_module("aiohttp.web", extra="webrtc", purpose="VoiceServer")

        # The metrics middleware records per-request server metrics keyed by the
        # matched route TEMPLATE (M8). It is a no-op when ``enable_metrics`` is
        # off; installing it always keeps the wiring simple and the count covers
        # whatever read-only routes are mounted below.
        app = web.Application(middlewares=[metrics_middleware(self)])
        if self.config.enable_health:
            register_health_routes(app, self)
        # The read-only ``/plan`` route (M6b). Registered unconditionally with
        # health so a factory-only server still answers with a documented empty
        # plan; the handler imports the planner LAZILY so registration pulls no
        # planner at module load.
        register_plan_route(app, self)
        # The M8 read-only endpoints. All three are registered unconditionally
        # (gating them on a manifest would 404 a factory-only server): each
        # degrades to a documented empty/absent shape. ``/metrics`` reads the
        # in-process snapshot; ``/manifest`` dumps the redacted manifest (no
        # resolved token); ``/capabilities`` aggregates the parity-gated planner
        # (imported lazily inside the payload method, preserving the M4 boundary).
        register_metrics_route(app, self)
        register_manifest_route(app, self)
        register_capabilities_route(app, self)

        # Mount the WebRTC routes (M7) on the SAME aiohttp app under ``/webrtc/*``
        # — they are aiohttp routes on the health listener, NOT a separate
        # listener like ``/ws``. They share ``self._gate`` / ``self._manager`` /
        # ``self._active_session_objs`` so capacity, draining, and drain-on-stop
        # span WebRTC offers AND ``/ws`` connections uniformly.
        if self.config.enable_webrtc and self._session_factory is not None:
            self._webrtc_routes = self._build_webrtc_routes()
            self._webrtc_routes.register(app, prefix="/webrtc", web=web)

        runner, site = await self._start_http_listener(web, app)
        self._runner = runner
        self._site = site

        try:
            if self.config.enable_websocket:
                self._ws_server = await self._start_websocket_listener()
        except BaseException as startup_error:
            # The HTTP listener is already live here, and can even have accepted
            # a WebRTC offer while the raw-WebSocket bind was pending. Route the
            # rollback through the normal forced-stop owner so every partially
            # started listener/session is closed and all lifecycle references
            # are reset before the original startup failure propagates.
            try:
                await self._stop_unlocked(force=True)
            except BaseException as cleanup_error:
                if isinstance(cleanup_error, asyncio.CancelledError):
                    # This rollback calls the unlocked teardown directly, so
                    # it does not pass through stop()'s cancellation epilogue.
                    # Publish the same retryable stopped state here.
                    self._finalize_stop_cleanup(
                        [RuntimeError("VoiceServer startup rollback was interrupted")]
                    )
                raise startup_error from cleanup_error
            raise

        self._started = True
        # Reflect the fresh serving state on the draining gauge (M8). A prior
        # ``stop`` flipped it to draining=1; a reused server is serving again, so
        # re-assert draining=0 (the gate flag itself was reset in
        # ``_reset_gate_bookkeeping``). A no-op without an OTel SDK.
        self._emit_draining(False)
        logger.info(
            "VoiceServer ready: http://%s:%s (websocket=%s, webrtc=%s)",
            self.config.host,
            self.config.port,
            self.config.enable_websocket,
            self._webrtc_routes is not None,
        )

    async def _start_http_listener(self, web: Any, app: Any) -> tuple[Any, Any]:
        """Start aiohttp transactionally, retaining failed rollback ownership."""
        runner = web.AppRunner(app)
        site: Any | None = None

        async def start_site(bind_host: str) -> Any:
            nonlocal site
            site = web.TCPSite(runner, bind_host, self.config.port)
            await site.start()
            return site

        try:
            await runner.setup()
            site = await authorized_bind(
                self.config.host,
                auth=self.config.auth,
                unsafe_allow_no_auth=self.config.unsafe_allow_no_auth,
                binder=start_site,
            )
        except BaseException as startup_error:
            # A site can fail after it has partially started. Publish both
            # listener references before rolling back so the regular bounded
            # drain owner closes every resource and retains any unfinished
            # cleanup for retry.
            self._runner = runner
            self._site = site
            try:
                await self._stop_unlocked(force=True)
            except BaseException as cleanup_error:
                if isinstance(cleanup_error, asyncio.CancelledError):
                    # This direct unlocked rollback does not pass through
                    # ``stop()``'s cancellation epilogue.
                    self._finalize_stop_cleanup(
                        [RuntimeError("VoiceServer startup rollback was interrupted")]
                    )
                raise startup_error from cleanup_error
            raise
        return runner, site

    def _build_webrtc_routes(self) -> Any:
        """Build the mounted :class:`WebRTCRoutes` from process policy.

        Derives a :class:`WebRTCTransportConfig` from :class:`VoiceServerConfig`:
        host/port are NOT used for binding here (the aiohttp site already bound),
        but ``cors_allowed_origins`` and the unified auth carry over. The shared
        ``_gate`` / ``_manager`` / ``_active_session_objs`` are injected so the
        WebRTC offers and ``/ws`` connections drain through one collaborator.
        """
        from easycat.server.webrtc_routes import WebRTCRoutes
        from easycat.transports._webrtc_config import WebRTCTransportConfig

        # ``max_sessions`` keeps the gate cap consistent in the ``/webrtc/health``
        # JSON; the actual reservation is the shared gate's. A non-loopback host
        # is fine here: the bind guard already ran in ``start`` against the
        # process policy, and these routes bind no socket.
        webrtc_config = WebRTCTransportConfig(
            host=self.config.host,
            port=self.config.port,
            cors_allowed_origins=self.config.cors_allowed_origins,
            max_sessions=self.config.max_sessions,
        )
        assert self._session_factory is not None  # guarded by the caller
        return WebRTCRoutes(
            webrtc_config,
            auth=self.config.auth,
            config_factory=self._session_factory,
            gate=self._gate,
            manager=self._manager,
            runtime_feedback=False,
            active_session_objs=self._active_session_objs,
            # Route WebRTC offer rejections (auth / draining / capacity) through
            # the SAME rejection metric path as ``/ws`` so ``/metrics`` and
            # ``easycat.server.sessions.rejected.total`` span both transports.
            on_session_rejected=self._emit_session_rejected,
            # Route accepted/torn-down WebRTC offers through the SAME
            # active-connection gauge as ``/ws`` so ``easycat.server.connections
            # .active`` spans both transports (the shared gate already does).
            on_connections_changed=self._emit_connections_active,
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
        3. Stop accepting on the raw-ws listener and close the aiohttp
           site/runner. The default ``drain_mode="stop_sessions"`` also closes
           existing raw-ws connections immediately; ``"await_natural_end"``
           leaves them open until caller hangup or ``drain_timeout_s`` expires.
        4. Hand the active sessions to :meth:`CapacityGate.drain`. During drain
           each handler leaves its session in the active set (the drain OWNS the
           stop — see :meth:`_teardown_ws_session`); the drain starts the single
           graceful ``session.stop()`` per session and waits ``drain_timeout_s``.
        5. The drain escalates any session whose graceful stop did NOT finish in
           the grace window by cancelling and reaping the still-pending graceful
           task, then calling ``session.stop(force=True)`` with one clear
           teardown owner.
        6. Cancel any handler task still hung in ``ws.wait_closed()`` (e.g. a
           client that never completed the close handshake) so the raw-ws
           ``Server._close`` waiter — which ``asyncio.wait``s on its handlers
           rather than cancelling them — cannot block forever.
        7. ``SessionManager.stop_all()`` ONLY as the final hard sweep, once the
           listeners are closed and no handler can still add/remove sessions.

        ``force=True`` collapses the grace window to zero so remaining sessions
        are force-stopped immediately.
        """
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "stop":
                return
            raise RuntimeError("VoiceServer.stop() cannot run reentrantly during start()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "stop"
            try:
                try:
                    await self._stop_unlocked(force=force)
                except asyncio.CancelledError:
                    # Cancellation may interrupt any direct cleanup await
                    # before _stop_unlocked reaches its normal epilogue.
                    # Publish a truthful, retryable stopped state while all
                    # not-yet-cleaned resource references are still retained,
                    # then preserve the caller's cancellation.
                    self._finalize_stop_cleanup(
                        [RuntimeError("VoiceServer teardown was interrupted by cancellation")]
                    )
                    raise
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _stop_unlocked(self, *, force: bool = False) -> None:
        """Stop while the caller owns ``_lifecycle_lock``."""
        cleanup_errors: list[Exception] = []

        # (1) draining flag.
        self._gate.start_draining()
        # Reflect the state transition on the draining gauge (M8). A no-op
        # without an OTel SDK; the registered name cannot raise in sanitize.
        self._emit_draining(True)

        await_natural_end = self.config.drain_mode == "await_natural_end" and not force
        self._await_natural_end_drain = await_natural_end

        # (3) stop accepting so no new handler task can start. In the default
        # mode, in-flight ``/ws`` connections are also closed so each handler's
        # ``ws.wait_closed()`` returns. In natural-end mode, existing sockets are
        # left open until caller hangup or drain timeout. Do NOT ``await
        # ws_server.wait_closed()`` yet: a hung handler would deadlock it before
        # the drain can force-escalate. The aiohttp site/runner are torn down
        # now; the raw-ws server is awaited AFTER the drain and handler cancel.
        ws_server = await self._close_listeners_for_drain(
            await_natural_end=await_natural_end,
            cleanup_errors=cleanup_errors,
        )

        # (4)+(5) wait for active connection tasks up to ``drain_timeout_s``,
        # then force-escalate the remainder. ``force=True`` skips the grace
        # window entirely. Running BEFORE ``ws_server.wait_closed()`` lets the
        # force-stop own teardown for any handler whose graceful stop is skipped
        # while draining.
        await self._attempt_cleanup(
            "session drain",
            self._drain_sessions_for_stop(
                force=force,
                await_natural_end=await_natural_end,
            ),
            cleanup_errors,
        )

        # (6) cancel any handler still hung in ``ws.wait_closed()``. The drain
        # already force-stopped the sessions; cancelling the surviving handler
        # tasks unblocks the raw-ws ``Server._close`` waiter so the bounded
        # ``wait_closed`` below cannot deadlock.
        await self._attempt_cleanup(
            "WebSocket handler cancellation",
            self._cancel_ws_handler_tasks(
                timeout_s=self.config.force_shutdown_timeout_s,
            ),
            cleanup_errors,
        )

        # Now all handlers can return (closed connections + forced sessions +
        # cancelled hung handlers), so awaiting the raw-ws server completes
        # promptly. Bound it with ``force_shutdown_timeout_s`` as an independent
        # backstop: even a pathological handler that resists cancellation cannot
        # make ``stop()`` block forever.
        if ws_server is not None:
            closed = await self._attempt_bounded_listener_cleanup(
                "raw-WebSocket listener",
                ws_server.wait_closed,
                cleanup_errors,
                cancel_on_timeout=True,
                timeout_action="close",
            )
            if closed and self._ws_server is ws_server:
                self._ws_server = None

        # Cancel the per-offer WebRTC ``wait_closed`` cleanup tasks. The drain
        # step already force-stopped the sessions via the shared active set;
        # their cleanup tasks (each awaiting ``transport.wait_closed``) would
        # otherwise outlive ``stop`` and leak. Mirrors the serve helper's
        # ``cancel_cleanup_tasks``.
        if self._webrtc_routes is not None:
            routes = self._webrtc_routes
            cleanup_succeeded, _ = await self._attempt_cleanup(
                "WebRTC cleanup tasks",
                routes.cancel_cleanup_tasks(
                    timeout_s=self.config.force_shutdown_timeout_s,
                ),
                cleanup_errors,
            )
            if cleanup_succeeded and self._webrtc_routes is routes:
                self._webrtc_routes = None

        # (7) final hard sweep of the bare registry, then reset the shared gate
        # bookkeeping. While draining, the ``/ws`` handlers deliberately skip
        # their own untrack/release (the drain owns teardown), so reset the gate
        # here once no handler can still run. The drain normally owns the single
        # effective stop per session. A natural disconnect can instead have a
        # handler-owned graceful removal cancelled at the deadline; because the
        # manager retains that entry until teardown succeeds, this force sweep
        # retries it after the handler has unwound. Bound the sweep with
        # ``force_shutdown_timeout_s`` so a force-stop that never returns cannot
        # block server teardown.
        sweep_task = self._session_sweep_task_scope.create_task(
            self._manager.stop_all(force=True),
            task_name="easycat-voice-server-session-sweep",
        )
        assert sweep_task is not None
        sweep_succeeded, swept = await self._attempt_cleanup(
            "SessionManager hard sweep",
            wait_for_owned_future(
                sweep_task,
                timeout_s=self.config.force_shutdown_timeout_s,
            ),
            cleanup_errors,
        )
        if sweep_succeeded:
            report = None
            if swept:
                try:
                    report = sweep_task.result()
                except Exception as exc:  # noqa: BLE001 intentional lifecycle boundary
                    self._record_cleanup_error(
                        "SessionManager hard sweep result retrieval",
                        exc,
                        cleanup_errors,
                    )
            await self._record_incomplete_hard_sweep(
                completed=swept,
                report=report,
                sweep_task=sweep_task,
                cleanup_errors=cleanup_errors,
            )
        await self._session_sweep_task_scope.release_standalone_if_empty()

        # Keep session/resource references when any cleanup stage failed so a
        # later stop can retry them. The gate itself is always reset to a
        # truthful non-serving state; start is blocked by
        # ``_lifecycle_cleanup_error`` until a retry completes successfully.
        self._finalize_stop_cleanup(cleanup_errors)
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _record_incomplete_hard_sweep(
        self,
        *,
        completed: bool,
        report: SessionStopReport[int] | None,
        sweep_task: asyncio.Task[SessionStopReport[int]],
        cleanup_errors: list[Exception],
    ) -> None:
        """Retain lifecycle ownership when the manager still owns sessions."""
        if not completed:
            abandon_report = await self._manager.abandon_pending_stops()
            # Cancelling the actual per-key tasks lets the already-cancelled
            # stop_all waiter settle. Give its cancellation propagation one
            # final turn without extending the hard deadline.
            await asyncio.sleep(0)
            if abandon_report.ok and sweep_task.cancelled():
                logger.warning(
                    "VoiceServer hard sweep exceeded force_shutdown_timeout_s=%ss; "
                    "cancellation settled with no retained session work",
                    self.config.force_shutdown_timeout_s,
                )
                return
            if sweep_task.done() and not sweep_task.cancelled():
                try:
                    report = sweep_task.result()
                except Exception as exc:  # noqa: BLE001 intentional lifecycle boundary
                    self._record_cleanup_error(
                        "SessionManager hard sweep cancellation cleanup",
                        exc,
                        cleanup_errors,
                    )
                    return
                completed = True
            else:
                timeout_error = RuntimeError(
                    "SessionManager.stop_all did not finish within "
                    f"force_shutdown_timeout_s={self.config.force_shutdown_timeout_s}s; "
                    f"retained {len(abandon_report.retained_keys)} session(s)"
                )
                logger.warning("VoiceServer: %s", timeout_error)
                cleanup_errors.append(timeout_error)
                return
        retained_keys = self._manager.active_keys()
        report_failed = report is not None and log_session_stop_failures(
            report,
            context="VoiceServer hard sweep",
            log=logger,
        )
        if retained_keys or report_failed:
            retained_count = len(retained_keys)
            if retained_count == 0 and report is not None:
                retained_count = len(report.failures)
            retained_error = RuntimeError(
                f"SessionManager retained {retained_count} session(s) after the hard sweep"
            )
            logger.warning("VoiceServer: %s", retained_error)
            cleanup_errors.append(retained_error)

    def _finalize_stop_cleanup(self, cleanup_errors: list[Exception]) -> None:
        """Publish truthful stopped state while retaining failed cleanup ownership."""
        self._await_natural_end_drain = False
        self._started = False
        self._lifecycle_cleanup_error = cleanup_errors[0] if cleanup_errors else None
        if cleanup_errors:
            # A retained listener can still accept work until its original
            # cleanup finishes. Keep the gate draining (and its reservations
            # intact) so that work is rejected rather than admitted into a
            # server which cannot be restarted yet. ``stop()`` retries the
            # retained resources; only a fully successful teardown clears this
            # fence for a future start.
            self._emit_connections_active()
            return

        self._active_session_objs.clear()
        self._reset_gate_bookkeeping()
        # Clear the active-connections gauge on both server_state series so the
        # post-drain reading is 0, not a stale non-zero value (M8 fix).
        self._emit_connections_active_cleared()
        self._emit_draining(False)

    @staticmethod
    def _record_cleanup_error(
        stage: str,
        exc: Exception,
        cleanup_errors: list[Exception],
    ) -> None:
        logger.error("VoiceServer cleanup failed during %s", stage, exc_info=exc)
        cleanup_errors.append(exc)

    async def _attempt_cleanup(
        self,
        stage: str,
        awaitable: Awaitable[Any],
        cleanup_errors: list[Exception],
    ) -> tuple[bool, Any]:
        """Await one cleanup stage, recording failure so later stages still run."""
        try:
            return True, await awaitable
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self._record_cleanup_error(stage, exc, cleanup_errors)
            return False, None

    async def _attempt_bounded_listener_cleanup(
        self,
        stage: str,
        cleanup: Callable[[], Awaitable[Any]],
        cleanup_errors: list[Exception],
        *,
        cancel_on_timeout: bool = False,
        timeout_action: str = "finish",
    ) -> bool:
        """Run one listener cleanup stage under the force-shutdown deadline.

        Unlike ``asyncio.wait_for``, this keeps a cancellation-resistant
        cleanup coroutine owned after the deadline. A later ``stop()`` awaits
        that same task instead of concurrently invoking the listener cleanup a
        second time. The surrounding drain can therefore still force-stop
        sessions promptly while the gate remains fenced as ``draining``.
        ``cancel_on_timeout`` preserves stages whose legacy hard bound first
        requested cooperative cancellation before retaining a survivor.
        """
        task, previously_completed = self._prepare_listener_cleanup_task(
            stage,
            cleanup,
            cleanup_errors,
        )
        if previously_completed:
            return True
        if task is None:
            return False
        wait_result = await self._wait_for_listener_cleanup_task(
            task,
            cancel_on_timeout=cancel_on_timeout,
        )
        if wait_result is _ListenerCleanupWaitResult.COOPERATIVELY_CANCELLED:
            # The wait was cancelled, but the stage still missed its deadline:
            # report the timeout so the caller keeps the listener for retry
            # instead of discarding it as successfully closed. The settled
            # task is reaped so a later stop() starts a fresh attempt.
            self._listener_cleanup_tasks.pop(stage, None)
            await asyncio.gather(task, return_exceptions=True)
            timeout_error = RuntimeError(
                f"{stage} did not {timeout_action} within "
                f"force_shutdown_timeout_s={self.config.force_shutdown_timeout_s}s "
                "(cleanup wait cooperatively cancelled)"
            )
            logger.warning("VoiceServer: %s", timeout_error)
            cleanup_errors.append(timeout_error)
            return False
        if wait_result is _ListenerCleanupWaitResult.RETAINED:
            timeout_error = RuntimeError(
                f"{stage} did not {timeout_action} within "
                f"force_shutdown_timeout_s={self.config.force_shutdown_timeout_s}s"
            )
            logger.warning("VoiceServer: %s", timeout_error)
            cleanup_errors.append(timeout_error)
            return False
        return await self._finish_listener_cleanup_task(stage, task, cleanup_errors)

    def _prepare_listener_cleanup_task(
        self,
        stage: str,
        cleanup: Callable[[], Awaitable[Any]],
        cleanup_errors: list[Exception],
    ) -> tuple[asyncio.Task[Any] | None, bool]:
        """Return a pending listener cleanup task or a completed retry result."""
        task = self._listener_cleanup_tasks.get(stage)
        if task is not None:
            if not task.done():
                return task, False
            self._listener_cleanup_tasks.pop(stage, None)
            if not task.cancelled():
                try:
                    task.result()
                except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                    pass
                else:
                    return None, True

        try:
            stage_slug = stage.lower().replace(" ", "-")
            task_name = f"easycat-voice-server-listener-cleanup-{stage_slug}"
            task = self._listener_cleanup_task_scope.create_task(
                self._run_listener_cleanup(cleanup),
                task_name=task_name,
            )
            assert task is not None
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self._record_cleanup_error(stage, exc, cleanup_errors)
            return None, False
        self._listener_cleanup_tasks[stage] = task
        return task, False

    @staticmethod
    async def _run_listener_cleanup(cleanup: Callable[[], Awaitable[Any]]) -> Any:
        """Invoke one listener cleanup operation inside its named owner task."""
        return await cleanup()

    async def _wait_for_listener_cleanup_task(
        self,
        task: asyncio.Task[Any],
        *,
        cancel_on_timeout: bool,
    ) -> _ListenerCleanupWaitResult:
        """Wait one bounded slice while preserving a listener cleanup task's ownership."""
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=max(self.config.force_shutdown_timeout_s, 0.0),
            )
        except asyncio.CancelledError:
            # Let a cooperative listener cleanup terminate with its owning
            # stop call. A cancellation-resistant task stays retained in the
            # mapping and a later stop waits that same task rather than racing
            # a second cleanup invocation.
            if not task.done():
                task.cancel()
            raise
        if task in done:
            return _ListenerCleanupWaitResult.COMPLETED
        if cancel_on_timeout and not task.done():
            task.cancel()
            # Preserve the old hard-timeout behavior: request cooperative
            # cancellation and give it one event-loop turn without waiting for
            # a cancellation-resistant listener.
            await asyncio.sleep(0)
            if task.done():
                if task.cancelled():
                    return _ListenerCleanupWaitResult.COOPERATIVELY_CANCELLED
                return _ListenerCleanupWaitResult.COMPLETED
        return _ListenerCleanupWaitResult.RETAINED

    async def _finish_listener_cleanup_task(
        self,
        stage: str,
        task: asyncio.Task[Any],
        cleanup_errors: list[Exception],
    ) -> bool:
        """Reap a completed listener cleanup task and preserve caller cancellation."""
        self._listener_cleanup_tasks.pop(stage, None)
        current_task = asyncio.current_task()
        cancellation_requests = current_task.cancelling() if current_task is not None else 0
        try:
            await task
        except asyncio.CancelledError:
            # This is the listener task's own cancellation, not a cancellation
            # of ``stop()``. The latter can arrive just after ``asyncio.wait``
            # returns, so preserve it rather than converting it to a cleanup
            # failure.
            if current_task is not None and current_task.cancelling() > cancellation_requests:
                raise
            cancelled_error = RuntimeError(f"{stage} was cancelled before it completed")
            logger.warning("VoiceServer: %s", cancelled_error)
            cleanup_errors.append(cancelled_error)
            return False
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self._record_cleanup_error(stage, exc, cleanup_errors)
            return False
        return True

    async def _drain_sessions_for_stop(
        self,
        *,
        force: bool,
        await_natural_end: bool,
    ) -> None:
        """Drain sessions after listeners stop accepting, escalating at the deadline."""
        drain_timeout = 0.0 if force else self.config.drain_timeout_s
        if await_natural_end:
            drain_timeout = await self._await_natural_drain_or_escalate(drain_timeout)
        await self._gate.drain(
            self._active_session_pairs,
            drain_timeout_s=drain_timeout,
            force_after=True,
            force_timeout_s=self.config.force_shutdown_timeout_s,
            stop_for_key=self._stop_managed_session,
        )

    async def _close_listeners_for_drain(
        self,
        *,
        await_natural_end: bool,
        cleanup_errors: list[Exception],
    ) -> Any:
        """Stop accepting new work and return the raw-ws server to await later."""
        ws_server = self._ws_server
        if ws_server is not None:
            try:
                if await_natural_end:
                    ws_server.close(close_connections=False)
                else:
                    ws_server.close()
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                self._record_cleanup_error(
                    "raw-WebSocket listener close",
                    exc,
                    cleanup_errors,
                )
        if self._site is not None:
            site = self._site
            succeeded = await self._attempt_bounded_listener_cleanup(
                "HTTP site stop",
                site.stop,
                cleanup_errors,
            )
            if succeeded and self._site is site:
                self._site = None
        site_cleanup = self._listener_cleanup_tasks.get("HTTP site stop")
        site_cleanup_pending = site_cleanup is not None and not site_cleanup.done()
        if self._runner is not None and not site_cleanup_pending:
            runner = self._runner
            succeeded = await self._attempt_bounded_listener_cleanup(
                "HTTP runner cleanup",
                runner.cleanup,
                cleanup_errors,
            )
            if succeeded and self._runner is runner:
                self._runner = None
        return ws_server

    async def _await_natural_drain_or_escalate(self, drain_timeout: float) -> float:
        """Wait for caller hangup; return the remaining stop-session grace window."""
        if await self._gate.wait_drained(timeout_s=drain_timeout):
            return 0.0
        self._await_natural_end_drain = False
        # ``await_natural_end`` spends the configured grace window waiting for
        # callers to hang up. Once it expires, stragglers move directly to the
        # existing force path rather than receiving a second full grace window.
        await self._close_active_ws_connections()
        return 0.0

    async def _close_active_ws_connections(self) -> None:
        """Close still-active raw WebSocket connections after natural drain expires."""
        connections = list(self._ws_connections.items())
        if not connections:
            return
        close_tasks: list[asyncio.Task[Any]] = []
        for key, ws in connections:
            task = self._ws_close_task_scope.create_task(
                ws.close(code=1001, reason=_WS_DRAINING_CLOSE_REASON),
                task_name=f"easycat-raw-ws-close-{key}",
            )
            assert task is not None
            close_tasks.append(task)
        close_group = asyncio.gather(*close_tasks, return_exceptions=True)
        closed = await wait_for_owned_future(
            close_group,
            timeout_s=max(self.config.force_shutdown_timeout_s, 0.0),
        )
        if closed:
            self._report_shutdown_task_results(
                "raw-WebSocket close",
                close_tasks,
                close_group.result(),
                explicitly_cancelled=set(),
            )
        await self._ws_close_task_scope.release_standalone_if_empty()
        if not closed:
            logger.warning(
                "VoiceServer: raw-ws connections did not close within "
                "force_shutdown_timeout_s=%ss; cancelling handlers",
                self.config.force_shutdown_timeout_s,
            )

    @staticmethod
    def _report_shutdown_task_results(
        stage: str,
        tasks: list[asyncio.Task[Any]],
        results: list[Any],
        *,
        explicitly_cancelled: set[asyncio.Task[Any]],
    ) -> None:
        """Log unexpected shutdown failures with the owning task's identity."""
        for task, result in zip(tasks, results, strict=True):
            if not isinstance(result, BaseException):
                continue
            if isinstance(result, asyncio.CancelledError) and task in explicitly_cancelled:
                continue
            logger.error(
                "VoiceServer %s task %s failed",
                stage,
                task.get_name(),
                exc_info=result,
            )

    @staticmethod
    def _report_late_shutdown_task_result(stage: str, task: asyncio.Task[Any]) -> None:
        """Report a shutdown worker that fails after its hard deadline."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "VoiceServer %s task %s failed",
                stage,
                task.get_name(),
                exc_info=error,
            )

    def _reset_gate_bookkeeping(self) -> None:
        """Reset the gate's active set, reservations, AND draining flag after a drain.

        Idempotent: drops every remaining active key and returns every reserved
        slot so a stopped server reports ``active_sessions == 0`` even though the
        draining handlers skipped their own untrack/release. Crucially it ALSO
        clears the draining flag the drain set in :meth:`stop`: without this a
        stop-then-:meth:`start` reuse would re-bind the listeners but leave the
        shared gate draining, so readiness never recovers and every new ``/ws`` /
        WebRTC session is rejected as "draining".
        """
        for key in self._gate.active_keys():
            self._gate.untrack(key)
        while self._gate.reserved_count > 0:
            self._gate.release()
        self._gate.stop_draining()
        self._ws_connections.clear()

    async def _cancel_ws_handler_tasks(self, *, timeout_s: float | None = None) -> None:
        """Cancel + await any ``/ws`` handler task still running.

        Called during :meth:`stop` after the drain so a handler hung in
        ``ws.wait_closed()`` (e.g. a client that never finished the close
        handshake) cannot keep the raw-ws ``Server._close`` waiter — which
        ``asyncio.wait``s on its handlers rather than cancelling them — blocked
        forever. The current task (``stop`` itself never runs as a handler) is
        excluded defensively.
        """
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._ws_handler_task_scope.tasks()
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            gathered = asyncio.gather(*tasks, return_exceptions=True)
            results: list[Any | BaseException] | None
            if timeout_s is None:
                results = await gathered
            else:
                completed = await wait_for_owned_future(gathered, timeout_s=timeout_s)
                results = gathered.result() if completed else None
            if results is not None:
                self._report_shutdown_task_results(
                    "raw-WebSocket handler",
                    tasks,
                    results,
                    explicitly_cancelled=set(tasks),
                )
            else:
                report_late = partial(
                    self._report_late_shutdown_task_result,
                    "raw-WebSocket handler",
                )
                for task in tasks:
                    task.add_done_callback(report_late)
        await self._ws_handler_task_scope.release_standalone_if_empty()

    def _active_session_pairs(self) -> list[tuple[int, Any]]:
        """Return the ``(key, session)`` pairs still active (for the drain step)."""
        return [
            (key, self._active_session_objs[key])
            for key in self._gate.active_keys()
            if key in self._active_session_objs
        ]

    async def _stop_managed_session(self, key: int, force: bool) -> None:
        """Route drain teardown through the manager's keyed stop ownership."""
        await self._manager.remove(key, force=force)

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
        # The manifest loaded; the plan may still be unbuildable (e.g. an unknown
        # provider/backend shortcut such as ``stt = "opnai"`` or ``vad =
        # "silro"``). Surface that as a plan blocking error, never a raised probe
        # (a raised health check breaks k8s liveness/readiness outright). The
        # reason stays a content-free token so the body leaks nothing.
        plan, _error = self._resolve_profile_plan(self.config.profile)
        if plan is None:
            return True, ("plan_unresolvable",)
        return True, plan.blocking_errors()

    def _resolve_profile_plan(self, profile: str) -> tuple[Any | None, str | None]:
        """Build the provider plan for *profile*, or return a redacted error.

        The planner RAISES on an unresolvable profile (an unknown provider /
        backend shortcut) to preserve the planner-vs-``create_session`` parity
        contract. The readiness probe and the read-only ``/plan`` /
        ``/capabilities`` endpoints must surface that as structured data, NOT a
        500 — they are the exact endpoints an operator reaches to diagnose a red
        ``/health/ready``, and a raised aiohttp handler would 500 the
        diagnostic. Returns ``(plan, None)`` on success or ``(None, error)`` with
        a redacted, content-bounded error string on failure.
        """
        from easycat.planning import build_provider_plan

        try:
            return build_provider_plan(self._manifest.profile(profile), profile=profile), None
        except Exception as exc:
            from easycat.validation.redaction import redact_value

            logger.warning(
                "VoiceServer: provider plan for profile %r is unresolvable; "
                "reporting a plan blocking error",
                profile,
                exc_info=True,
            )
            return None, f"plan_unresolvable: {redact_value(str(exc))}"

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
        profile = self.config.profile
        plan, error = self._resolve_profile_plan(profile)
        if plan is None:
            # The manifest loaded but the profile is unbuildable. Return a
            # structured plan-with-blocking-errors (HTTP 200), never a 500.
            return {
                "profile": profile,
                "selected": {},
                "missing_env": [],
                "missing_extras": [],
                "warnings": [],
                "blocking_errors": [error],
                "has_blocking_errors": True,
                "manifest_loaded": True,
            }
        from easycat.planning import selection_to_dict

        return {
            "profile": plan.profile,
            "selected": {
                role: selection_to_dict(selection) for role, selection in plan.selected.items()
            },
            "missing_env": list(plan.missing_env),
            "missing_extras": list(plan.missing_extras),
            "warnings": list(plan.warnings),
            "blocking_errors": list(plan.blocking_errors()),
            "has_blocking_errors": plan.has_blocking_errors,
            "manifest_loaded": True,
        }

    def metrics_payload(self) -> dict[str, Any]:
        """Return the read-only ``GET /metrics`` JSON payload (M8).

        A PII-safe snapshot of the in-process server-side counters/gauges
        (stable in CI independent of any OTel SDK, which the
        ``easycat._observability`` instruments require to read back). The shape
        is a stable key set; it never includes session IDs, IPs, tokens, or raw
        paths.
        """
        return {
            "active_sessions": self._gate.reserved_count,
            "max_sessions": self.config.max_sessions,
            "draining": self._gate.is_draining,
            "requests_total": self._requests_total,
            "sessions_rejected_total": self._sessions_rejected_total,
        }

    def manifest_payload(self) -> dict[str, Any]:
        """Return the read-only ``GET /manifest`` JSON payload (M8).

        For a ``from_manifest`` server returns ``{"loaded": True, "manifest":
        <redacted>}`` where the redacted dump
        (:meth:`ProjectManifest.to_redacted_dict`) carries only the
        ``bearer-env:NAME`` reference under ``*_ref`` keys and routes every value
        through ``redact_value`` — so no resolved token can appear. For a
        factory-only server (no manifest) returns the documented absent shape
        ``{"loaded": False, "manifest": None}``. This NEVER calls
        :meth:`resolve_auth` (which reads the env token).
        """
        if self._manifest is None:
            return {"loaded": False, "manifest": None}
        return {"loaded": True, "manifest": self._manifest.to_redacted_dict()}

    def capabilities_payload(self) -> dict[str, Any]:
        """Return the read-only ``GET /capabilities`` JSON payload (M8).

        For a ``from_manifest`` server, aggregates the parity-gated planner's
        declared capability strings across the seven roles into ``{"profile",
        "roles": {role: sorted(capabilities)}, "all_capabilities":
        sorted(union)}``. For a factory-only server returns the documented empty
        shape ``{"profile", "roles": {}, "all_capabilities": []}``.

        The planner reads only provider metadata (names / extras / env-var NAMES
        / declared capability strings) — never secret values — so no token can
        appear. ``build_provider_plan`` is imported LAZILY here (like
        :meth:`plan_payload`) so this method does not pull the planner at module
        load (the M4 import boundary).
        """
        if self._manifest is None:
            return {"profile": self.config.profile, "roles": {}, "all_capabilities": []}
        profile = self.config.profile
        plan, _error = self._resolve_profile_plan(profile)
        if plan is None:
            # Unbuildable profile: no capabilities resolvable. Return the
            # documented empty shape (HTTP 200), never a 500.
            return {"profile": profile, "roles": {}, "all_capabilities": []}
        roles = {role: sorted(selection.capabilities) for role, selection in plan.selected.items()}
        union: set[str] = set()
        for caps in roles.values():
            union.update(caps)
        return {
            "profile": plan.profile,
            "roles": roles,
            "all_capabilities": sorted(union),
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

        def _factory(transport: Any) -> EasyConfig:
            # A fresh EasyConfig bound to the already-negotiated connection
            # transport. The manifest's
            # transport shortcut only selects the preset (browser/websocket/…) and
            # its provider defaults; ``to_easyconfig`` would otherwise hand back a
            # standalone transport that opens a SECOND listener/peer instead of
            # driving the accepted socket. Override it with the live transport so
            # manifest-backed websocket/webrtc serving uses the connected client —
            # mirrors the per-connection factory ``from_app`` builds.
            config = manifest.to_easyconfig(profile)
            config.transport = transport
            return config

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

        from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES

        return await authorized_bind(
            self.config.host,
            auth=self.config.auth,
            unsafe_allow_no_auth=self.config.unsafe_allow_no_auth,
            binder=lambda bind_host: websockets.serve(
                self._handle_websocket_connection,
                bind_host,
                self._websocket_port(),
                compression=None,
                max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
            ),
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
        + session via the ``session_factory`` and drive its lifetime here; the
        gate tracks the key + session so the graceful-drain step can force-stop it.

        Drain-ownership rule (review fix): the per-connection teardown is
        drain-aware. On a normal client disconnect (not draining) the handler
        stops its own session GRACEFULLY. During drain, the handler does NOT
        stop the session itself — the shared :meth:`CapacityGate.drain` owns the
        single stop (graceful, then force / cancel on timeout). This matters
        because the real :meth:`Session.stop` has a ``_stopping`` idempotency
        guard: a graceful stop already in progress turns a later ``force=True``
        call into a no-op, so a handler that started its own graceful stop could
        never be force-preempted and a hung teardown would deadlock ``stop()``.
        Letting the drain start the single stop keeps the escalation effective.
        """
        from easycat.transports.websocket import WebSocketConnectionTransport

        # Register this handler task so :meth:`stop` can cancel it if it hangs
        # in ``ws.wait_closed()`` (otherwise ``ws_server.wait_closed()`` would
        # block forever on the surviving handler).
        task = asyncio.current_task()
        if task is not None:
            task.set_name(f"easycat-raw-ws-handler-{id(ws)}")
            self._ws_handler_task_scope.adopt_task(task)
        try:
            if self._gate.is_draining:
                self._emit_session_rejected(server_state="draining")
                await ws.close(code=_WS_OVER_CAPACITY_CLOSE_CODE, reason=_WS_DRAINING_CLOSE_REASON)
                return

            auth_reason = self._websocket_auth_reason(ws)
            if auth_reason != "allowed":
                self._emit_session_rejected(server_state="serving", auth_result=auth_reason)
                await ws.close(
                    code=_WS_UNAUTHORIZED_CLOSE_CODE,
                    reason=_WS_UNAUTHORIZED_CLOSE_REASON,
                )
                return

            if self._session_factory is None:
                # No factory configured (e.g. a health-only server); reject cleanly
                # rather than crashing the listener task.
                self._emit_session_rejected(server_state="serving")
                await ws.close(
                    code=_WS_OVER_CAPACITY_CLOSE_CODE,
                    reason="No session factory configured",
                )
                return

            if not self._gate.try_acquire():
                self._emit_session_rejected(server_state="serving")
                await ws.close(
                    code=_WS_OVER_CAPACITY_CLOSE_CODE,
                    reason=_WS_OVER_CAPACITY_CLOSE_REASON,
                )
                return

            key = id(ws)
            try:
                self._ws_connections[key] = ws
                transport = WebSocketConnectionTransport(ws)
                session = self._build_session(transport)
                self._gate.track(key)
                self._active_session_objs[key] = session
                self._emit_connections_active()
                await self._manager.add(key, session)
                try:
                    await ws.wait_closed()
                finally:
                    await self._teardown_ws_session(key)
            finally:
                # In stop-sessions drain mode, the shared drain owns BOTH the
                # force-stop and gate/objs/release bookkeeping. In natural-end
                # mode, a caller hangup is the desired completion signal, so the
                # handler performs its normal cleanup even while the gate is
                # draining.
                if not self._gate.is_draining or self._await_natural_end_drain:
                    self._gate.untrack(key)
                    self._active_session_objs.pop(key, None)
                    self._gate.release()
                    self._emit_connections_active()
                self._ws_connections.pop(key, None)
        finally:
            if task is not None:
                self._ws_handler_task_scope.discard_task(task)

    async def _teardown_ws_session(self, key: int) -> None:
        """Tear down one ``/ws`` session, deferring to the drain when draining.

        When NOT draining (a normal client disconnect), stop the session
        GRACEFULLY and only then drop it from the registry. When draining, leave the
        session in the active set and registry untouched so the shared
        :meth:`CapacityGate.drain` owns the single stop. The drain starts the
        sole graceful ``session.stop()`` itself and cancels / force-escalates it
        on timeout, keeping one lifecycle authority during drain. During a
        natural-end drain the handler may begin graceful removal after caller
        hangup; ``SessionManager.remove`` retains that entry until stop succeeds.
        If the drain deadline cancels the handler, the final force sweep can
        therefore retry the still-registered session and complete backend
        teardown.
        """
        if self._gate.is_draining and not self._await_natural_end_drain:
            return
        await self._manager.remove(key)

    def _reconcile_query_token_opt_in(self) -> None:
        """Thread ``config.allow_query_token`` onto the configured auth policy.

        Makes the process-level ``allow_query_token`` field LIVE: when it is set
        and ``config.auth`` is a token policy exposing an ``allow_query_token``
        attribute, opt that policy in so its ``?token=`` query auth is accepted.
        This is opt-in ONLY — it never disables a policy that already enabled
        query tokens — and it covers both the ``/ws`` and ``/webrtc`` paths,
        which authorize through ``self.config.auth``.
        """
        if not self.config.allow_query_token:
            return
        auth = self.config.auth
        if auth is None or not hasattr(auth, "allow_query_token"):
            return
        if auth.allow_query_token:
            return  # already opted in — nothing to reconcile
        # Opt in WITHOUT mutating the caller-owned policy object (a single
        # ``BearerTokenAuth`` may be shared across servers). Store a fresh copy on
        # THIS server's config instead; it is read by both the ``/ws`` path and
        # the WebRTC routes, which are built after this runs.
        if is_dataclass(auth) and not isinstance(auth, type):
            self.config.auth = replace(auth, allow_query_token=True)
        else:  # pragma: no cover - non-dataclass custom policy
            auth.allow_query_token = True

    def _websocket_auth_reason(self, ws: Any) -> str:
        """Authorize a ``/ws`` handshake through the unified ``AuthPolicy``.

        Returns the ``AuthReason`` Literal: ``"allowed"`` when no auth policy is
        configured (the loopback/dev default) or the credential is valid;
        otherwise ``"missing"`` / ``"invalid"`` so the rejection metric can carry
        the right ``easycat.auth_result`` label. When a policy is set, the
        handshake ``Authorization`` header and ``?token=`` query are read from the
        accepted ``ServerConnection``'s request and run through
        :meth:`AuthPolicy.authorize`. Never logs tokens.
        """
        auth = self.config.auth
        if auth is None:
            return "allowed"
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
        path = getattr(request, "path", "") or ""
        result = auth.authorize(from_websocket(headers, path))
        return result.reason

    # ── Metric emission (M8) ─────────────────────────────────────────
    # Each helper bumps the in-process snapshot (read back by ``/metrics``) AND
    # emits the registered OTel metric (a no-op without an SDK, never raising on
    # the registered names/labels). Emission lives in the real lifecycle paths so
    # the same-PR registration is genuine, not test-only.

    def _server_state(self) -> ServerState:
        """Return the ``ServerState`` Literal for the current draining flag."""
        return "draining" if self._gate.is_draining else "serving"

    def _emit_draining(self, is_draining: bool) -> None:
        from easycat.server import metrics as server_metrics

        server_metrics.observe_draining(is_draining)

    def _emit_connections_active(self) -> None:
        from easycat.server import metrics as server_metrics

        server_metrics.observe_connections_active(
            self._gate.reserved_count,
            server_state=self._server_state(),
        )

    def _emit_connections_active_cleared(self) -> None:
        """Reset the active-connections gauge to 0 on BOTH ``server_state`` series.

        The gauge is push-cached per ``server_state`` label, but the drain path
        never calls :meth:`_emit_connections_active` (handlers skip their own
        release while draining). Without this the ``serving`` series would stay
        pinned at its last non-zero value indefinitely after a drain — and a
        later server reusing the same process would inherit the stale reading.
        Emit 0 for both label values so neither series is left stale.
        """
        from easycat.server import metrics as server_metrics

        for state in ("serving", "draining"):
            server_metrics.observe_connections_active(0, server_state=state)

    def _emit_session_rejected(self, *, server_state: str, auth_result: str | None = None) -> None:
        from easycat.server import metrics as server_metrics

        self._sessions_rejected_total += 1
        server_metrics.record_session_rejected(
            server_state=server_state,  # type: ignore[arg-type]
            auth_result=auth_result,  # type: ignore[arg-type]
        )

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
