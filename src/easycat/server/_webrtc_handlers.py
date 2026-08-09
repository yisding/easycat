"""Shared stateless WebRTC signaling surface (QS6 M7 convergence).

Both the singleton :class:`~easycat.transports.webrtc.WebRTCTransport` and the
multi-session :class:`~easycat.server.webrtc_routes.WebRTCRoutes` expose the SAME
config / stats / health / root / cors signaling surface. Before QS6 that surface
was copy-pasted into both classes and kept "byte-identical" by docstring promise
only, with no test enforcing it (and the transport's auth had a latent non-ASCII
DoS). It now lives here ONCE, in :class:`WebRTCSignalingHandlers`, parameterized
by ``(config, AuthPolicy | None, stats, ...)`` — there is a single implementation
to audit, and both owners delegate to it.

Only genuinely per-peer negotiation (``_handle_offer``) stays on each owner: it
is semantically different between the singleton transport (one peer connection)
and the multi-session routes (one isolated peer + session per accepted offer).

Auth is unified on :class:`~easycat.server.auth.AuthPolicy`: pass a
:class:`~easycat.server.auth.BearerTokenAuth` when a token is configured, or
``None`` for open access (the loopback / dev default). ``allow_query_token``
lives on the policy. This is also the CANONICAL reconciliation of the previously
diverged ``_stats_write_permitted`` (transport read ``config.auth_token``; routes
read the policy): both now derive it from the injected policy.

Import weight: this module imports only stdlib, the leaf ``is_loopback_host``,
and the focused WebRTC config/stats modules. aiohttp is NEVER imported here —
the ``web`` module is passed in by the caller (the transport resolves it in
``connect``; the routes resolve it in ``register``), so ``import easycat.server``
stays light and the ``webrtc`` extra stays optional. The transport imports this
module LAZILY so importing the transport pulls no server package at load.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

from easycat._net import is_loopback_host
from easycat.transports._webrtc_config import (
    _CORS_ALLOW_HEADERS,
    _CORS_ALLOW_METHODS,
    WebRTCTransportConfig,
    sanitize_webrtc_base,
)
from easycat.transports._webrtc_stats import (
    WebRTCStatsState,
    append_webrtc_stats_record,
    sanitize_webrtc_stats_snapshot,
)

if TYPE_CHECKING:
    from easycat.server.auth import AuthPolicy


def _origin_matches_request(origin: str, request: Any) -> bool:
    """Return whether ``origin`` equals the request's own ``scheme://host``."""
    scheme = getattr(request, "scheme", None)
    host = getattr(request, "host", None)
    if not scheme or not host:
        return False
    return origin.rstrip("/") == f"{scheme}://{host}"


class WebRTCSignalingHandlers:
    """The stateless config / stats / health / root / cors signaling surface, once.

    Parameterized by the shared :class:`WebRTCTransportConfig`, an optional
    unified :class:`AuthPolicy` (``None`` == open access), a per-server
    :class:`WebRTCStatsState`, and the small per-surface knobs that legitimately
    differ (``has_bundled_client`` / ``client_base`` for the root redirect, and a
    ``health_payload`` builder). Holds NO peer-connection state.
    """

    def __init__(
        self,
        config: WebRTCTransportConfig,
        *,
        web: Any,
        auth: AuthPolicy | None,
        stats: WebRTCStatsState,
        has_bundled_client: bool = False,
        client_base: str = "",
        health_payload: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._config = config
        self._web = web
        self._auth = auth
        self._stats = stats
        self.has_bundled_client = has_bundled_client
        self._client_base = client_base
        self._health_payload = health_payload

    # ── CORS ──────────────────────────────────────────────────────────

    def cors_headers(self, request: Any) -> dict[str, str]:
        """Build CORS headers for ``request`` from ``config.cors_allowed_origins``."""
        origin = getattr(request, "headers", {}).get("Origin")
        if not origin:
            return {}
        configured_origins = self._config.cors_allowed_origins
        if isinstance(configured_origins, str):
            configured_origins = (configured_origins,)
        allowed = {item.rstrip("/") for item in configured_origins}
        if "*" in allowed:
            allowed_origin = "*"
        elif origin.rstrip("/") in allowed or _origin_matches_request(origin, request):
            allowed_origin = origin
        else:
            return {}
        return {
            "Access-Control-Allow-Origin": allowed_origin,
            "Access-Control-Allow-Methods": _CORS_ALLOW_METHODS,
            "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        }

    def unauthorized_response(self, request: Any) -> Any:
        return self._web.Response(
            status=401,
            text=json.dumps({"error": "Missing or invalid bearer token"}),
            content_type="application/json",
            headers=self.cors_headers(request),
        )

    # ── Auth (unified AuthPolicy) ─────────────────────────────────────

    def authorized(self, request: Any) -> bool:
        """Authorize ``request`` through the unified :class:`AuthPolicy` (bool shim)."""
        return self.auth_reason(request) == "allowed"

    def auth_reason(self, request: Any) -> str:
        """Return the ``AuthReason`` for ``request`` (``None`` policy == open access).

        Returns the ``AuthReason`` Literal (``allowed`` / ``missing`` / ``invalid``)
        so a rejection metric can carry the right ``easycat.auth_result`` label.
        Routing through :class:`BearerTokenAuth` guards the attacker-controlled
        credential against a non-ASCII value (``hmac.compare_digest`` raises
        ``TypeError`` on non-ASCII), so a non-ASCII credential is a clean deny,
        never an HTTP 500.
        """
        if self._auth is None:
            return "allowed"
        from easycat.server.auth import from_aiohttp_request

        return self._auth.authorize(from_aiohttp_request(request)).reason

    # ── ICE ───────────────────────────────────────────────────────────

    def ice_servers_as_dicts(
        self,
        *,
        include_credentials: bool,
        browser_safe: bool = False,
    ) -> list[dict[str, Any]]:
        """Serialize configured ICE servers to plain dictionaries.

        With ``browser_safe=True``, omit TURN URLs unless their complete
        credentials are explicitly included: credential-less TURN makes browser
        ``RTCPeerConnection`` construction fail. The default full serialization
        keeps incomplete TURN entries available to the server peer.
        """
        result: list[dict[str, Any]] = []
        for srv in self._config.ice_servers:
            has_complete_credentials = bool(srv.username and srv.credential)
            expose_turn = include_credentials and has_complete_credentials
            urls = [
                url
                for url in srv.urls
                if not browser_safe
                or expose_turn
                or url.partition(":")[0].lower() not in {"turn", "turns"}
            ]
            if not urls:
                continue
            entry: dict[str, Any] = {"urls": urls}
            if include_credentials and (not browser_safe or expose_turn):
                if srv.username:
                    entry["username"] = srv.username
                if srv.credential:
                    entry["credential"] = srv.credential
            result.append(entry)
        return result

    # ── Stats ─────────────────────────────────────────────────────────

    def stats_write_permitted(self, request: Any) -> bool:
        """Return whether an unauthenticated stats write is validation-local.

        The CANONICAL reconciliation of the previously diverged copies: a
        configured :class:`AuthPolicy` always authorizes through
        :meth:`authorized`; without a policy, stats artifacts are only writable
        for loopback-bound validation / demo servers and same-origin browser
        requests, keeping a non-loopback signaling server from exposing an
        unauthenticated append sink.
        """
        if self._auth is not None:
            return self.authorized(request)
        if not is_loopback_host(self._config.host):
            return False
        origin = getattr(request, "headers", {}).get("Origin")
        return bool(origin and _origin_matches_request(origin, request))

    def stats_forbidden_response(self, request: Any) -> Any:
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
            headers=self.cors_headers(request),
        )

    def stats_quota_response(self, request: Any, message: str) -> Any:
        return self._web.Response(
            status=429,
            text=json.dumps({"error": message}),
            content_type="application/json",
            headers=self.cors_headers(request),
        )

    def stats_quota_error(self, stats_path: Path, snapshot: dict[str, object]) -> str | None:
        """Per-server rate-limit / size / record quota check."""
        now = time.monotonic()
        window_start = now - 60.0
        request_times = self._stats.request_times
        while request_times and request_times[0] < window_start:
            request_times.popleft()

        max_requests = self._config.stats_max_requests_per_minute
        if max_requests >= 0 and len(request_times) >= max_requests:
            return "WebRTC stats rate limit exceeded"

        encoded = json.dumps(snapshot, sort_keys=True) + "\n"
        current_size = stats_path.stat().st_size if stats_path.exists() else 0
        max_bytes = self._config.stats_max_file_bytes
        if max_bytes >= 0 and current_size + len(encoded.encode("utf-8")) > max_bytes:
            return "WebRTC stats artifact size limit exceeded"

        max_records = self._config.stats_max_records
        if max_records >= 0:
            # The count is cached per server and advanced by this process's own
            # writes (avoiding a full-file recount per request). If the artifact
            # was rotated/removed externally, drop the stale cache so an empty
            # file doesn't keep returning 429 until the process restarts.
            if not stats_path.exists():
                self._stats.record_count = 0
            elif self._stats.record_count is None:
                with stats_path.open("r", encoding="utf-8") as handle:
                    self._stats.record_count = sum(1 for _ in handle)
            if self._stats.record_count >= max_records:
                return "WebRTC stats artifact record limit exceeded"

        return None

    def record_stats_write(self) -> None:
        self._stats.request_times.append(time.monotonic())
        if self._stats.record_count is not None:
            self._stats.record_count += 1

    # ── Handlers ──────────────────────────────────────────────────────

    async def handle_config(self, request: Any) -> Any:
        """``GET /config`` — browser-safe ICE servers; TURN is explicit opt-in."""
        web = self._web
        if not self.authorized(request):
            return self.unauthorized_response(request)
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {
                    "iceServers": self.ice_servers_as_dicts(
                        include_credentials=self._config.expose_ice_credentials,
                        browser_safe=True,
                    )
                }
            ),
            headers=self.cors_headers(request),
        )

    async def handle_stats(self, request: Any) -> Any:
        """``POST /stats`` — sanitize + (optionally) persist a stats snapshot."""
        web = self._web
        if not self.authorized(request):
            return self.unauthorized_response(request)
        try:
            payload = await request.json()
            snapshot = sanitize_webrtc_stats_snapshot(payload)
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return web.Response(
                status=400,
                text=json.dumps({"error": f"Invalid WebRTC stats payload: {exc}"}),
                content_type="application/json",
                headers=self.cors_headers(request),
            )

        if self._config.stats_path:
            if not self.stats_write_permitted(request):
                return self.stats_forbidden_response(request)
            stats_path = Path(self._config.stats_path)
            # Hold the per-server lock across check + append + counter update:
            # ``to_thread`` yields the loop, and without the lock concurrent
            # posts all observe the same pre-write counters / file size and
            # append together, exceeding the configured quotas.
            async with self._stats.write_lock:
                quota_error = self.stats_quota_error(stats_path, snapshot)
                if quota_error is not None:
                    return self.stats_quota_response(request, quota_error)
                await asyncio.to_thread(append_webrtc_stats_record, stats_path, snapshot)
                self.record_stats_write()

        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "ok"}),
            headers=self.cors_headers(request),
        )

    async def handle_health(self, request: Any) -> Any:
        """``GET /health`` — the caller-supplied payload (defaults to ``status: ok``)."""
        payload = self._health_payload() if self._health_payload is not None else {"status": "ok"}
        return self._web.Response(
            content_type="application/json",
            text=json.dumps(payload),
            headers=self.cors_headers(request),
        )

    async def handle_root(self, request: Any) -> Any:
        """``GET /`` — redirect to the bundled client, else a JSON endpoint hint.

        A trusted mount base (``client_base``, e.g. ``/webrtc``) always replaces
        whatever the client supplied. In flat mode (``client_base == ""``) there
        is no trusted base, so a sanitized same-origin ``?webrtc=`` prefix
        (reverse-proxy path prefixes) is preserved and any untrusted / cross-origin
        value dropped. A legacy ``?token=`` is also dropped so the redirect does
        not copy a secret into another request or browser-history entry.
        """
        web = self._web
        if self.has_bundled_client:
            location = "/webrtc_client.html"
            query_string = getattr(request, "query_string", "")
            params: list[tuple[str, str]] = []
            user_base = ""
            for key, value in parse_qsl(query_string, keep_blank_values=True):
                if key == "webrtc":
                    if not user_base:
                        user_base = value
                    continue
                if key == "token":
                    continue
                params.append((key, value))
            base = self._client_base or sanitize_webrtc_base(user_base)
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
            headers=self.cors_headers(request),
        )

    async def handle_cors_preflight(self, request: Any) -> Any:
        return self._web.Response(headers=self.cors_headers(request))
