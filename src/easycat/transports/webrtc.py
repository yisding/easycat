"""WebRTC transport: real peer-to-peer audio via aiortc.

Hosts an HTTP signaling server (aiohttp) on a configurable port.  Clients
POST an SDP offer to ``/offer`` and receive an SDP answer.  Audio is
exchanged over the WebRTC peer connection using the Opus codec.

Inbound audio (remote peer → pipeline) is decoded from Opus at 48 kHz and
resampled to the pipeline's target rate (default 16 kHz PCM16 mono).

Outbound audio (pipeline → remote peer) is resampled from whatever the TTS
provider emits to 48 kHz and sent via an Opus-encoded audio track.

Requires the ``webrtc`` extra::

    uv add easycat[webrtc]
"""

from __future__ import annotations

import asyncio
import fractions
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import EventBus, TransportAudioDelivered
from easycat.extras import require_module
from easycat.transports._base import _AudioQueueMixin

logger = logging.getLogger(__name__)

_WEBRTC_SAMPLE_RATE = 48000  # Opus standard
_FRAME_DURATION_MS = 20
_FRAME_SAMPLES = (_WEBRTC_SAMPLE_RATE * _FRAME_DURATION_MS) // 1000  # 960

# WebRTC-specific ``TransportDegraded.reason`` codes emitted on the session
# event bus (via the inherited ``_AudioQueueMixin._emit_degraded``).  These
# mirror conditions that previously only reached ``logger.warning``; emitting
# them keeps the journal the single source of truth for observability.  The
# cross-transport ``inbound_queue_full`` code is emitted by ``_enqueue_chunk``
# in ``_base`` and needs no wiring here.
_DEGRADED_NEGOTIATION_FAILED = "negotiation_failed"
_DEGRADED_INBOUND_CONSUME_ERROR = "inbound_consume_error"

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


# ── Configuration ────────────────────────────────────────────────


@dataclass
class ICEServer:
    """STUN or TURN server descriptor."""

    urls: str | list[str]
    username: str | None = None
    credential: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.urls, str):
            self.urls = [self.urls]


@dataclass
class WebRTCTransportConfig:
    """Configuration for :class:`WebRTCTransport`.

    Parameters
    ----------
    host:
        Bind address for the HTTP signaling server.
    port:
        Listen port for the HTTP signaling server.
    ice_servers:
        STUN/TURN servers for ICE negotiation.  Defaults to Google's public
        STUN server which works when both peers are on the public internet.
        For NAT traversal add a TURN server (e.g. coturn).
    audio_format:
        Target audio format for the pipeline side (default 16 kHz PCM16 mono).
    max_pending_chunks:
        Maximum number of inbound audio chunks to buffer before dropping.
    static_dir:
        Directory to serve static files from (e.g. the HTML client).  When set,
        static files are served from the same HTTP server as the signaling
        endpoint, eliminating the need for a separate file server.

        Defaults to a bundled demo client shipped with the package.  Set to
        ``None`` to disable static file serving entirely.
    """

    _BUNDLED_STATIC_DIR: ClassVar[str] = str(Path(__file__).parent / "static")
    _USE_BUNDLED: ClassVar[str] = "__USE_BUNDLED__"

    host: str = "0.0.0.0"
    port: int = 8080
    ice_servers: list[ICEServer] = field(
        default_factory=lambda: [ICEServer(urls="stun:stun.l.google.com:19302")]
    )
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    static_dir: str | None = _USE_BUNDLED


# ── Outbound audio track ─────────────────────────────────────────


@dataclass
class _QueuedOutboundChunk:
    transport_data: bytes
    original_chunk: AudioChunk
    turn_id: str | None = None
    turn_ref: object | None = None
    transport_offset: int = 0
    original_reported: int = 0


class _OutboundAudioSource:
    """Custom audio source that reads PCM16 data from a queue.

    Produces 20 ms Opus-compatible frames at 48 kHz.  When the queue is
    empty, silence frames are emitted so the RTP stream stays alive.

    This is *not* a ``MediaStreamTrack`` itself — call :meth:`create_track`
    to obtain an aiortc track that delegates ``recv()`` back to this source.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_QueuedOutboundChunk] = asyncio.Queue(maxsize=100)
        self._pending: deque[_QueuedOutboundChunk] = deque()
        self._pts = 0
        self._start: float | None = None
        self._event_bus: EventBus | None = None
        # Cache the av.AudioFrame class to avoid per-frame import overhead.
        self._AudioFrame: type | None = None

    def create_track(self) -> Any:
        """Return an aiortc MediaStreamTrack wrapping this source."""
        transport_src = self
        aiortc = require_module("aiortc", extra="webrtc", purpose="WebRTC transport")

        class _Track(aiortc.MediaStreamTrack):
            kind = "audio"

            async def recv(self_track) -> Any:  # noqa: N805
                return await transport_src._recv()

        return _Track()

    def enqueue(
        self,
        pcm_s16_48k: bytes,
        *,
        original_chunk: AudioChunk,
        turn_id: str | None = None,
        turn_ref: object | None = None,
    ) -> bool:
        """Enqueue a chunk of 48 kHz PCM16 mono data for sending.

        Returns ``True`` when the chunk was accepted and ``False`` when
        the outbound queue was full and the frame was dropped.
        """
        if not pcm_s16_48k:
            return True
        try:
            self._queue.put_nowait(
                _QueuedOutboundChunk(
                    transport_data=pcm_s16_48k,
                    original_chunk=original_chunk,
                    turn_id=turn_id,
                    turn_ref=turn_ref,
                )
            )
        except asyncio.QueueFull:
            logger.debug("Outbound WebRTC audio queue full — dropping frame")
            return False
        return True

    async def _recv(self) -> Any:
        """Produce the next 20 ms audio frame for aiortc."""
        if self._AudioFrame is None:
            av = require_module("av", extra="webrtc", purpose="WebRTC audio frames")
            self._AudioFrame = av.AudioFrame

        if self._start is None:
            self._start = time.monotonic()

        # Pace frames to real-time so RTP timing is correct.
        # Use monotonic clock so pacing is not affected by wall-clock jumps.
        expected = self._start + (self._pts / _WEBRTC_SAMPLE_RATE)
        wait = expected - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        frame_bytes = _FRAME_SAMPLES * 2  # 16-bit mono

        buf = bytearray()
        delivered_chunks: list[tuple[AudioChunk, str | None, object | None]] = []

        while len(buf) < frame_bytes:
            if not self._pending:
                try:
                    self._pending.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            queued = self._pending[0]
            remaining = queued.transport_data[queued.transport_offset :]
            if not remaining:
                self._pending.popleft()
                continue

            take = min(frame_bytes - len(buf), len(remaining))
            if take <= 0:
                break

            buf.extend(remaining[:take])
            queued.transport_offset += take

            original_size = len(queued.original_chunk.data)
            if queued.transport_offset >= len(queued.transport_data):
                reported = original_size
            else:
                reported = min(
                    original_size,
                    int((queued.transport_offset / len(queued.transport_data)) * original_size),
                )
            if reported > queued.original_reported:
                delivered_chunks.append(
                    (
                        AudioChunk(
                            data=queued.original_chunk.data[queued.original_reported : reported],
                            format=queued.original_chunk.format,
                            timestamp=queued.original_chunk.timestamp,
                        ),
                        queued.turn_id,
                        queued.turn_ref,
                    )
                )
                queued.original_reported = reported

            if queued.transport_offset >= len(queued.transport_data):
                self._pending.popleft()

        if len(buf) < frame_bytes:
            # Pad with silence.
            buf.extend(bytes(frame_bytes - len(buf)))

        pcm_data = bytes(buf)

        frame = self._AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
        frame.sample_rate = _WEBRTC_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _WEBRTC_SAMPLE_RATE)
        frame.planes[0].update(pcm_data)

        self._pts += _FRAME_SAMPLES
        if self._event_bus is not None:
            for delivered_chunk, turn_id, turn_ref in delivered_chunks:
                if delivered_chunk.data:
                    await self._event_bus.emit(
                        TransportAudioDelivered(
                            chunk=delivered_chunk,
                            turn_id=turn_id,
                            turn_ref=turn_ref,
                        )
                    )
        return frame

    def clear(self) -> None:
        """Discard all queued audio data (used for barge-in / interruption)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending.clear()

    def stop(self) -> None:
        """Signal that no more data will be enqueued.

        No-op: the track is discarded along with the peer connection on
        disconnect, so there is nothing to clean up here.
        """


# ── WebRTC Transport ─────────────────────────────────────────────


class WebRTCTransport(_AudioQueueMixin):
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

    **GET /config** — Returns the ICE server configuration as JSON so
    browser clients can configure their ``RTCPeerConnection`` with the
    same STUN/TURN servers.

    **GET /health** — Returns ``{"status": "ok"}``.
    """

    transport_kind = "webrtc"

    _transport_name = "WebRTC"
    reports_audio_delivery = True

    def __init__(self, config: WebRTCTransportConfig | None = None) -> None:
        self._config = config or WebRTCTransportConfig()
        self._init_audio_queue(self._config.max_pending_chunks)

        # Peer connection state.
        self._pc: Any | None = None
        self._outbound: _OutboundAudioSource = _OutboundAudioSource()
        self._outbound_track: Any | None = None
        # ``_event_bus`` / ``_emit_degraded`` come from ``_AudioQueueMixin``
        # (``_init_audio_queue`` above).  Session attaches the bus
        # post-construction; it is forwarded to ``_outbound`` (for
        # ``TransportAudioDelivered``) once a peer connects.

        # HTTP signaling server (aiohttp).
        self._web: Any | None = None  # cached aiohttp.web module
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._has_bundled_client = False

        # Background task that consumes the inbound audio track.
        self._consume_task: asyncio.Task[None] | None = None
        self._peer_generation = 0
        self._offer_lock = asyncio.Lock()

    # ── Helpers ─────────────────────────────────────────────────

    def _ice_servers_as_dicts(self) -> list[dict[str, Any]]:
        """Serialize configured ICE servers to plain dicts.

        Used by both the ``/offer`` handler (to build ``RTCIceServer``
        objects) and ``/config`` (to return JSON to the browser).
        """
        result: list[dict[str, Any]] = []
        for srv in self._config.ice_servers:
            entry: dict[str, Any] = {"urls": srv.urls}
            if srv.username:
                entry["username"] = srv.username
            if srv.credential:
                entry["credential"] = srv.credential
            result.append(entry)
        return result

    def _is_current_peer_generation(self, peer_generation: int | None) -> bool:
        return peer_generation is None or peer_generation == self._peer_generation

    def _enqueue_sentinel_for_peer(self, peer_generation: int | None) -> None:
        if self._is_current_peer_generation(peer_generation):
            self._enqueue_sentinel()

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the HTTP signaling server."""
        if self._connected:
            return

        self._web = require_module("aiohttp.web", extra="webrtc", purpose="WebRTC signaling")
        web = self._web

        self._reset_audio_queue()
        self._has_bundled_client = False

        app = web.Application()
        app.router.add_post("/offer", self._handle_offer)
        app.router.add_get("/config", self._handle_config)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/", self._handle_root)
        app.router.add_options("/offer", self._handle_cors_preflight)

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
        logger.info(
            "WebRTC signaling server listening on http://%s:%d",
            self._config.host,
            self._config.port,
        )

    async def disconnect(self) -> None:
        """Close the peer connection and stop the signaling server."""
        if not self._connected:
            return

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

        self._outbound.stop()  # no-op by design; track is discarded with the PC

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
        self._connected = False
        self._client_connected.clear()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send an audio chunk to the remote WebRTC peer."""
        if self._pc is None or self._outbound_track is None:
            return False

        from easycat.audio_utils import resample

        # Resample to 48 kHz for Opus encoding.
        if chunk.format.sample_rate != _WEBRTC_SAMPLE_RATE:
            pcm_data = resample(chunk.data, chunk.format.sample_rate, _WEBRTC_SAMPLE_RATE)
        else:
            pcm_data = chunk.data

        self._outbound._event_bus = self._event_bus
        return self._outbound.enqueue(
            pcm_data,
            original_chunk=chunk,
            turn_id=getattr(chunk, "_easycat_turn_id", None),
            turn_ref=getattr(chunk, "_easycat_turn_ref", None),
        )

    async def clear_audio(self) -> None:
        """Discard queued outbound audio (useful during barge-in)."""
        self._outbound.clear()

    # ── Signaling handlers ────────────────────────────────────────

    async def _handle_offer(self, request: Any) -> Any:
        """Handle an SDP offer from the browser client."""
        async with self._offer_lock:
            return await self._handle_offer_locked(request)

    async def _handle_offer_locked(self, request: Any) -> Any:
        """Handle an SDP offer with peer replacement serialized."""
        web = self._web
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
                headers=_CORS_HEADERS,
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
                headers=_CORS_HEADERS,
            )

        peer_generation = self._peer_generation + 1
        self._peer_generation = peer_generation
        self._client_connected.clear()
        self._outbound_track = None

        # Close any existing peer connection. Advancing the generation before
        # teardown keeps late callbacks from the previous peer from ending the
        # receive_audio() iterator for the replacement peer.
        if self._consume_task is not None and not self._consume_task.done():
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
        self._consume_task = None

        if self._pc is not None:
            await self._pc.close()
            self._pc = None

        # Clear stale audio from the previous peer so it doesn't leak into
        # the new session's receive_audio() iterator. Do not replace the queue:
        # Session.receive_audio() may already be blocked on this object.
        self._drain_audio_queue()

        # Build ICE configuration from the shared serializer.
        ice_servers = [RTCIceServer(**entry) for entry in self._ice_servers_as_dicts()]
        rtc_config = RTCConfiguration(iceServers=ice_servers)

        pc = None
        try:
            pc = RTCPeerConnection(rtc_config)
            self._pc = pc

            # Reset outbound track for the new connection.
            self._outbound = _OutboundAudioSource()
            self._outbound_track = self._outbound.create_track()
            pc.addTrack(self._outbound_track)

            # Listen for the remote audio track.
            @pc.on("track")
            def on_track(track: Any) -> None:
                if not self._is_current_peer_generation(peer_generation):
                    return
                if track.kind == "audio":
                    logger.info("WebRTC remote audio track received")
                    self._consume_task = asyncio.ensure_future(
                        self._consume_audio(track, peer_generation=peer_generation)
                    )

                    @track.on("ended")
                    async def on_ended() -> None:
                        if not self._is_current_peer_generation(peer_generation):
                            return
                        logger.info("WebRTC remote audio track ended")
                        self._enqueue_sentinel_for_peer(peer_generation)

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
                fatal=True,
            )
            if pc is not None:
                await pc.close()
            self._pc = None
            return web.Response(
                status=400,
                text=json.dumps({"error": f"SDP negotiation failed: {exc}"}),
                content_type="application/json",
                headers=_CORS_HEADERS,
            )

        return web.Response(
            content_type="application/json",
            text=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}),
            headers=_CORS_HEADERS,
        )

    async def _handle_config(self, request: Any) -> Any:
        """Return ICE server configuration for browser clients."""
        web = self._web
        return web.Response(
            content_type="application/json",
            text=json.dumps({"iceServers": self._ice_servers_as_dicts()}),
            headers=_CORS_HEADERS,
        )

    async def _handle_health(self, request: Any) -> Any:
        web = self._web
        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "ok"}),
            headers=_CORS_HEADERS,
        )

    async def _handle_root(self, request: Any) -> Any:
        """Return a friendly landing response for signaling server root.

        When the bundled demo client is served, redirect to it. Otherwise,
        return a small JSON payload describing available endpoints so first
        time users can immediately discover how to connect.
        """
        web = self._web
        if self._has_bundled_client:
            raise web.HTTPFound("/webrtc_client.html")
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {
                    "service": "easycat-webrtc-signaling",
                    "endpoints": ["/offer", "/config", "/health"],
                    "note": (
                        "Set WebRTCTransportConfig.static_dir to serve "
                        "the demo browser client from this server."
                    ),
                }
            ),
            headers=_CORS_HEADERS,
        )

    async def _handle_cors_preflight(self, request: Any) -> Any:
        web = self._web
        return web.Response(headers=_CORS_HEADERS)

    # ── Audio track consumer ──────────────────────────────────────

    async def _consume_audio(self, track: Any, *, peer_generation: int | None = None) -> None:
        """Read audio frames from the remote track and enqueue as AudioChunk.

        Always enqueues a sentinel on exit so that ``receive_audio()`` does not
        block indefinitely if the track ends without a connection-state callback.
        """
        from easycat.audio_utils import resample, to_mono

        target_rate = self._config.audio_format.sample_rate
        target_format = self._config.audio_format

        logger.info("Consuming WebRTC audio track (target %d Hz)", target_rate)

        try:
            while True:
                frame = await track.recv()
                if not self._is_current_peer_generation(peer_generation):
                    break

                # Extract raw PCM from the av.AudioFrame.
                # aiortc decodes Opus to s16 at 48 kHz by default.
                raw = bytes(frame.planes[0])
                frame_rate = frame.sample_rate or _WEBRTC_SAMPLE_RATE
                channels = len(frame.layout.channels) if frame.layout else 1

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

    # ── Properties ────────────────────────────────────────────────

    @property
    def has_client(self) -> bool:
        return self._pc is not None and self._pc.connectionState == "connected"

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            rtc_ver = version("aiortc")
        except Exception:
            rtc_ver = "unknown"
        return {
            "provider": "webrtc",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": rtc_ver,
        }
