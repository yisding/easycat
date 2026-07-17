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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from easycat._extras import require_module
from easycat._net import is_loopback_host, normalize_auth_token
from easycat.audio_format import AudioChunk
from easycat.transports._base import AudioQueueMixin, make_version_info
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
        self._init_audio_queue(self._config.max_pending_chunks)

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
        self._offer_lock = asyncio.Lock()
        self._peer_closed = asyncio.Event()
        self._peer_closed.set()
        # Per-server stats rate-limit / record state, shared with each lazily
        # built signaling-handlers instance (see ``_signaling``).
        self._stats_state = WebRTCStatsState()

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
        return peer_generation is None or peer_generation == self._peer_generation

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
        if self._connected:
            return

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
            static_path = Path(static_dir)
            if static_path.is_dir():
                default_client = static_path / "webrtc_client.html"
                if default_client.is_file():
                    self._has_bundled_client = True
                app.router.add_static("/", static_path)
                logger.info("Serving static files from %s", static_path)
            else:
                logger.warning(
                    "Configured static_dir '%s' does not exist or is not a directory; "
                    "static file serving is disabled",
                    static_path,
                )

        self._app = app
        try:
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
            await self._site.start()
        except Exception:
            self._has_bundled_client = False
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            self._site = None
            self._app = None
            raise

        self._connected = True
        self._ensure_browser_event_forwarder()
        logger.info(
            "WebRTC signaling server listening on http://%s:%d",
            self._config.host,
            self._config.port,
        )

    async def disconnect(self) -> None:
        """Close the peer connection and stop the signaling server."""
        if not self._connected:
            return

        # Flip the public state while serialized against ``_handle_offer`` so an
        # in-flight offer either finishes before teardown starts, or every offer
        # queued behind teardown immediately observes the disconnected state. Do
        # not hold this lock across aiohttp cleanup: cleanup waits for active
        # request handlers, and queued ``/offer`` handlers need the lock in order
        # to return their shutdown 503 response.
        async with self._offer_lock:
            if not self._connected:
                return
            self._connected = False

        # Cancel the inbound audio consumer task.
        if self._consume_task is not None and not self._consume_task.done():
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
            self._consume_task = None

        # Close the peer connection.
        if self._pc is not None:
            await self._pc.close()
            self._pc = None

        self._close_browser_event_forwarder()
        self._events_channel = None
        self._outbound.stop()  # no-op by design; track is discarded with the PC
        # Drain the outbound source's own off-RTP-path emit tasks (a *different*
        # set from the transport-level ``_emit_tasks`` drained below), mirroring
        # LocalTransport.stop() -> _drain_emit_tasks().
        await self._outbound.aclose()

        # Shut down HTTP server.
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        self._has_bundled_client = False

        self._enqueue_sentinel()
        self._client_connected.clear()
        self._peer_closed.set()
        await self._drain_emit_tasks()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send an audio chunk to the remote WebRTC peer."""
        if self._pc is None or self._outbound_track is None:
            return False

        from easycat._audio_utils import resample

        # Resample to 48 kHz for Opus encoding.
        if chunk.format.sample_rate != WEBRTC_SAMPLE_RATE:
            pcm_data = resample(chunk.data, chunk.format.sample_rate, WEBRTC_SAMPLE_RATE)
        else:
            pcm_data = chunk.data

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

    def drain_aec_reference_frames(self) -> list[bytes]:
        """Return and clear pending AEC far-end reference frames, oldest first.

        Shared AEC reference capability drained by AudioRouter before the
        near-end mic frame is processed, so the far-end reference is always fed
        to the echo canceller ahead of the corresponding near-end frame.

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

    # ── Signaling handlers ────────────────────────────────────────

    async def _handle_offer(self, request: Any) -> Any:
        """Handle an SDP offer from the browser client."""
        async with self._offer_lock:
            return await self._handle_offer_locked(request)

    def _unavailable_response(self, request: Any) -> Any:
        """Build a 503 response for offers received while disconnected."""
        web = self._web
        return web.Response(
            status=503,
            text=json.dumps({"error": "Transport is shutting down"}),
            content_type="application/json",
            headers=self._cors_headers(request),
        )

    async def _handle_offer_locked(self, request: Any) -> Any:
        """Handle an SDP offer with peer replacement serialized."""
        web = self._web
        handlers = self._signaling()
        # Bail before doing any work if teardown has already begun. ``disconnect``
        # clears ``_connected`` under ``_offer_lock``, so once we hold the lock the
        # value is stable for the duration of this handler.
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
        except Exception:
            return web.Response(
                status=400,
                text=json.dumps({"error": "Invalid JSON"}),
                content_type="application/json",
                headers=self._cors_headers(request),
            )

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

            # Re-check teardown before committing the new peer. This handler still
            # holds ``_offer_lock``, so ``disconnect`` cannot flip ``_connected``
            # between the initial guard and this commit point; keep the guard so a
            # half-built PC is discarded if the locking changes in the future.
            if not self._connected:
                await pc.close()
                return self._unavailable_response(request)

            # Prepare an outbound track for the new connection, but keep the
            # existing peer's source active until negotiation succeeds.
            outbound = OutboundAudioSource()
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

            @pc.on("connectionstatechange")
            async def on_connectionstatechange() -> None:
                if not self._is_current_peer_generation(peer_generation):
                    return
                state = pc.connectionState
                logger.info("WebRTC connection state: %s", state)
                if state == "connected":
                    self._client_connected.set()
                elif state in ("disconnected", "failed", "closed"):
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
            start = time.monotonic()
            while pc.iceGatheringState != "complete" and (time.monotonic() - start) < 2.0:
                await asyncio.sleep(0.1)
        except Exception as exc:
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

        self._peer_generation = peer_generation
        self._client_connected.clear()
        self._peer_closed.clear()
        self._outbound_track = None

        # Close any existing peer connection only after the replacement SDP is
        # proven valid. Advancing the generation before teardown keeps late
        # callbacks from the previous peer from ending the receive_audio()
        # iterator for the replacement peer. Cancel the *old* peer's consume
        # task here; the new task is created only at swap time below so this
        # block can never cancel it.
        if self._consume_task is not None and not self._consume_task.done():
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
        self._consume_task = None

        if self._pc is not None:
            await self._pc.close()

        # Clear stale audio from the previous peer so it doesn't leak into
        # the new session's receive_audio() iterator. Do not replace the queue:
        # Session.receive_audio() may already be blocked on this object.
        self._drain_audio_queue()

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
        from easycat._audio_utils import resample, to_mono

        target_rate = self._config.audio_format.sample_rate
        target_format = self._config.audio_format

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

                # Resample to pipeline target rate.
                if frame_rate != target_rate:
                    raw = resample(raw, frame_rate, target_rate)

                chunk = AudioChunk(data=raw, format=target_format)
                if self._is_current_peer_generation(peer_generation):
                    self._enqueue_chunk(chunk, context="WebRTC")

        except StopAsyncIteration:
            logger.info("WebRTC audio track stream ended")
        except Exception as exc:
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
