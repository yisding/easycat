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

* Stateless / shim-only (LIFTED here, need no transport instance):

  - ``_cors_headers`` — reads only ``config.cors_allowed_origins``.
  - ``_origin_matches_request`` — already a ``@staticmethod`` (imported).
  - request authorization — now routed through the UNIFIED
    :class:`~easycat.server.auth.AuthPolicy`
    (``from_aiohttp_request`` + ``AuthPolicy.authorize``), NOT
    ``WebRTCTransport._request_authorized``, so WebSocket and WebRTC share one
    auth layer and the ``allow_query_token`` default-off posture applies here.
  - ``_handle_config`` / ``_ice_servers_as_dicts`` — read ``config.ice_servers``
    + ``expose_ice_credentials``.
  - ``_handle_stats`` + the stats permission/quota/record helpers — read the
    ``stats_*`` config and a PER-SERVER rate-limit deque. The shim previously
    owned that deque for the whole process; :class:`WebRTCRoutes` owns an
    equivalent ``_stats_request_times`` deque so the rate-limit/quota semantics
    are per-server (identical to the shim).
  - ``_handle_root`` — reads ``_has_bundled_client`` (a routes-level flag) and
    preserves the ``?token=`` query-string redirect.
  - ``_handle_cors_preflight`` / ``_handle_health``.

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
:class:`WebRTCTransport` is imported lazily inside the handlers too, so
``import easycat.server`` stays light and pulls no planner/aiohttp at module
load.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

from easycat.transports.webrtc import (
    _CORS_ALLOW_HEADERS,
    _CORS_ALLOW_METHODS,
    WebRTCTransport,
    WebRTCTransportConfig,
    _is_loopback_host,
    _sanitize_webrtc_base,
    _sanitize_webrtc_stats_snapshot,
)

if TYPE_CHECKING:
    from easycat.config import EasyConfig
    from easycat.server.auth import AuthPolicy
    from easycat.server.transports import CapacityGate
    from easycat.session import Session
    from easycat.session_manager import SessionManager

logger = logging.getLogger(__name__)

# Per-connection factory seam (NO ``ConnectionContext`` type): a per-transport
# ``Callable[[WebRTCTransport], EasyConfig | Session]``.
WebRTCConfigFactory = Callable[[WebRTCTransport], "EasyConfig | Session"]


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
        # Per-server stats rate-limit window (lifted off the shim's per-instance
        # ``_stats_request_times`` deque so the rate limit is per-server, not
        # per-request — matching the previous behavior exactly).
        self._stats_request_times: deque[float] = deque()
        # Set by ``register`` when a bundled client is served so ``handle_root``
        # redirects to it (mirrors ``WebRTCTransport._has_bundled_client``).
        self._has_bundled_client = False
        # The route prefix the bundled client must target (``""`` for the flat
        # helper, ``/webrtc`` for the mounted server). Threaded into the root
        # redirect so the served client points at the right routes.
        self._client_base = ""
        # Tracks per-offer transport cleanup tasks so ``stop`` can cancel them.
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
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

    # ── Auth (unified) ───────────────────────────────────────────────

    def _authorized(self, request: Any) -> bool:
        """Authorize ``request`` through the UNIFIED :class:`AuthPolicy` (bool shim)."""
        return self._auth_reason(request) == "allowed"

    def _auth_reason(self, request: Any) -> str:
        """Return the ``AuthReason`` for ``request`` through the UNIFIED policy.

        Routes through ``server.auth.from_aiohttp_request`` +
        ``AuthPolicy.authorize`` (NOT ``WebRTCTransport._request_authorized``) so
        the WebSocket and WebRTC paths share one auth layer and the
        ``allow_query_token`` default-off posture applies to mounted WebRTC. No
        policy configured means open access (``"allowed"`` — the loopback/dev
        default). Returns the ``AuthReason`` Literal (``allowed`` / ``missing`` /
        ``invalid``) so a rejection metric can carry the right
        ``easycat.auth_result`` label.
        """
        if self._auth is None:
            return "allowed"
        from easycat.server.auth import from_aiohttp_request

        return self._auth.authorize(from_aiohttp_request(request)).reason

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
        """Build CORS headers for ``request`` (byte-identical to the transport).

        Reads only ``config.cors_allowed_origins`` — no transport instance
        needed.
        """
        origin = getattr(request, "headers", {}).get("Origin")
        if not origin:
            return {}
        configured_origins = self._config.cors_allowed_origins
        if isinstance(configured_origins, str):
            configured_origins = (configured_origins,)
        allowed = {item.rstrip("/") for item in configured_origins}
        if "*" in allowed:
            allowed_origin = "*"
        elif origin.rstrip("/") in allowed or WebRTCTransport._origin_matches_request(
            origin, request
        ):
            allowed_origin = origin
        else:
            return {}
        return {
            "Access-Control-Allow-Origin": allowed_origin,
            "Access-Control-Allow-Methods": _CORS_ALLOW_METHODS,
            "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        }

    def _unauthorized_response(self, request: Any) -> Any:
        return self._web.Response(
            status=401,
            text=json.dumps({"error": "Missing or invalid bearer token"}),
            content_type="application/json",
            headers=self._cors_headers(request),
        )

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
        web = self._web
        auth_reason = self._auth_reason(request)
        if auth_reason != "allowed":
            # Auth is checked before draining here, so report the live gate state.
            self._record_rejection(server_state=self._server_state(), auth_result=auth_reason)
            return self._unauthorized_response(request)
        if self._gate.is_draining:
            self._record_rejection(server_state="draining")
            return web.Response(
                status=503,
                text=json.dumps({"error": "Server is shutting down"}),
                content_type="application/json",
                headers=self._cors_headers(request),
            )
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

            await self._register_session(key, transport)
            session_started = True
            return response
        except Exception:
            if session_started:
                await self._unregister_session(key)
            await transport.disconnect()
            self._gate.release()
            raise

    async def _register_session(self, key: int, transport: WebRTCTransport) -> None:
        """Build + start + track the session for an accepted per-offer transport.

        Awaits ``manager.add`` (which starts the session) before returning so a
        successful ``/offer`` response only follows a started session — matching
        the old serve helper.
        """
        from easycat.config import create_session

        built = self._config_factory(transport)
        if hasattr(built, "start") and hasattr(built, "stop"):
            session = built
        else:
            session = create_session(built)
        if self._runtime_feedback:
            from easycat.helpers import attach_runtime_feedback

            attach_runtime_feedback(session)
        await self._manager.add(key, session)
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
            task = asyncio.create_task(self._cleanup_session(key, transport))
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)
        except Exception:
            # Stop + drop the started session (``manager.remove`` stops it) and
            # clear any partial gate/active-map bookkeeping. The gate reservation
            # itself is released by ``handle_offer``'s except (no cleanup task ran).
            await self._unregister_session(key)
            raise

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
            try:
                await asyncio.shield(self._manager.remove(key))
                self._gate.untrack(key)
                if self._active_session_objs is not None:
                    self._active_session_objs.pop(key, None)
            finally:
                self._gate.release()
                # The reservation is gone — refresh the shared gauge so the
                # active-connection count decrements for WebRTC teardown too.
                self._emit_connections_changed()

    async def cancel_cleanup_tasks(self) -> None:
        """Cancel + await the per-offer cleanup tasks (called on server stop)."""
        for task in list(self._cleanup_tasks):
            task.cancel()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)

    # ── Config / stats / health / root / cors (stateless) ─────────────

    def _ice_servers_as_dicts(self, *, include_credentials: bool) -> list[dict[str, Any]]:
        """Serialize the configured ICE servers to plain dicts (no instance)."""
        result: list[dict[str, Any]] = []
        for srv in self._config.ice_servers:
            entry: dict[str, Any] = {"urls": srv.urls}
            if include_credentials:
                if srv.username:
                    entry["username"] = srv.username
                if srv.credential:
                    entry["credential"] = srv.credential
            result.append(entry)
        return result

    async def handle_config(self, request: Any) -> Any:
        """``GET /config`` — ICE servers (TURN creds omitted unless opted in)."""
        web = self._web
        if not self._authorized(request):
            return self._unauthorized_response(request)
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {
                    "iceServers": self._ice_servers_as_dicts(
                        include_credentials=self._config.expose_ice_credentials
                    )
                }
            ),
            headers=self._cors_headers(request),
        )

    async def handle_stats(self, request: Any) -> Any:
        """``POST /stats`` — sanitize + (optionally) persist a stats snapshot."""
        web = self._web
        if not self._authorized(request):
            return self._unauthorized_response(request)
        try:
            payload = await request.json()
            snapshot = _sanitize_webrtc_stats_snapshot(payload)
        except Exception as exc:
            return web.Response(
                status=400,
                text=json.dumps({"error": f"Invalid WebRTC stats payload: {exc}"}),
                content_type="application/json",
                headers=self._cors_headers(request),
            )

        if self._config.stats_path:
            if not self._stats_write_permitted(request):
                return self._stats_forbidden_response(request)
            stats_path = Path(self._config.stats_path)
            quota_error = self._stats_quota_error(stats_path, snapshot)
            if quota_error is not None:
                return self._stats_quota_response(request, quota_error)
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            with stats_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot, sort_keys=True) + "\n")
            self._stats_request_times.append(time.monotonic())

        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "ok"}),
            headers=self._cors_headers(request),
        )

    def _stats_write_permitted(self, request: Any) -> bool:
        """Return whether an unauthenticated stats write is validation-local.

        A configured ``AuthPolicy`` always authorizes through :meth:`_authorized`.
        Without a policy, stats artifacts are only writable for loopback-bound
        validation/demo servers and same-origin browser requests — keeping a
        non-loopback signaling server from exposing an unauthenticated append
        sink (identical to ``WebRTCTransport._stats_write_permitted``).
        """
        if self._auth is not None:
            return self._authorized(request)
        if not _is_loopback_host(self._config.host):
            return False
        origin = getattr(request, "headers", {}).get("Origin")
        return bool(origin and WebRTCTransport._origin_matches_request(origin, request))

    def _stats_forbidden_response(self, request: Any) -> Any:
        return self._web.Response(
            status=403,
            text=json.dumps(
                {
                    "error": (
                        "WebRTC stats collection requires a bearer token for non-loopback "
                        "servers or a same-origin loopback validation request"
                    )
                }
            ),
            content_type="application/json",
            headers=self._cors_headers(request),
        )

    def _stats_quota_response(self, request: Any, message: str) -> Any:
        return self._web.Response(
            status=429,
            text=json.dumps({"error": message}),
            content_type="application/json",
            headers=self._cors_headers(request),
        )

    def _stats_quota_error(self, stats_path: Path, snapshot: dict[str, object]) -> str | None:
        """Per-server rate-limit / size / record quota check (lifted off the shim)."""
        now = time.monotonic()
        window_start = now - 60.0
        while self._stats_request_times and self._stats_request_times[0] < window_start:
            self._stats_request_times.popleft()

        max_requests = self._config.stats_max_requests_per_minute
        if max_requests >= 0 and len(self._stats_request_times) >= max_requests:
            return "WebRTC stats rate limit exceeded"

        encoded = json.dumps(snapshot, sort_keys=True) + "\n"
        current_size = stats_path.stat().st_size if stats_path.exists() else 0
        max_bytes = self._config.stats_max_file_bytes
        if max_bytes >= 0 and current_size + len(encoded.encode("utf-8")) > max_bytes:
            return "WebRTC stats artifact size limit exceeded"

        max_records = self._config.stats_max_records
        if max_records >= 0:
            current_records = 0
            if stats_path.exists():
                with stats_path.open("r", encoding="utf-8") as handle:
                    current_records = sum(1 for _ in handle)
            if current_records >= max_records:
                return "WebRTC stats artifact record limit exceeded"

        return None

    async def handle_health(self, request: Any) -> Any:
        """``GET /health`` — capacity JSON spanning the shared gate."""
        web = self._web
        status = "draining" if self._gate.is_draining else "ok"
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {
                    "status": status,
                    "active_sessions": self._gate.active_count,
                    "max_sessions": self._config.max_sessions,
                }
            ),
            headers=self._cors_headers(request),
        )

    async def handle_root(self, request: Any) -> Any:
        """``GET /`` — redirect to the bundled client, else a JSON endpoint hint.

        When a bundled client is served, redirect to it, appending the
        ``?webrtc=<prefix>`` base so the served client targets the mounted
        (``/webrtc/*``) or flat (``""``) routes, while preserving any existing
        ``?token=`` (the WebRTC client uses the ``Authorization`` header, so
        ``?token=`` is just forwarded, never consumed for query auth). With no
        bundled client, return the JSON endpoint hint (byte-identical to
        ``WebRTCTransport._handle_root``) so the flat helper's root keeps working.
        """
        web = self._web
        if self._has_bundled_client:
            location = "/webrtc_client.html"
            query_string = getattr(request, "query_string", "")
            params: list[tuple[str, str]] = []
            user_base = ""
            for key, value in parse_qsl(query_string, keep_blank_values=True):
                if key == "webrtc":
                    if not user_base:
                        user_base = value
                    continue
                params.append((key, value))
            # A trusted mount base (e.g. ``/webrtc``) always replaces whatever
            # the client supplied. In flat mode (``_client_base == ""``) there
            # is no trusted base to substitute, so preserve a sanitized
            # same-origin ``?webrtc=`` prefix (reverse-proxy path prefixes) and
            # drop any untrusted/cross-origin value rather than echoing it.
            base = self._client_base or _sanitize_webrtc_base(user_base)
            if base:
                params.append(("webrtc", base))
            if params:
                location = f"{location}?{urlencode(params, doseq=True, safe='/')}"
            raise web.HTTPFound(location)
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {
                    "service": "easycat-webrtc-signaling",
                    "endpoints": ["/offer", "/stats", "/config", "/health"],
                    "note": (
                        "Set WebRTCTransportConfig.static_dir to serve "
                        "the demo browser client from this server."
                    ),
                }
            ),
            headers=self._cors_headers(request),
        )

    async def handle_cors_preflight(self, request: Any) -> Any:
        return self._web.Response(headers=self._cors_headers(request))
