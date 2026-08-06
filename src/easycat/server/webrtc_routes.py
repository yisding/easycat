"""Transport-instance-free WebRTC route unit — the M7 decoupling seam.

Before M7 the multi-session WebRTC signaling server reused the WebRTC route
handlers by binding them to a throwaway ``shim = WebRTCTransport(settings)``
inside the standalone WebRTC serve helper. That shim existed ONLY to lend its
bound methods (``_handle_config`` / ``_handle_stats`` / ``_handle_root`` /
``_handle_cors_preflight`` / ``_cors_headers``) and its per-process stats
rate-limit deque to the serve helper's nested route closures. It was never a
real peer connection.

This module lifts that stateless route logic OFF :class:`WebRTCTransport` into
:class:`WebRTCRoutes`, a small stateful collaborator that needs NO transport
instance for the config/stats/root/cors/health routes. The shared
:class:`~easycat.server.voice_server.VoiceServer` mounts it as aiohttp routes
(namespaced under ``/webrtc/*``) and the standalone serve helper delegates to it
(flat ``/offer`` / ``/config`` / ``/stats``), so both surfaces share one
implementation.

Decoupling seam (which :class:`WebRTCTransport` methods were stateless vs.
genuinely per-peer):

* Stateless / shim-only (need no transport instance). As of QS6 these no longer
  live on either class as byte-identical copies: the config / stats / health /
  root / cors handlers and their CORS / auth / stats helpers are LIFTED ONCE into
  :class:`~easycat.server._webrtc_handlers.WebRTCSignalingHandlers`, and both
  :class:`WebRTCRoutes` (via :meth:`_signaling`) and :class:`WebRTCTransport`
  delegate to it. Auth is routed through the UNIFIED
  :class:`~easycat.server.auth.AuthPolicy` (``from_aiohttp_request`` +
  ``AuthPolicy.authorize``), so WebSocket and WebRTC share one auth layer and the
  ``allow_query_token`` default-off posture applies here. The stats rate-limit /
  record window stays PER-SERVER: :class:`WebRTCRoutes` owns a
  :class:`~easycat.transports._webrtc_stats.WebRTCStatsState` (``_stats_state``) passed
  into the shared handlers, so the quota semantics are per-server (identical to
  the pre-M7 shim's per-process deque, one instance per routes unit).

* Genuinely per-peer (KEPT on :class:`WebRTCTransport`, constructed per offer):

  - ``_handle_offer`` / ``_handle_offer_locked`` (negotiates the
    ``RTCPeerConnection``), ``_prepare_external_signaling``, and all
    peer/track/consume/events state. :meth:`WebRTCRoutes.handle_offer` reserves
    through the shared :class:`CapacityGate`, builds a real per-offer
    ``WebRTCTransport`` (with ``auth_token=None`` because auth already ran at
    this unified layer), drives the negotiation, then builds the session via the
    injected ``config_factory`` and tracks it on the gate + manager.

Import weight: aiohttp/aiortc are gated lazily inside :meth:`register` and the
handlers (via the per-offer transport's own ``require_module``), and
:class:`WebRTCTransport` is imported lazily inside the offer handler, so
``import easycat.server`` stays light and pulls no planner/aiohttp at module
load.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from easycat.runtime._event_tasks import RuntimeTaskScope, wait_for_owned_future
from easycat.server._webrtc_handlers import WebRTCSignalingHandlers
from easycat.session_manager import SessionStopReport, log_session_stop_failures
from easycat.teardown_budgets import (
    SERVER_DRAIN_TIMEOUT_S,
    STANDALONE_WEBRTC_FORCE_SHUTDOWN_TIMEOUT_S,
)
from easycat.transports._webrtc_config import WebRTCTransportConfig
from easycat.transports._webrtc_stats import WebRTCStatsState

if TYPE_CHECKING:
    from easycat.config import EasyConfig
    from easycat.server.auth import AuthPolicy
    from easycat.server.transports import CapacityGate
    from easycat.session import Session
    from easycat.session_manager import SessionManager
    from easycat.transports.webrtc import WebRTCTransport

logger = logging.getLogger(__name__)

_OFFER_CLEANUP_TASK = "webrtc_offer_cleanup"
_OFFER_CLEANUP_COHORT = "webrtc-offer-cleanup"
_FORCE_CLEANUP_TASK = "webrtc_force_cleanup"
_FORCE_CLEANUP_COHORT = "webrtc-force-cleanup"
_STANDALONE_SWEEP_TASK = "standalone_webrtc_session_sweep"
_STANDALONE_SWEEP_COHORT = "standalone-webrtc-session-sweep"

# Per-connection factory seam (NO ``ConnectionContext`` type): a per-transport
# ``Callable[[WebRTCTransport], EasyConfig | Session]``.
WebRTCConfigFactory = Callable[["WebRTCTransport"], "EasyConfig | Session"]


def _standalone_sweep_error(
    *,
    swept: bool,
    sweep_task: asyncio.Task[SessionStopReport[int]],
    timeout_s: float,
) -> RuntimeError | None:
    """Surface a completed sweep report without losing hard-timeout behavior."""
    if not swept:
        logger.warning(
            "Standalone WebRTC session cleanup exceeded %.2fs; abandoning final sweep",
            timeout_s,
        )
        return None
    report = sweep_task.result()
    if not isinstance(report, SessionStopReport) or not log_session_stop_failures(
        report,
        context="Standalone WebRTC session shutdown",
        log=logger,
    ):
        return None
    return RuntimeError(f"Standalone WebRTC shutdown retained {len(report.failures)} session(s)")


class WebRTCRoutes:
    """Mount the WebRTC signaling routes without a throwaway transport shim.

    Construct from a :class:`WebRTCTransportConfig`, an optional unified
    :class:`AuthPolicy`, a per-connection ``config_factory``, the shared
    :class:`CapacityGate`, and the :class:`SessionManager`. The config / stats /
    root / cors / health handlers are pure functions of the config + this
    object's per-server state and require NO :class:`WebRTCTransport` instance.
    Only :meth:`handle_offer` constructs a real per-offer transport (the
    legitimate peer connection).

    Capacity + draining are owned by the injected :class:`CapacityGate` so they
    behave identically across WebRTC offers and ``/ws`` connections when the gate
    is shared (the :class:`VoiceServer` case). The created session is registered
    into ``active_session_objs`` keyed by the gate key so the server's drain step
    force-stops it on shutdown.
    """

    def __init__(
        self,
        config: WebRTCTransportConfig,
        *,
        auth: AuthPolicy | None,
        config_factory: WebRTCConfigFactory,
        gate: CapacityGate[int],
        manager: SessionManager[int],
        runtime_feedback: bool = True,
        active_session_objs: dict[int, Any] | None = None,
        on_session_rejected: Callable[..., None] | None = None,
        on_connections_changed: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._config_factory = config_factory
        self._gate = gate
        self._manager = manager
        self._runtime_feedback = runtime_feedback
        # Optional rejection-metric sink, injected by ``VoiceServer`` so an offer
        # rejected at this unified layer (auth / draining / capacity) feeds the
        # SAME ``/metrics`` snapshot + ``easycat.server.sessions.rejected.total``
        # counter as a ``/ws`` rejection — both transports share one gate, so the
        # rejection counts must span both. The standalone serve helper passes
        # ``None`` (it owns no server-level rejection metric).
        self._on_session_rejected = on_session_rejected
        # Optional active-connection gauge sink, injected by ``VoiceServer`` so an
        # ACCEPTED offer (and its later teardown) updates the SAME
        # ``easycat.server.connections.active`` OTel gauge as a ``/ws`` session.
        # Both transports reserve on the one shared gate, so the gauge must span
        # both — otherwise OTel alerts / autoscaling miss WebRTC-only traffic even
        # though the JSON ``/metrics`` snapshot (which reads the gate directly)
        # stays correct. The standalone serve helper passes ``None``.
        self._on_connections_changed = on_connections_changed
        # When the server shares an active-session map across transports (the
        # ``VoiceServer`` case), the offer handler registers the created session
        # here keyed by the gate key so the shared drain step force-stops it.
        self._active_session_objs = active_session_objs
        # Per-server stats rate-limit / record state, shared with each lazily
        # built signaling-handlers instance (see ``_signaling``). Per-server (not
        # per-request), matching the previous behavior exactly.
        self._stats_state = WebRTCStatsState()
        # Set by ``register`` when a bundled client is served so ``handle_root``
        # redirects to it (mirrors ``WebRTCTransport._has_bundled_client``).
        self._has_bundled_client = False
        # The route prefix the bundled client must target (``""`` for the flat
        # helper, ``/webrtc`` for the mounted server). Threaded into the root
        # redirect so the served client points at the right routes.
        self._client_base = ""
        # Owns per-offer transport cleanup tasks so ``stop`` can cancel them.
        self._cleanup_task_scope = RuntimeTaskScope(
            owner_label="webrtc-routes",
            member_name=_OFFER_CLEANUP_TASK,
            cohort=_OFFER_CLEANUP_COHORT,
            logger=logger,
            failure_message="WebRTC offer cleanup task failed",
            drop_if_closed=False,
            release_standalone_when_idle=True,
        )
        self._force_cleanup_task_scope = RuntimeTaskScope(
            owner_label="webrtc-routes-force-cleanup",
            member_name=_FORCE_CLEANUP_TASK,
            cohort=_FORCE_CLEANUP_COHORT,
            logger=logger,
            failure_message="WebRTC forced cleanup task failed",
            drop_if_closed=False,
            release_standalone_when_idle=True,
        )
        self._cleanup_task_keys: dict[asyncio.Task[Any], int] = {}
        self._released_cleanup_keys: set[int] = set()
        # aiohttp.web, resolved lazily inside ``register``.
        self._web: Any = None

    # ── Registration ─────────────────────────────────────────────────

    def register(self, app: Any, *, prefix: str = "/webrtc", web: Any = None) -> None:
        """Wire the WebRTC routes onto ``app`` under ``prefix``.

        ``prefix=""`` keeps the FLAT ``/offer`` / ``/config`` / ``/stats`` paths
        that the standalone serve helper and the bundled client (flat mode) rely
        on. ``prefix="/webrtc"`` (the :class:`VoiceServer` default) mounts the
        namespaced routes from the Endpoint Set.

        The root (``GET /``) + static mount registration differs by surface:

        * Flat helper (``prefix=""``): root is always registered (it serves the
          bundled-client redirect when present, else the JSON endpoint hint), and
          the static dir is mounted when configured — byte-identical to the old
          serve helper.
        * Namespaced server (``prefix="/webrtc"``): root + static are registered
          ONLY when a static dir is configured, so a health-only server does not
          claim ``/`` (the :class:`VoiceServer` mounts its own ``/health`` family
          on the same app).

        ``web`` may be passed when the caller already resolved ``aiohttp.web``
        (the serve helper does); otherwise it is resolved lazily here.
        """
        if web is None:
            from easycat._extras import require_module

            web = require_module("aiohttp.web", extra="webrtc", purpose="WebRTC signaling")
        self._web = web
        self._client_base = prefix

        app.router.add_post(f"{prefix}/offer", self.handle_offer)
        app.router.add_post(f"{prefix}/stats", self.handle_stats)
        app.router.add_get(f"{prefix}/config", self.handle_config)
        app.router.add_get(f"{prefix}/health", self.handle_health)
        app.router.add_options(f"{prefix}/offer", self.handle_cors_preflight)
        app.router.add_options(f"{prefix}/stats", self.handle_cors_preflight)
        app.router.add_options(f"{prefix}/config", self.handle_cors_preflight)

        self._register_root_and_static(app, flat=prefix == "")

    def _register_root_and_static(self, app: Any, *, flat: bool) -> None:
        """Mount the root redirect + static client dir.

        Resolves the bundled-client sentinel, sets ``_has_bundled_client`` so
        :meth:`handle_root` redirects, and serves the static files from ``/``.
        For the flat helper, root is registered even with no static dir (it
        serves the JSON endpoint hint), preserving the old serve-helper behavior.
        For the namespaced server, root + static register only when a static dir
        is configured so the server's own ``/`` ownership is not double-claimed.
        """
        static_dir = self._config.static_dir
        if static_dir == WebRTCTransportConfig._USE_BUNDLED:
            static_dir = WebRTCTransportConfig._BUNDLED_STATIC_DIR

        static_path: Path | None = None
        if static_dir is not None:
            candidate = Path(static_dir)
            if candidate.is_dir():
                static_path = candidate
                if (candidate / "webrtc_client.html").is_file():
                    self._has_bundled_client = True
            else:
                logger.warning(
                    "Configured static_dir '%s' does not exist or is not a directory; "
                    "static file serving is disabled",
                    candidate,
                )

        if not flat and static_path is None:
            # Namespaced server with no static dir: do not claim ``/``.
            return

        # Register the root redirect before the catch-all static mount so the
        # bundled-client redirect (with the right ``?webrtc=`` base) takes effect.
        app.router.add_get("/", self.handle_root)
        if static_path is not None:
            app.router.add_static("/", static_path)
            logger.info("Serving static files from %s", static_path)

    # ── Shared stateless signaling surface ───────────────────────────

    def _signaling(self) -> WebRTCSignalingHandlers:
        """Build the shared stateless signaling surface from current state.

        The config / stats / health / root / cors handlers and their CORS / auth
        / stats helpers live ONCE in
        :class:`~easycat.server._webrtc_handlers.WebRTCSignalingHandlers`; this
        builds it fresh from the live ``_web`` / ``_has_bundled_client`` /
        ``_client_base`` (set in :meth:`register`). The per-server rate-limit /
        record state persists across calls via the shared ``self._stats_state``.
        """
        return WebRTCSignalingHandlers(
            self._config,
            web=self._web,
            auth=self._auth,
            stats=self._stats_state,
            has_bundled_client=self._has_bundled_client,
            client_base=self._client_base,
            health_payload=self._build_health_payload,
        )

    def _build_health_payload(self) -> dict[str, Any]:
        """Capacity JSON spanning the shared gate (the routes' ``/health`` body)."""
        return {
            "status": "draining" if self._gate.is_draining else "ok",
            "active_sessions": self._gate.active_count,
            "max_sessions": self._config.max_sessions,
        }

    # ── Auth (unified) ───────────────────────────────────────────────

    def _authorized(self, request: Any) -> bool:
        """Authorize ``request`` through the UNIFIED :class:`AuthPolicy` (bool shim)."""
        return self._signaling().authorized(request)

    def _auth_reason(self, request: Any) -> str:
        """Return the ``AuthReason`` for ``request`` through the UNIFIED policy.

        Delegates to the shared :class:`WebRTCSignalingHandlers`, which routes
        through ``server.auth.from_aiohttp_request`` + ``AuthPolicy.authorize`` so
        the WebSocket and WebRTC paths share one auth layer and the
        ``allow_query_token`` default-off posture applies to mounted WebRTC. No
        policy configured means open access (``"allowed"``). Returns the
        ``AuthReason`` Literal so a rejection metric carries the right
        ``easycat.auth_result`` label.
        """
        return self._signaling().auth_reason(request)

    def _server_state(self) -> str:
        """Return the ``ServerState`` Literal for the shared gate's draining flag."""
        return "draining" if self._gate.is_draining else "serving"

    def _record_rejection(self, *, server_state: str, auth_result: str | None = None) -> None:
        """Forward an offer rejection to the owning server's rejection metric.

        A no-op for the standalone serve helper (no callback injected). When
        ``VoiceServer`` mounts these routes it injects ``_emit_session_rejected``,
        so a WebRTC offer rejected here counts toward the same ``/metrics``
        snapshot + ``easycat.server.sessions.rejected.total`` counter as a ``/ws``
        rejection.
        """
        if self._on_session_rejected is not None:
            self._on_session_rejected(server_state=server_state, auth_result=auth_result)

    def _emit_connections_changed(self) -> None:
        """Forward an active-connection count change to the owning server's gauge.

        A no-op for the standalone serve helper (no callback injected). When
        ``VoiceServer`` mounts these routes it injects ``_emit_connections_active``
        so an accepted/torn-down WebRTC offer updates the same
        ``easycat.server.connections.active`` gauge as a ``/ws`` session.
        """
        if self._on_connections_changed is not None:
            self._on_connections_changed()

    def _cors_headers(self, request: Any) -> dict[str, str]:
        """Build CORS headers for ``request`` via the shared signaling surface."""
        return self._signaling().cors_headers(request)

    def _unauthorized_response(self, request: Any) -> Any:
        return self._signaling().unauthorized_response(request)

    # ── Offer (genuinely per-peer) ───────────────────────────────────

    async def handle_offer(self, request: Any) -> Any:
        """Reserve capacity, negotiate a real per-offer peer, build a session.

        Rejects with 503 while draining (checked BEFORE capacity, matching the
        serve helper's ordering) or at capacity. Authorizes through the unified
        :class:`AuthPolicy` so a tokenless offer on a token-guarded server is
        rejected with 401. Each accepted offer gets an ISOLATED
        :class:`WebRTCTransport` (the genuine peer connection); the session is
        tracked on the gate + manager (and the shared active-session map, if
        provided) so the drain step can force-stop it.
        """
        from easycat.transports.webrtc import WebRTCTransport

        web = self._web
        auth_reason = self._auth_reason(request)
        if auth_reason != "allowed":
            # Auth is checked before draining here, so report the live gate state.
            self._record_rejection(server_state=self._server_state(), auth_result=auth_reason)
            return self._unauthorized_response(request)
        if self._gate.is_draining:
            self._record_rejection(server_state="draining")
            return self._draining_response(request)
        if not self._gate.try_acquire():
            # ``try_acquire`` only fails for capacity here (draining handled above).
            self._record_rejection(server_state="serving")
            return web.Response(
                status=503,
                text=json.dumps({"error": "Server is at the configured session limit"}),
                content_type="application/json",
                headers=self._cors_headers(request),
            )

        # The per-offer transport authorizes nothing itself (auth already ran at
        # this unified layer): drop the token + the static dir from its config.
        transport = WebRTCTransport(replace(self._config, static_dir=None, auth_token=None))
        transport._prepare_external_signaling(web)
        key = id(transport)
        session_started = False
        try:
            response = await transport._handle_offer(request)
            if getattr(response, "status", 200) >= 400:
                await transport.disconnect()
                self._gate.release()
                return response

            transport._offer_request = request
            # Negotiation may suspend for long enough that shutdown starts after
            # the admission check above. Do not start a session that was absent
            # from the drain snapshot; _register_session has a second fence
            # around the asynchronous Session.start() window.
            if self._gate.is_draining or not await self._register_session(key, transport):
                return await self._reject_draining_offer(request, transport)
            session_started = True
            return response
        except BaseException:
            # Request-handler cancellation is a normal aiohttp shutdown/client
            # disconnect path, but ``CancelledError`` does not inherit from
            # ``Exception``. Always unwind the peer and its reservation before
            # propagating cancellation (or another base exception), otherwise
            # one abandoned offer can permanently consume server capacity.
            if session_started:
                try:
                    await self._unregister_session(key)
                except BaseException:
                    # Cleanup is best-effort and must never replace the original
                    # offer failure/cancellation that aiohttp is waiting for.
                    logger.warning(
                        "Failed to unregister WebRTC session after offer failure",
                        exc_info=True,
                    )
            try:
                await transport.disconnect()
            except BaseException:
                logger.warning(
                    "Failed to disconnect WebRTC transport after offer failure",
                    exc_info=True,
                )
            self._gate.release()
            raise

    async def _register_session(self, key: int, transport: WebRTCTransport) -> bool:
        """Build + start + track the session for an accepted per-offer transport.

        Awaits ``manager.add`` (which starts the session) before returning so a
        successful ``/offer`` response only follows a started session — matching
        the old serve helper. Returns ``False`` when draining starts while
        ``manager.add`` is suspended; the caller then tears down the negotiated
        peer and returns a 503 response.
        """
        from easycat.config import create_session

        built = self._config_factory(transport)
        if hasattr(built, "start") and hasattr(built, "stop"):
            session = cast("Session", built)
        else:
            session = create_session(built)
        if self._runtime_feedback:
            from easycat.helpers import attach_runtime_feedback

            attach_runtime_feedback(session)
        await self._manager.add(key, session)
        # There is no await between this fence and ``gate.track`` below. If a
        # drain began during Session.start(), remove the just-started session
        # before it can miss the drain's active-session snapshot.
        if self._gate.is_draining:
            # This session was never published to the gate, so the bounded
            # drain cannot see or escalate it. Force removal here rather than
            # letting an unbounded graceful stop hold the offer handler (and
            # aiohttp runner cleanup) past the server shutdown deadline.
            await self._manager.remove(key, force=True)
            return False
        # ``manager.add`` already STARTED the session. ``handle_offer``'s except
        # only runs its unregister when ``session_started`` is True, and that flag
        # is set by the caller AFTER this method returns — so any failure wiring up
        # the remaining bookkeeping below (forwarder / cleanup task) would otherwise
        # leave the started session registered forever (never stopped) with its gate
        # slot orphaned. Unwind it here so a partial registration cannot leak.
        try:
            self._gate.track(key)
            if self._active_session_objs is not None:
                self._active_session_objs[key] = session
            # The accepted offer now holds a gate reservation — refresh the shared
            # active-connection gauge so WebRTC traffic is visible to OTel.
            self._emit_connections_changed()
            transport._ensure_browser_event_forwarder()
            self._released_cleanup_keys.discard(key)
            self._start_cleanup_task(key, transport)
            return True
        except Exception:
            # Stop + drop the started session (``manager.remove`` stops it) and
            # clear any partial gate/active-map bookkeeping. The gate reservation
            # itself is released by ``handle_offer``'s except (no cleanup task ran).
            await self._unregister_session(key)
            raise

    def _draining_response(self, request: Any) -> Any:
        """Build the standard 503 response for an offer crossing shutdown."""
        return self._web.Response(
            status=503,
            text=json.dumps({"error": "Server is shutting down"}),
            content_type="application/json",
            headers=self._cors_headers(request),
        )

    async def _reject_draining_offer(self, request: Any, transport: WebRTCTransport) -> Any:
        """Release a negotiated peer that crossed the server drain boundary."""
        self._record_rejection(server_state="draining")
        await transport.disconnect()
        self._gate.release()
        return self._draining_response(request)

    async def _unregister_session(self, key: int) -> None:
        """Untrack a session that failed after being registered."""
        await self._manager.remove(key)
        self._gate.untrack(key)
        if self._active_session_objs is not None:
            self._active_session_objs.pop(key, None)
        self._emit_connections_changed()

    async def _cleanup_session(self, key: int, transport: WebRTCTransport) -> None:
        """Untrack + release once the peer connection closes."""
        try:
            await transport.wait_closed()
        finally:
            await self._finalize_session_cleanup(key, force=False)

    def _start_cleanup_task(
        self,
        key: int,
        transport: WebRTCTransport,
    ) -> asyncio.Task[Any]:
        """Start and index one scope-owned per-offer cleanup worker."""
        task = self._cleanup_task_scope.create_task(
            self._cleanup_session(key, transport),
            task_name="easycat-webrtc-offer-cleanup",
        )
        assert task is not None
        self._cleanup_task_keys[task] = key
        task.add_done_callback(self._cleanup_task_done)
        return task

    async def _finalize_session_cleanup(self, key: int, *, force: bool) -> None:
        """Stop and release one offer exactly once, including pre-start cancellation."""
        await self._manager.remove(key, force=force)
        self._gate.untrack(key)
        if self._active_session_objs is not None:
            self._active_session_objs.pop(key, None)
        if key not in self._released_cleanup_keys:
            self._released_cleanup_keys.add(key)
            self._gate.release()
            self._emit_connections_changed()

    def _cleanup_task_done(self, task: asyncio.Task[Any]) -> None:
        self._cleanup_task_keys.pop(task, None)

    async def cancel_cleanup_tasks(self, *, timeout_s: float | None = None) -> None:
        """Cancel + await the per-offer cleanup tasks (called on server stop)."""
        pending = [
            (task, self._cleanup_task_keys.get(task)) for task in self._cleanup_task_scope.tasks()
        ]
        for task, _key in pending:
            task.cancel()
        if pending:
            finalizers: list[asyncio.Task[Any]] = []
            for _task, key in pending:
                if key is None:
                    continue
                finalizer = self._force_cleanup_task_scope.create_task(
                    self._finalize_session_cleanup(key, force=True),
                    task_name=f"easycat-webrtc-force-cleanup-{key}",
                )
                assert finalizer is not None
                finalizers.append(finalizer)
            cleanup_tasks = [*(task for task, _key in pending), *finalizers]
            cleanup = asyncio.gather(
                *cleanup_tasks,
                return_exceptions=True,
            )
            results: list[Any | BaseException] | None
            if timeout_s is None:
                results = await cleanup
            else:
                completed = await wait_for_owned_future(cleanup, timeout_s=timeout_s)
                results = cleanup.result() if completed else None
            if results is not None:
                self._report_cleanup_results(
                    cleanup_tasks,
                    results,
                    explicitly_cancelled={task for task, _key in pending},
                )
            else:
                for finalizer in finalizers:
                    finalizer.add_done_callback(self._report_late_cleanup_result)
        await self._cleanup_task_scope.release_standalone_if_empty()
        await self._force_cleanup_task_scope.release_standalone_if_empty()

    def _report_cleanup_results(
        self,
        tasks: list[asyncio.Task[Any]],
        results: list[Any],
        *,
        explicitly_cancelled: set[asyncio.Task[Any]],
    ) -> None:
        """Log unexpected cleanup failures with the owning task's identity."""
        for task, result in zip(tasks, results, strict=True):
            if not isinstance(result, BaseException):
                continue
            if isinstance(result, asyncio.CancelledError) and task in explicitly_cancelled:
                continue
            logger.error(
                "WebRTC cleanup task %s failed",
                task.get_name(),
                exc_info=result,
            )

    def _report_late_cleanup_result(self, task: asyncio.Task[Any]) -> None:
        """Report a forced finalizer that settles after the hard deadline."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "WebRTC cleanup task %s failed",
                task.get_name(),
                exc_info=error,
            )

    async def _stop_managed_session(self, key: int, force: bool) -> None:
        """Route drain teardown through the manager's keyed stop ownership."""
        await self._manager.remove(key, force=force)

    # ── Config / stats / health / root / cors (shared, stateless) ─────
    # These handlers and their CORS / auth / stats helpers are byte-identical to
    # the singleton transport's, so they live ONCE in
    # ``easycat.server._webrtc_handlers.WebRTCSignalingHandlers``. These thin
    # delegators keep the routes' public handler names (registered in
    # :meth:`register`) and the private helper names the parity test pins.

    async def handle_config(self, request: Any) -> Any:
        """``GET /config`` — ICE servers (TURN creds omitted unless opted in)."""
        return await self._signaling().handle_config(request)

    async def handle_stats(self, request: Any) -> Any:
        """``POST /stats`` — sanitize + (optionally) persist a stats snapshot."""
        return await self._signaling().handle_stats(request)

    def _stats_write_permitted(self, request: Any) -> bool:
        return self._signaling().stats_write_permitted(request)

    def _stats_forbidden_response(self, request: Any) -> Any:
        return self._signaling().stats_forbidden_response(request)

    def _stats_quota_response(self, request: Any, message: str) -> Any:
        return self._signaling().stats_quota_response(request, message)

    def _stats_quota_error(self, stats_path: Path, snapshot: dict[str, object]) -> str | None:
        return self._signaling().stats_quota_error(stats_path, snapshot)

    async def handle_health(self, request: Any) -> Any:
        """``GET /health`` — capacity JSON spanning the shared gate."""
        return await self._signaling().handle_health(request)

    async def handle_root(self, request: Any) -> Any:
        """``GET /`` — redirect to the bundled client, else a JSON endpoint hint.

        When a bundled client is served, redirect to it, appending the
        ``?webrtc=<prefix>`` base (the ``_client_base``) so the served client
        targets the mounted (``/webrtc/*``) or flat (``""``) routes, while
        preserving any existing ``?token=``.
        """
        return await self._signaling().handle_root(request)

    async def handle_cors_preflight(self, request: Any) -> Any:
        return await self._signaling().handle_cors_preflight(request)


async def _shutdown_standalone_webrtc(  # noqa: C901 - independent cleanup stages
    *,
    site: Any,
    runner: Any,
    gate: CapacityGate[int],
    active_sessions: dict[int, Any],
    routes: WebRTCRoutes,
    manager: SessionManager[int],
    drain_timeout_s: float,
    force_shutdown_timeout_s: float,
) -> None:
    """Drain sessions even when the standalone HTTP listener fails to stop."""
    gate.start_draining()
    listener_error: Exception | None = None
    body_error: BaseException | None = None

    def record_error(exc: BaseException) -> None:
        nonlocal listener_error, body_error
        if not isinstance(exc, Exception):
            if body_error is None:
                body_error = exc
            return
        if listener_error is None:
            listener_error = exc
        else:
            logger.warning("Standalone WebRTC listener cleanup also failed")

    try:
        await site.stop()
    except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
        record_error(exc)
    try:
        await runner.cleanup()
    except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
        record_error(exc)
    try:
        await gate.drain(
            lambda: tuple(active_sessions.items()),
            drain_timeout_s=max(drain_timeout_s, 0.0),
            force_after=True,
            force_timeout_s=max(force_shutdown_timeout_s, 0.0),
            stop_for_key=routes._stop_managed_session,
        )
    except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
        if body_error is None:
            body_error = exc
    try:
        await routes.cancel_cleanup_tasks(timeout_s=max(force_shutdown_timeout_s, 0.0))
    except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
        if body_error is None:
            body_error = exc
    sweep_scope = RuntimeTaskScope(
        owner_label="standalone-webrtc-shutdown",
        member_name=_STANDALONE_SWEEP_TASK,
        cohort=_STANDALONE_SWEEP_COHORT,
        logger=logger,
        failure_message="Standalone WebRTC session sweep failed",
        drop_if_closed=False,
    )
    try:
        try:
            sweep_task = sweep_scope.create_task(
                manager.stop_all(force=True),
                task_name="easycat-standalone-webrtc-session-sweep",
            )
            assert sweep_task is not None
            swept = await wait_for_owned_future(
                sweep_task,
                timeout_s=max(force_shutdown_timeout_s, 0.0),
            )
            report_error = _standalone_sweep_error(
                swept=swept,
                sweep_task=sweep_task,
                timeout_s=force_shutdown_timeout_s,
            )
            body_error = body_error or report_error
        finally:
            await sweep_scope.release_standalone_if_empty()
    except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
        if body_error is None:
            body_error = exc

    if body_error is not None:
        raise body_error
    if listener_error is not None:
        raise listener_error


async def serve_webrtc_config_sessions(
    config_factory: WebRTCConfigFactory,
    config: WebRTCTransportConfig | None = None,
    *,
    stop_event: asyncio.Event | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S,
    force_shutdown_timeout_s: float = STANDALONE_WEBRTC_FORCE_SHUTDOWN_TIMEOUT_S,
) -> None:
    """Serve one EasyCat session per browser WebRTC offer."""
    from easycat._extras import require_module
    from easycat._net import normalize_auth_token
    from easycat._signals import create_shutdown_event
    from easycat.server.auth import BearerTokenAuth, enforce_bind_guard
    from easycat.server.transports import CapacityGate
    from easycat.session_manager import SessionManager

    settings = config or WebRTCTransportConfig()
    auth_token = normalize_auth_token(settings.auth_token)
    bind_auth = (
        BearerTokenAuth(token=auth_token, allow_query_token=settings.allow_query_token)
        if auth_token is not None
        else None
    )
    enforce_bind_guard(
        settings.host,
        auth=bind_auth,
        unsafe_allow_no_auth=unsafe_allow_no_auth,
    )
    web = require_module("aiohttp.web", extra="webrtc", purpose="WebRTC signaling")
    manager: SessionManager[int] = SessionManager()
    gate: CapacityGate[int] = CapacityGate(settings.max_sessions)
    active_sessions: dict[int, Any] = {}
    routes = WebRTCRoutes(
        settings,
        auth=bind_auth,
        config_factory=config_factory,
        gate=gate,
        manager=manager,
        runtime_feedback=runtime_feedback,
        active_session_objs=active_sessions,
    )

    app = web.Application()
    routes.register(app, prefix="", web=web)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    if announce:
        print(f"\nServer ready. Open http://{settings.host}:{settings.port} in your browser")
        print("Press Ctrl+C to stop.\n")

    event = stop_event or create_shutdown_event()
    try:
        await event.wait()
    finally:
        await _shutdown_standalone_webrtc(
            site=site,
            runner=runner,
            gate=gate,
            active_sessions=active_sessions,
            routes=routes,
            manager=manager,
            drain_timeout_s=drain_timeout_s,
            force_shutdown_timeout_s=force_shutdown_timeout_s,
        )


def run_webrtc_config_server(
    config_factory: WebRTCConfigFactory,
    config: WebRTCTransportConfig | None = None,
    *,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S,
    force_shutdown_timeout_s: float = STANDALONE_WEBRTC_FORCE_SHUTDOWN_TIMEOUT_S,
) -> None:
    """Run a multi-session WebRTC signaling server synchronously."""
    asyncio.run(
        serve_webrtc_config_sessions(
            config_factory,
            config,
            runtime_feedback=runtime_feedback,
            announce=announce,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
            drain_timeout_s=drain_timeout_s,
            force_shutdown_timeout_s=force_shutdown_timeout_s,
        )
    )
