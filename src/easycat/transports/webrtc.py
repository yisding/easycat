"""WebRTC transport: real peer-to-peer audio via aiortc.

Hosts an HTTP signaling server (aiohttp) on a configurable port.  Clients
POST an SDP offer to ``/offer`` and receive an SDP answer.  Audio is
exchanged over the WebRTC peer connection using the Opus codec.

Inbound audio (remote peer → pipeline) is decoded from Opus at 48 kHz and
resampled to the pipeline's target rate (default 16 kHz PCM16 mono).

Outbound audio (pipeline → remote peer) is resampled from whatever the TTS
provider emits to 48 kHz and sent via an Opus-encoded audio track.

Session events (transcripts, interruptions, per-turn latency) are forwarded
to the browser over a client-created data channel named ``"events"`` using
the JSON wire format in :mod:`easycat.transports._browser_events`; the
maintained reader-facing description lives in ``docs/browser-playground.md``.

Requires the ``webrtc`` extra: ``uv add 'easycat[webrtc]'``. From the
EasyCat repo, use ``uv sync --extra webrtc --group dev``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from easycat._concurrency import shielded_cleanup
from easycat._extras import require_module
from easycat._net import is_loopback_host, normalize_auth_token
from easycat.audio_format import AudioChunk
from easycat.runtime.scope import RuntimeScope
from easycat.teardown_budgets import (
    WEBRTC_OFFER_CANCEL_DRAIN_TIMEOUT_S as _OFFER_CANCEL_DRAIN_TIMEOUT_S,
)
from easycat.transports._base import (
    AudioQueueMixin,
    _raise_rollback_cancellation,
    make_version_info,
)
from easycat.transports._webrtc_audio import (
    WEBRTC_SAMPLE_RATE,
    OutboundAudioSource,
    audio_frame_pcm16_bytes,
)
from easycat.transports._webrtc_config import (
    ICEServer,
    WebRTCTransportConfig,
    webrtc_ice_servers_from_env,
    webrtc_transport_config_from_env,
)
from easycat.transports._webrtc_stats import WebRTCStatsState

__all__ = [
    "ICEServer",
    "WebRTCTransport",
    "WebRTCTransportConfig",
    "webrtc_ice_servers_from_env",
    "webrtc_transport_config_from_env",
]

if TYPE_CHECKING:
    from easycat.server._webrtc_handlers import WebRTCSignalingHandlers
    from easycat.server.auth import AuthPolicy

logger = logging.getLogger(__name__)

# WebRTC-specific degraded reason codes emitted on the session event bus.
_DEGRADED_NEGOTIATION_FAILED = "negotiation_failed"
_DEGRADED_INBOUND_CONSUME_ERROR = "inbound_consume_error"
_DEGRADED_OUTBOUND_QUEUE_FULL = "outbound_queue_full"

# Browser-created data channel carrying session events to the playground.
_EVENTS_CHANNEL_LABEL = "events"


def _inspect_static_dir(static_dir: str | Path) -> tuple[Path, bool, bool]:
    """Resolve static-directory state away from the asyncio event loop."""
    static_path = Path(static_dir)
    is_dir = static_path.is_dir()
    has_client = is_dir and (static_path / "webrtc_client.html").is_file()
    return static_path, is_dir, has_client


async def _wait_for_ice_gathering(pc: Any, completed: asyncio.Event) -> None:
    """Wait briefly for a candidate-complete SDP without polling the loop."""
    if pc.iceGatheringState == "complete":
        return
    try:
        await asyncio.wait_for(completed.wait(), timeout=2.0)
    except TimeoutError:
        pass


# ── WebRTC Transport ─────────────────────────────────────────────


class WebRTCTransport(AudioQueueMixin):
    """Transport that exchanges audio over a WebRTC peer connection.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Signaling
    ---------
    A lightweight HTTP server is started on ``config.host:config.port``.

    **POST /offer** — Client sends ``{"sdp": "...", "type": "offer"}``.
    Server creates an ``RTCPeerConnection``, sets the remote offer, adds
    an outbound audio track, creates an answer, and returns
    ``{"sdp": "...", "type": "answer"}``.  ICE candidates are gathered
    in-band (full ICE) before the answer is returned.

    **GET /config** — Returns browser ICE server configuration as JSON so
    clients can configure their ``RTCPeerConnection``. Credentials are omitted
    by default because this endpoint is public; set
    ``WebRTCTransportConfig.expose_ice_credentials`` only when that is
    appropriate for the deployment.

    **GET /health** — Returns ``{"status": "ok"}``.
    """

    send_audio_is_nonblocking = True

    transport_kind = "webrtc"

    _transport_name = "WebRTC"
    reports_audio_delivery = True
    # Deliberate flip from the prior implicit ``False`` default (which came from
    # ``getattr(..., False)`` when no attribute was declared): WebRTC is a
    # browser-mic transport like WebSocket, so it adopts the same EasyCat-side
    # AEC default of ``True`` for consistency across browser transports.
    # NOTE: browser WebRTC stacks may already apply their own echo cancellation;
    # if double-processing degrades audio, set ``enable_echo_cancellation=False``
    # explicitly on the session.
    default_echo_cancellation_enabled = True

    def __init__(self, config: WebRTCTransportConfig | None = None) -> None:
        self._config = config or WebRTCTransportConfig()
        self._init_audio_queue(
            self._config.max_pending_chunks,
            self._config.max_pending_bytes,
        )
        self._offer_request: Any | None = None

        # Peer connection state.
        self._pc: Any | None = None
        self._outbound = OutboundAudioSource()
        self._outbound_track: Any | None = None
        # Browser-created "events" data channel for the playground UI.
        self._events_channel: Any | None = None
        # ``_event_bus`` / ``_emit_degraded`` come from ``AudioQueueMixin``
        # (``_init_audio_queue`` above).  Session attaches the bus
        # post-construction; it is forwarded to ``_outbound`` (for
        # ``TransportAudioDelivered``) once a peer connects.

        # HTTP signaling server (aiohttp).
        self._web: Any = None  # cached aiohttp.web module
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._has_bundled_client = False

        # Background task that consumes the inbound audio track.
        self._consume_task: asyncio.Task[None] | None = None
        self._peer_generation = 0
        self._retiring_peer_generation: int | None = None
        self._offer_lock = asyncio.Lock()
        # Exact request-handler task currently owning ``_offer_lock``. Teardown
        # cancels and reaps it before waiting on the lock, so a client stalled
        # in request-body parsing or SDP negotiation cannot block disconnect.
        self._active_offer_task: asyncio.Task[Any] | None = None
        # Includes both the lock owner and handlers queued behind it. Register
        # before awaiting the lock so disconnect can account for a request that
        # passed its initial admission gate just before terminal state was
        # published.
        self._offer_tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        self._peer_closed = asyncio.Event()
        self._peer_closed.set()
        # ``disconnect`` publishes ``_connected=False`` before cleanup so offer
        # handlers reject promptly. Keep cleanup ownership separate from that
        # public state: failed resources remain retryable and reconnect is
        # blocked until a later disconnect finishes them.
        self._disconnect_cleanup_error: Exception | None = None
        self._outbound_cleanup_pending = False
        # A negotiated replacement peer is kept here until it is either
        # published atomically or closed. If cancellation/error interrupts the
        # old-peer retirement and the candidate close itself fails, disconnect()
        # retains an exact handle for a later cleanup retry.
        self._pending_peer_cleanup: Any | None = None
        # Per-server stats rate-limit / record state, shared with each lazily
        # built signaling-handlers instance (see ``_signaling``).
        self._stats_state = WebRTCStatsState()

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach transport and outbound delivery work to one runtime child."""
        super().set_runtime_scope(parent, name=name)
        scope = self._emit_scope
        assert scope is not None
        self._outbound._bind_event_scope(scope)

    @property
    def offer_request(self) -> Any | None:
        """Accepted aiohttp offer request, when this transport was built by a route."""
        return self._offer_request

    # ── Helpers ─────────────────────────────────────────────────

    def _auth_policy(self) -> AuthPolicy | None:
        """Build the unified bearer-token policy, or ``None`` for open access.

        No configured token maps to ``None`` (open); otherwise a
        :class:`~easycat.server.auth.BearerTokenAuth` carrying ``allow_query_token``
        is returned. Imported lazily so ``import easycat.transports.webrtc`` pulls
        no server package.
        """
        from easycat.server.auth import BearerTokenAuth

        token = normalize_auth_token(self._config.auth_token)
        if token is None:
            return None
        return BearerTokenAuth(token=token, allow_query_token=self._config.allow_query_token)

    def _signaling(self) -> WebRTCSignalingHandlers:
        """Build the shared stateless signaling surface from current state.

        Imported LAZILY (the optional ``webrtc`` server deps stay optional) and
        built fresh each call so it always reflects the live ``_web`` /
        ``_has_bundled_client``; the per-server rate-limit / record state persists
        across calls via the shared ``self._stats_state`` object passed in.
        """
        from easycat.server._webrtc_handlers import WebRTCSignalingHandlers

        return WebRTCSignalingHandlers(
            self._config,
            web=self._web,
            auth=self._auth_policy(),
            stats=self._stats_state,
            has_bundled_client=self._has_bundled_client,
        )

    def _is_current_peer_generation(self, peer_generation: int | None) -> bool:
        return peer_generation is None or (
            peer_generation == self._peer_generation
            and peer_generation != self._retiring_peer_generation
        )

    def _enqueue_sentinel_for_peer(self, peer_generation: int | None) -> None:
        if self._is_current_peer_generation(peer_generation):
            self._enqueue_sentinel()

    # The stateless signaling surface (CORS, auth, stats permission/quota/deque)
    # lives ONCE in ``easycat.server._webrtc_handlers.WebRTCSignalingHandlers``;
    # these thin delegators keep the transport's private names for the offer path
    # and the transport unit tests. ``_request_authorized`` is retained (rather
    # than inlined) because the offer path and the auth tests call it by name.

    def _cors_headers(self, request: Any) -> dict[str, str]:
        return self._signaling().cors_headers(request)

    def _request_authorized(self, request: Any) -> bool:
        return self._signaling().authorized(request)

    def _stats_write_permitted(self, request: Any) -> bool:
        return self._signaling().stats_write_permitted(request)

    def _stats_forbidden_response(self, request: Any) -> Any:
        return self._signaling().stats_forbidden_response(request)

    def _stats_quota_response(self, request: Any, message: str) -> Any:
        return self._signaling().stats_quota_response(request, message)

    def _stats_quota_error(self, stats_path: Path, snapshot: dict[str, object]) -> str | None:
        return self._signaling().stats_quota_error(stats_path, snapshot)

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the HTTP signaling server."""
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "connect":
                return
            raise RuntimeError("WebRTCTransport.connect() cannot run during disconnect()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "connect"
            try:
                await self._connect_unlocked()
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _connect_unlocked(self) -> None:
        """Connect while the caller owns ``_lifecycle_lock``."""
        if self._connected:
            return
        if self._disconnect_cleanup_error is not None:
            raise RuntimeError(
                "WebRTC transport cleanup is incomplete; call disconnect() "
                "again before reconnecting"
            ) from self._disconnect_cleanup_error

        auth_token = normalize_auth_token(self._config.auth_token)
        if not is_loopback_host(self._config.host) and auth_token is None:
            raise ValueError(
                "WebRTCTransportConfig.auth_token is required when binding WebRTC "
                "signaling to a non-loopback host"
            )

        self._web = require_module("aiohttp.web", extra="webrtc", purpose="WebRTC signaling")
        web = self._web

        self._reset_audio_queue()
        self._has_bundled_client = False
        self._peer_closed.set()

        app = web.Application()
        app.router.add_post("/offer", self._handle_offer)
        app.router.add_post("/stats", self._handle_stats)
        app.router.add_get("/config", self._handle_config)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/", self._handle_root)
        app.router.add_options("/offer", self._handle_cors_preflight)
        app.router.add_options("/stats", self._handle_cors_preflight)
        app.router.add_options("/config", self._handle_cors_preflight)

        # Serve static files — resolve the bundled-client sentinel first.
        static_dir = self._config.static_dir
        if static_dir == WebRTCTransportConfig._USE_BUNDLED:
            static_dir = WebRTCTransportConfig._BUNDLED_STATIC_DIR
        if static_dir is not None:
            static_path, is_dir, has_client = await asyncio.to_thread(
                _inspect_static_dir,
                static_dir,
            )
            if is_dir:
                self._has_bundled_client = has_client
                app.router.add_static("/", static_path)
                logger.info("Serving static files from %s", static_path)
            else:
                logger.warning(
                    "Configured static_dir '%s' does not exist or is not a directory; "
                    "static file serving is disabled",
                    static_path,
                )

        runner = web.AppRunner(app)
        site: Any | None = None
        try:
            await runner.setup()
            site = web.TCPSite(runner, self._config.host, self._config.port)
            await site.start()
        except BaseException as startup_error:
            # Publish the partial stack before protected rollback so cleanup
            # failures and repeated caller cancellation remain retryable.
            self._app = app
            self._runner = runner
            self._site = site
            settlement = await shielded_cleanup(
                self._rollback_failed_connect,
            )
            cancellation = (
                asyncio.CancelledError()
                if settlement.cancellation_requests
                and not isinstance(startup_error, asyncio.CancelledError)
                else None
            )
            cleanup_error = settlement.error or settlement.result
            _raise_rollback_cancellation(cancellation, startup_error, cleanup_error)
            if cleanup_error is not None:
                raise startup_error from cleanup_error
            raise

        self._app = app
        self._runner = runner
        self._site = site
        self._connected = True
        self._ensure_browser_event_forwarder()
        logger.info(
            "WebRTC signaling server listening on http://%s:%d",
            self._config.host,
            self._config.port,
        )

    async def disconnect(self) -> None:
        """Close the peer connection and stop the signaling server."""
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "disconnect":
                return
            raise RuntimeError("WebRTCTransport.disconnect() cannot run during connect()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "disconnect"
            try:
                try:
                    await self._disconnect_unlocked()
                except asyncio.CancelledError:
                    self._publish_interrupted_disconnect()
                    raise
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _disconnect_unlocked(self) -> None:
        """Disconnect while the caller owns ``_lifecycle_lock``."""
        if not self._has_disconnect_work():
            return
        if self._active_offer_task is asyncio.current_task():
            raise RuntimeError(
                "WebRTCTransport.disconnect() cannot run from the active offer handler"
            )
        was_connected = self._connected
        if was_connected:
            # Set this before the first cleanup await. A cancelled disconnect
            # must not forget that the current outbound source still needs its
            # async close on retry.
            self._outbound_cleanup_pending = True

        # Publish terminal state before touching the active handler. This makes
        # every queued/new offer return 503 as soon as it acquires the lock.
        self._connected = False

        cleanup_errors: list[Exception] = []
        offers_reaped = await self._stop_active_offer_for_disconnect(cleanup_errors)

        if not offers_reaped:
            # A cancellation-resistant handler can still own the offer lock
            # and candidate/peer locals. Do not race it by closing shared
            # resources or asking aiohttp to wait for its request task. Leave
            # exact ownership reachable for a later disconnect retry.
            self._client_connected.clear()
            self._enqueue_sentinel()
            self._peer_closed.set()
            self._disconnect_cleanup_error = cleanup_errors[0]
            raise cleanup_errors[0]

        # Use the lock only as a short barrier after cancelling the active owner.
        # Previously disconnect waited here while an unbounded request.json() or
        # SDP operation still owned the lock. Do not hold it across aiohttp
        # cleanup: cleanup itself waits for request handlers to finish.
        async with self._offer_lock:
            pass

        await self._stop_consumer_for_disconnect(cleanup_errors)
        await self._close_peer_for_disconnect(cleanup_errors)
        await self._close_pending_peer_for_disconnect(cleanup_errors)
        self._close_browser_events_for_disconnect(cleanup_errors)
        await self._close_outbound_for_disconnect(
            was_connected=was_connected,
            cleanup_errors=cleanup_errors,
        )
        await self._close_signaling_for_disconnect(cleanup_errors)

        self._has_bundled_client = False
        self._enqueue_sentinel()
        self._client_connected.clear()
        self._peer_closed.set()
        await self._attempt_disconnect_cleanup(
            "transport diagnostic events",
            self._drain_emit_tasks(),
            cleanup_errors,
        )
        self._disconnect_cleanup_error = cleanup_errors[0] if cleanup_errors else None
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _rollback_failed_connect(self) -> Exception | None:
        """Clean a partially initialized signaling stack in an owned task."""
        cleanup_errors: list[Exception] = []
        try:
            await self._close_signaling_for_disconnect(cleanup_errors)
        except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            retained_error = (
                exc
                if isinstance(exc, Exception)
                else RuntimeError("WebRTC connect rollback was interrupted")
            )
            self._record_disconnect_cleanup_error(
                "connect rollback",
                retained_error,
                cleanup_errors,
            )
        self._has_bundled_client = False
        self._disconnect_cleanup_error = cleanup_errors[0] if cleanup_errors else None
        return self._disconnect_cleanup_error

    def _publish_interrupted_disconnect(self) -> None:
        """Retain cleanup ownership before preserving caller cancellation."""
        self._connected = False
        self._client_connected.clear()
        self._enqueue_sentinel()
        self._peer_closed.set()
        self._disconnect_cleanup_error = RuntimeError(
            "WebRTC disconnect was interrupted by cancellation"
        )

    async def _stop_active_offer_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> bool:
        """Cancel and reap the offer-lock owner without waiting forever.

        Returns ``False`` when the active handler ignores cancellation past the
        bounded drain window. In that case its lock and local candidate state
        remain live, so the caller must retain the signaling stack for an
        explicit retry rather than continuing concurrent teardown.
        """
        offer_task = self._active_offer_task
        current = asyncio.current_task()
        if offer_task is None:
            return True
        if offer_task is current:
            raise RuntimeError(
                "WebRTCTransport.disconnect() cannot run from the active offer handler"
            )

        if not offer_task.done():
            offer_task.cancel()
        done, pending = await asyncio.wait(
            (offer_task,),
            timeout=_OFFER_CANCEL_DRAIN_TIMEOUT_S,
        )
        if pending:
            # Queued handlers have no provider work of their own; cancel them
            # so they do not stay forever behind the uncooperative lock owner.
            queued = tuple(
                task
                for task in self._offer_tasks
                if task is not offer_task and task is not current and not task.done()
            )
            for task in queued:
                task.cancel()
            if queued:
                await asyncio.gather(*queued, return_exceptions=True)
            error = TimeoutError(
                "WebRTC offer handler did not stop after cancellation; "
                "call disconnect() again after it unwinds"
            )
            self._record_disconnect_cleanup_error(
                "active WebRTC offer",
                error,
                cleanup_errors,
            )
            return False

        assert offer_task in done
        try:
            offer_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self._record_disconnect_cleanup_error(
                "active WebRTC offer",
                exc,
                cleanup_errors,
            )
        finally:
            if self._active_offer_task is offer_task:
                self._active_offer_task = None
        return True

    async def _stop_consumer_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> None:
        """Cancel and reap the inbound consumer without blocking later cleanup."""
        consume_task = self._consume_task
        if consume_task is not None:
            current = asyncio.current_task()
            cancellation_count = current.cancelling() if current is not None else 0
            if not consume_task.done():
                consume_task.cancel()
            try:
                await consume_task
            except asyncio.CancelledError:
                if current is not None and current.cancelling() > cancellation_count:
                    raise
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                self._record_disconnect_cleanup_error(
                    "inbound audio consumer",
                    exc,
                    cleanup_errors,
                )
            finally:
                if self._consume_task is consume_task:
                    self._consume_task = None
            if current is not None and current.cancelling() > cancellation_count:
                # A cancellation-resistant child can swallow the cancellation
                # forwarded by Task.cancel(), making ``await child`` return
                # normally. Preserve the caller's independent request.
                raise asyncio.CancelledError

    async def _close_peer_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> None:
        """Close the peer, retaining its reference only when close fails."""
        pc = self._pc
        if pc is not None:
            succeeded = await self._attempt_disconnect_cleanup(
                "peer connection",
                pc.close(),
                cleanup_errors,
            )
            if succeeded and self._pc is pc:
                self._pc = None
        self._outbound_track = None

    async def _close_pending_peer_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> None:
        """Close a replacement candidate retained by an interrupted swap."""
        pending = self._pending_peer_cleanup
        if pending is None:
            return
        succeeded = await self._attempt_disconnect_cleanup(
            "unpublished peer connection",
            pending.close(),
            cleanup_errors,
        )
        if succeeded and self._pending_peer_cleanup is pending:
            self._pending_peer_cleanup = None

    def _close_browser_events_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> None:
        """Release the browser-event subscription without blocking later cleanup."""
        try:
            self._close_browser_event_forwarder()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self._record_disconnect_cleanup_error(
                "browser event forwarder",
                exc,
                cleanup_errors,
            )
        self._events_channel = None

    async def _close_outbound_for_disconnect(
        self,
        *,
        was_connected: bool,
        cleanup_errors: list[Exception],
    ) -> None:
        """Stop outbound delivery work, preserving retry ownership on failure."""
        if was_connected:
            self._outbound_cleanup_pending = True
        if self._outbound_cleanup_pending:
            outbound_stopped = True
            try:
                self._outbound.stop()  # no-op by design; track is discarded with the PC
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                outbound_stopped = False
                self._record_disconnect_cleanup_error(
                    "outbound audio stop",
                    exc,
                    cleanup_errors,
                )
            # Drain the outbound source's own off-RTP-path emit tasks (a
            # different set from the transport-level ``_emit_tasks`` drained
            # below), mirroring LocalTransport.stop() -> _drain_emit_tasks().
            outbound_closed = await self._attempt_disconnect_cleanup(
                "outbound audio source",
                self._outbound.aclose(),
                cleanup_errors,
            )
            if outbound_stopped and outbound_closed:
                self._outbound_cleanup_pending = False

    async def _close_signaling_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> None:
        """Stop the HTTP signaling stack, retaining only failed references."""
        site = self._site
        if site is not None:
            succeeded = await self._attempt_disconnect_cleanup(
                "HTTP signaling site",
                site.stop(),
                cleanup_errors,
            )
            if succeeded and self._site is site:
                self._site = None
        runner = self._runner
        if runner is not None:
            succeeded = await self._attempt_disconnect_cleanup(
                "HTTP signaling runner",
                runner.cleanup(),
                cleanup_errors,
            )
            if succeeded and self._runner is runner:
                self._runner = None
        if self._runner is None:
            self._app = None

    def _has_disconnect_work(self) -> bool:
        return any(
            (
                self._connected,
                self._active_offer_task is not None,
                bool(self._offer_tasks),
                self._consume_task is not None,
                self._pc is not None,
                self._pending_peer_cleanup is not None,
                self._browser_event_forwarder is not None,
                self._outbound_cleanup_pending,
                self._site is not None,
                self._runner is not None,
                bool(self._emit_tasks),
                self._disconnect_cleanup_error is not None,
            )
        )

    @staticmethod
    def _record_disconnect_cleanup_error(
        stage: str,
        exc: Exception,
        cleanup_errors: list[Exception],
    ) -> None:
        logger.error("WebRTC cleanup failed during %s", stage, exc_info=exc)
        cleanup_errors.append(exc)

    async def _attempt_disconnect_cleanup(
        self,
        stage: str,
        awaitable: Any,
        cleanup_errors: list[Exception],
    ) -> bool:
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self._record_disconnect_cleanup_error(stage, exc, cleanup_errors)
            return False
        return True

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send an audio chunk to the remote WebRTC peer."""
        if self._pc is None or self._outbound_track is None:
            return False

        from easycat._audio_utils import resample, to_mono, validate_pcm16_format

        # Transport sends have a per-call delivery result and no tail-send
        # phase. Keep this fallback conversion immediate; normal configured
        # sessions already perform stateful output alignment in TTSBase.
        validate_pcm16_format("chunk.format", chunk.format)
        if len(chunk.data) % chunk.format.frame_size:
            raise ValueError(
                "chunk.data must contain complete PCM frames "
                f"(got {len(chunk.data)} bytes for {chunk.format.frame_size}-byte frames)"
            )
        pcm_data = chunk.data
        if chunk.format.channels != 1:
            pcm_data = to_mono(pcm_data, chunk.format.channels)
        if chunk.format.sample_rate != WEBRTC_SAMPLE_RATE:
            pcm_data = resample(pcm_data, chunk.format.sample_rate, WEBRTC_SAMPLE_RATE)

        self._outbound._event_bus = self._event_bus
        accepted = self._outbound.enqueue(
            pcm_data,
            original_chunk=chunk,
            session_id=getattr(chunk, "_easycat_session_id", None),
            turn_id=getattr(chunk, "_easycat_turn_id", None),
            turn_ref=getattr(chunk, "_easycat_turn_ref", None),
        )
        if not accepted:
            # Mirror WebTransport: a full outbound queue dropping a TTS frame
            # must reach the journal so backpressure is observable, not just a
            # logger.debug line lost outside the debug bundle.
            self._emit_degraded(
                _DEGRADED_OUTBOUND_QUEUE_FULL,
                f"dropped {len(pcm_data)}-byte TTS frame; outbound queue full",
            )
        return accepted

    async def clear_audio(self) -> None:
        """Discard queued outbound audio (useful during barge-in)."""
        self._outbound.clear()

    def drain_aec_reference_frames(self) -> list[AudioChunk]:
        """Return and clear pending AEC far-end reference frames, oldest first.

        Shared AEC reference capability drained by AudioRouter before the
        near-end mic frame is processed, so the far-end reference is always fed
        to the echo canceller ahead of the corresponding near-end frame. Each
        chunk retains the original reference format so AEC can reject a
        near/far sample-rate mismatch instead of processing mislabeled PCM.

        Returns an empty list when the outbound source is not present.
        """
        outbound = self._outbound
        if outbound is None:
            return []
        return outbound.drain_aec_reference_frames()

    async def _send_client_event(self, payload: dict[str, Any]) -> None:
        """Push one JSON event message over the browser's "events" data channel."""
        channel = self._events_channel
        if channel is None or getattr(channel, "readyState", None) != "open":
            return
        channel.send(json.dumps(payload))

    async def _raise_cancelled_offer_after_peer_close(
        self,
        pc: Any | None,
        cancellation: asyncio.CancelledError,
    ) -> NoReturn:
        """Close an unpublished peer in an owned task, then preserve cancellation."""
        cleanup_error = await self._close_unpublished_peer(
            pc,
            finish_despite_cancellation=True,
        )
        if cleanup_error is not None:
            self._publish_failed_peer_replacement(
                RuntimeError("WebRTC cancelled-offer peer cleanup failed")
            )
            raise cancellation from cleanup_error
        raise cancellation

    async def _close_unpublished_peer(
        self,
        pc: Any | None,
        *,
        finish_despite_cancellation: bool = False,
    ) -> BaseException | None:
        """Close *pc* in an owned task, retaining it if cleanup does not finish."""
        if pc is None:
            return None
        self._pending_peer_cleanup = pc
        settlement = await shielded_cleanup(pc.close)
        cleanup_error = settlement.error
        if cleanup_error is None and self._pending_peer_cleanup is pc:
            self._pending_peer_cleanup = None
        if settlement.cancellation_requests and not finish_despite_cancellation:
            later_cancellation = asyncio.CancelledError()
            if cleanup_error is not None:
                raise later_cancellation from cleanup_error
            raise later_cancellation
        if cleanup_error is not None:
            return cleanup_error
        return None

    async def _retire_current_peer_for_replacement(self) -> None:
        """Retire the published peer without exposing a half-committed swap."""
        cleanup_errors: list[Exception] = []
        await self._stop_consumer_for_disconnect(cleanup_errors)
        await self._close_peer_for_disconnect(cleanup_errors)
        await self._close_outbound_for_disconnect(
            was_connected=True,
            cleanup_errors=cleanup_errors,
        )
        if cleanup_errors:
            raise cleanup_errors[0]

    def _publish_failed_peer_replacement(self, error: Exception) -> None:
        """Make interrupted replacement cleanup explicit and retryable."""
        self._connected = False
        self._client_connected.clear()
        self._peer_closed.set()
        self._outbound_track = None
        self._events_channel = None
        self._retiring_peer_generation = None
        # The old outbound object remains installed until the candidate is
        # committed, so a retrying disconnect can safely call aclose again.
        self._outbound_cleanup_pending = True
        self._disconnect_cleanup_error = error
        self._enqueue_sentinel()

    # ── Signaling handlers ────────────────────────────────────────

    async def _handle_offer(self, request: Any) -> Any:
        """Handle an SDP offer from the browser client."""
        # This check intentionally precedes lock acquisition: once disconnect
        # publishes terminal state, fresh HTTP requests must receive 503 even
        # if an old handler is still stuck holding the previous offer lock.
        if not self._connected:
            return self._unavailable_response(request)
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - async request handlers have a task
            raise RuntimeError("WebRTC offer handler requires an asyncio task")
        self._offer_tasks.add(current)
        try:
            async with self._offer_lock:
                if not self._connected:
                    return self._unavailable_response(request)
                self._active_offer_task = current
                try:
                    return await self._handle_offer_locked(request)
                finally:
                    if self._active_offer_task is current:
                        self._active_offer_task = None
        finally:
            self._offer_tasks.discard(current)

    def _unavailable_response(self, request: Any) -> Any:
        """Build a 503 response for offers received while disconnected."""
        web = self._web
        return web.Response(
            status=503,
            text=json.dumps({"error": "Transport is shutting down"}),
            content_type="application/json",
            headers=self._cors_headers(request),
        )

    async def _discard_unpublished_offer_during_shutdown(
        self,
        request: Any,
        pc: Any,
    ) -> Any:
        """Close a negotiated candidate that raced with terminal teardown."""
        candidate_cleanup_error = await self._close_unpublished_peer(pc)
        if candidate_cleanup_error is not None:
            shutdown_error = RuntimeError(
                "WebRTC offer completed negotiation after shutdown and candidate cleanup failed"
            )
            self._publish_failed_peer_replacement(shutdown_error)
            raise shutdown_error from candidate_cleanup_error
        return self._unavailable_response(request)

    async def _handle_offer_locked(self, request: Any) -> Any:
        """Handle an SDP offer with peer replacement serialized."""
        web = self._web
        handlers = self._signaling()
        # Bail before doing any work if teardown has already begun. ``disconnect``
        # clears ``_connected`` before cancelling the active offer and waiting on
        # ``_offer_lock``, so queued/new handlers observe terminal state here.
        if not self._connected:
            return self._unavailable_response(request)
        if not handlers.authorized(request):
            return handlers.unauthorized_response(request)
        aiortc = require_module("aiortc", extra="webrtc", purpose="WebRTC transport")
        RTCPeerConnection = aiortc.RTCPeerConnection
        RTCSessionDescription = aiortc.RTCSessionDescription
        RTCConfiguration = aiortc.RTCConfiguration
        RTCIceServer = aiortc.RTCIceServer

        try:
            params = await request.json()
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return web.Response(
                status=400,
                text=json.dumps({"error": "Invalid JSON"}),
                content_type="application/json",
                headers=self._cors_headers(request),
            )

        # A request-body implementation may swallow Task.cancel(). Avoid
        # allocating a peer after disconnect has already closed admission.
        if not self._connected:
            return self._unavailable_response(request)

        sdp = params.get("sdp") if isinstance(params, dict) else None
        sdp_type = params.get("type") if isinstance(params, dict) else None
        if not isinstance(sdp, str) or not sdp.strip() or sdp_type != "offer":
            return web.Response(
                status=400,
                text=json.dumps(
                    {
                        "error": (
                            "Expected JSON body with non-empty 'sdp' and 'type' set to 'offer'"
                        )
                    }
                ),
                content_type="application/json",
                headers=self._cors_headers(request),
            )

        # Negotiate the replacement peer against a pending generation first. Do
        # not make it current or tear down the existing peer until the incoming
        # SDP has been accepted; otherwise a malformed replacement offer can
        # strand receive_audio() after the old peer's shutdown sentinel is
        # intentionally suppressed as stale.
        peer_generation = self._peer_generation + 1

        # Build ICE configuration from the shared serializer.
        ice_servers = [
            RTCIceServer(**entry)
            for entry in handlers.ice_servers_as_dicts(include_credentials=True)
        ]
        rtc_config = RTCConfiguration(iceServers=ice_servers)

        pc = None
        # aiortc fires the synchronous ``track`` event *during*
        # ``setRemoteDescription`` — before this generation is committed below.
        # Capture the remote audio track here and only start ``_consume_audio``
        # against it after the commit/teardown/swap, so a successfully
        # negotiated peer always gets an inbound reader instead of being
        # rejected as a not-yet-current generation.
        captured_track: Any | None = None
        try:
            pc = RTCPeerConnection(rtc_config)
            ice_gathering_complete = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def on_ice_gathering_state_change() -> None:
                if pc.iceGatheringState == "complete":
                    ice_gathering_complete.set()

            # Prepare an outbound track for the new connection, but keep the
            # existing peer's source active until negotiation succeeds.
            outbound = OutboundAudioSource()
            emit_scope = self._emit_scope
            if emit_scope is not None:
                outbound._bind_event_scope(emit_scope)
            outbound_track = outbound.create_track()
            pc.addTrack(outbound_track)

            # Listen for the remote audio track. The event fires during
            # ``setRemoteDescription`` (before commit), so just capture the
            # track; ``_consume_audio`` is started after the swap below.
            @pc.on("track")
            def on_track(track: Any) -> None:
                nonlocal captured_track
                if track.kind == "audio":
                    logger.info("WebRTC remote audio track received")
                    captured_track = track

            # The browser playground creates an "events" data channel before
            # offering; capture it so session events (transcripts,
            # interruptions, latency) can be pushed to the page. The channel
            # opens only after the connection is established — well past the
            # generation commit below — so guard against stale peers here.
            @pc.on("datachannel")
            def on_datachannel(channel: Any) -> None:
                if not self._is_current_peer_generation(peer_generation):
                    return
                if channel.label == _EVENTS_CHANNEL_LABEL:
                    logger.info("WebRTC events data channel received")
                    self._events_channel = channel

            abnormal_disconnect_recorded = False

            @pc.on("connectionstatechange")
            async def on_connectionstatechange() -> None:
                nonlocal abnormal_disconnect_recorded
                if not self._is_current_peer_generation(peer_generation):
                    return
                state = pc.connectionState
                logger.info("WebRTC connection state: %s", state)
                if state == "connected":
                    # A later drop after a genuine recovery is a new incident.
                    abnormal_disconnect_recorded = False
                    self._client_connected.set()
                elif state in ("disconnected", "failed", "closed"):
                    # ``disconnected``/``failed`` are abnormal peer drops (ICE
                    # loss, connectivity failure); ``closed`` is the terminal
                    # state of an application-initiated teardown and is clean.
                    if state in ("disconnected", "failed") and not abnormal_disconnect_recorded:
                        self._record_transport_disconnect(f"webrtc peer {state}")
                        abnormal_disconnect_recorded = True
                    self._client_connected.clear()
                    self._peer_closed.set()
                    # Null the outbound track so send_audio() reports the
                    # drop (via bool False) instead of silently queueing into
                    # a source that nothing is draining any more.
                    self._outbound_track = None
                    self._enqueue_sentinel_for_peer(peer_generation)

            # Set remote offer and create answer.
            offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
            await pc.setRemoteDescription(offer)

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            # Wait for ICE gathering to complete before responding, so that
            # the SDP answer includes candidates (important behind NAT).
            await _wait_for_ice_gathering(pc, ice_gathering_complete)
        except asyncio.CancelledError as cancellation:
            await self._raise_cancelled_offer_after_peer_close(pc, cancellation)
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.warning("WebRTC offer handling failed: %s", exc)
            self._emit_degraded(
                _DEGRADED_NEGOTIATION_FAILED,
                f"SDP negotiation failed: {type(exc).__name__}: {exc}",
                fatal=False,
            )
            if pc is not None:
                await pc.close()
            return web.Response(
                status=400,
                text=json.dumps({"error": f"SDP negotiation failed: {exc}"}),
                content_type="application/json",
                headers=self._cors_headers(request),
            )

        assert pc is not None
        return await self._commit_negotiated_offer(
            request,
            pc=pc,
            outbound=outbound,
            outbound_track=outbound_track,
            captured_track=captured_track,
            peer_generation=peer_generation,
        )

    async def _commit_negotiated_offer(
        self,
        request: Any,
        *,
        pc: Any,
        outbound: OutboundAudioSource,
        outbound_track: Any,
        captured_track: Any | None,
        peer_generation: int,
    ) -> Any:
        """Atomically install a negotiated candidate or discard it on shutdown."""
        web = self._web
        # A third-party SDP await can consume Task.cancel() and return normally.
        # Re-check the state disconnect published before retiring the current peer
        # or committing this candidate.
        if not self._connected:
            return await self._discard_unpublished_offer_during_shutdown(request, pc)

        # Close any existing peer connection only after the replacement SDP is
        # proven valid. Keep the old generation current until its resources
        # retire successfully; the new task is created only at the atomic swap
        # below, so this block can never cancel it.
        # Suppress terminal callbacks from the old peer while it retires
        # without claiming the candidate generation before publication.
        self._retiring_peer_generation = self._peer_generation
        try:
            await self._retire_current_peer_for_replacement()
        except asyncio.CancelledError as cancellation:
            self._publish_failed_peer_replacement(
                RuntimeError("WebRTC peer replacement was interrupted by cancellation")
            )
            await self._raise_cancelled_offer_after_peer_close(pc, cancellation)
        except Exception as retirement_error:
            self._publish_failed_peer_replacement(retirement_error)
            candidate_cleanup_error = await self._close_unpublished_peer(pc)
            if candidate_cleanup_error is not None:
                raise retirement_error from candidate_cleanup_error
            raise

        # Retirement itself awaits third-party cleanup. A cancellation-resistant
        # close can return normally after disconnect has published terminal
        # state, so check once more before atomically installing this candidate.
        if not self._connected:
            self._retiring_peer_generation = None
            return await self._discard_unpublished_offer_during_shutdown(request, pc)

        # Clear stale audio from the previous peer so it doesn't leak into
        # the new session's receive_audio() iterator. Do not replace the queue:
        # Session.receive_audio() may already be blocked on this object.
        self._drain_audio_queue()

        # Publish the complete replacement in one no-await section. Cancellation
        # can no longer strand a local candidate or leave the generation
        # pointing at an unpublished peer.
        self._peer_generation = peer_generation
        self._retiring_peer_generation = None
        self._client_connected.clear()
        self._peer_closed.clear()
        self._pc = pc
        self._outbound = outbound
        self._outbound_track = outbound_track
        # Drop the previous peer's events channel; the replacement peer's
        # channel arrives via the generation-guarded ``datachannel`` callback.
        self._events_channel = None

        # Now that the new generation is current, start the inbound reader for
        # the track captured during ``setRemoteDescription`` and register its
        # ``ended`` handler. ``_consume_audio`` is generation-guarded internally,
        # so starting it post-commit is safe.
        if captured_track is not None:

            @captured_track.on("ended")
            async def on_ended() -> None:
                if not self._is_current_peer_generation(peer_generation):
                    return
                logger.info("WebRTC remote audio track ended")
                self._enqueue_sentinel_for_peer(peer_generation)

            self._consume_task = asyncio.ensure_future(
                self._consume_audio(captured_track, peer_generation=peer_generation)
            )

        return web.Response(
            content_type="application/json",
            text=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}),
            headers=self._cors_headers(request),
        )

    # ── Stateless signaling handlers (shared) ─────────────────────
    # ``/config``, ``/stats``, ``/health``, ``/`` (root), and the CORS preflight
    # are the byte-identical stateless surface lifted into
    # ``WebRTCSignalingHandlers``; the singleton transport serves the FLAT
    # ``status: ok`` health payload and the flat root (``client_base=""``).

    async def _handle_config(self, request: Any) -> Any:
        return await self._signaling().handle_config(request)

    async def _handle_stats(self, request: Any) -> Any:
        return await self._signaling().handle_stats(request)

    async def _handle_health(self, request: Any) -> Any:
        return await self._signaling().handle_health(request)

    async def _handle_root(self, request: Any) -> Any:
        return await self._signaling().handle_root(request)

    async def _handle_cors_preflight(self, request: Any) -> Any:
        return await self._signaling().handle_cors_preflight(request)

    # ── Audio track consumer ──────────────────────────────────────

    async def _consume_audio(self, track: Any, *, peer_generation: int | None = None) -> None:
        """Read audio frames from the remote track and enqueue as AudioChunk.

        Always enqueues a sentinel on exit so that ``receive_audio()`` does not
        block indefinitely if the track ends without a connection-state callback.
        """
        from easycat._audio_utils import PCM16StreamResampler, to_mono

        target_rate = self._config.audio_format.sample_rate
        target_format = self._config.audio_format
        resampler = PCM16StreamResampler(target_rate)

        logger.info("Consuming WebRTC audio track (target %d Hz)", target_rate)

        try:
            while True:
                frame = await track.recv()
                if not self._is_current_peer_generation(peer_generation):
                    break

                # Extract raw PCM from the av.AudioFrame. aiortc decodes Opus
                # to s16 at 48 kHz by default, but PyAV plane buffers can
                # include padding; the helper returns only valid samples.
                raw, frame_rate, channels = audio_frame_pcm16_bytes(frame)

                # Downmix to mono if needed.
                if channels > 1:
                    raw = to_mono(raw, channels)

                raw = resampler.process(raw, frame_rate)

                chunk = AudioChunk(data=raw, format=target_format)
                if raw and self._is_current_peer_generation(peer_generation):
                    self._enqueue_chunk(chunk, context="WebRTC")

        except StopAsyncIteration:
            logger.info("WebRTC audio track stream ended")
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            # aiortc raises MediaStreamError when the track ends.
            aiortc = require_module("aiortc", extra="webrtc", purpose="WebRTC transport")
            if isinstance(exc, aiortc.MediaStreamError):
                logger.info("WebRTC audio track stream ended")
            else:
                logger.warning("WebRTC audio consume error: %s", exc)
                self._emit_degraded(
                    _DEGRADED_INBOUND_CONSUME_ERROR,
                    f"inbound audio track failed: {type(exc).__name__}: {exc}",
                )
        finally:
            tail = resampler.finish()
            if tail and self._is_current_peer_generation(peer_generation):
                self._enqueue_chunk(
                    AudioChunk(data=tail, format=target_format),
                    context="WebRTC",
                )
            # Ensure the pipeline unblocks even if on_ended/connectionstatechange
            # callbacks don't fire.  Duplicate sentinels are harmless — the first
            # one stops receive_audio() and extras are cleared on next connection.
            self._enqueue_sentinel_for_peer(peer_generation)

    async def wait_closed(self) -> None:
        """Wait until the current peer connection is closed or failed."""
        await self._peer_closed.wait()

    def _prepare_external_signaling(self, web: Any) -> None:
        """Mark this transport as owned by an outer multi-session signaling app."""
        self._web = web
        self._connected = True
        self._reset_audio_queue()
        self._peer_closed.set()
        self._has_bundled_client = False

    # ── Properties ────────────────────────────────────────────────

    @property
    def has_client(self) -> bool:
        return self._pc is not None and self._pc.connectionState == "connected"

    def version_info(self) -> dict[str, str]:
        return make_version_info("webrtc", "aiortc")
