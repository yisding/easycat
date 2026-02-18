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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.extras import require_module
from easycat.transports._base import _AudioQueueMixin

logger = logging.getLogger(__name__)

_WEBRTC_SAMPLE_RATE = 48000  # Opus standard
_FRAME_DURATION_MS = 20
_FRAME_SAMPLES = (_WEBRTC_SAMPLE_RATE * _FRAME_DURATION_MS) // 1000  # 960

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
        Optional directory to serve static files from (e.g. the HTML client).
        When set, static files are served from the same HTTP server as the
        signaling endpoint, eliminating the need for a separate file server.
    """

    host: str = "0.0.0.0"
    port: int = 8080
    ice_servers: list[ICEServer] = field(
        default_factory=lambda: [ICEServer(urls="stun:stun.l.google.com:19302")]
    )
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    static_dir: str | None = None


# ── Outbound audio track ─────────────────────────────────────────


class _OutboundAudioSource:
    """Custom audio track that reads PCM16 data from a queue.

    Produces 20 ms Opus-compatible frames at 48 kHz.  When the queue is
    empty, silence frames are emitted so the RTP stream stays alive.
    """

    kind = "audio"

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._remainder = b""
        self._pts = 0
        self._start: float | None = None
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

    def enqueue(self, pcm_s16_48k: bytes) -> None:
        """Enqueue a chunk of 48 kHz PCM16 mono data for sending."""
        try:
            self._queue.put_nowait(pcm_s16_48k)
        except asyncio.QueueFull:
            logger.debug("Outbound WebRTC audio queue full — dropping frame")

    async def _recv(self) -> Any:
        """Produce the next 20 ms audio frame for aiortc."""
        if self._AudioFrame is None:
            av = require_module("av", extra="webrtc", purpose="WebRTC audio frames")
            self._AudioFrame = av.AudioFrame

        if self._start is None:
            self._start = time.time()

        # Pace frames to real-time so RTP timing is correct.
        expected = self._start + (self._pts / _WEBRTC_SAMPLE_RATE)
        wait = expected - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

        frame_bytes = _FRAME_SAMPLES * 2  # 16-bit mono

        # Start with any leftover data from the previous frame to preserve
        # audio ordering (instead of putting remainders back on the queue).
        buf = bytearray(self._remainder)
        self._remainder = b""

        while len(buf) < frame_bytes:
            try:
                chunk = self._queue.get_nowait()
                buf.extend(chunk)
            except asyncio.QueueEmpty:
                break

        if len(buf) < frame_bytes:
            # Pad with silence.
            buf.extend(bytes(frame_bytes - len(buf)))

        pcm_data = bytes(buf[:frame_bytes])
        self._remainder = bytes(buf[frame_bytes:])

        frame = self._AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
        frame.sample_rate = _WEBRTC_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _WEBRTC_SAMPLE_RATE)
        frame.planes[0].update(pcm_data)

        self._pts += _FRAME_SAMPLES
        return frame

    def clear(self) -> None:
        """Discard all queued audio data (used for barge-in / interruption)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._remainder = b""

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

    _transport_name = "WebRTC"

    def __init__(self, config: WebRTCTransportConfig | None = None) -> None:
        self._config = config or WebRTCTransportConfig()
        self._init_audio_queue(self._config.max_pending_chunks)

        # Peer connection state.
        self._pc: Any | None = None
        self._outbound: _OutboundAudioSource = _OutboundAudioSource()
        self._outbound_track: Any | None = None

        # HTTP signaling server (aiohttp).
        self._web: Any | None = None  # cached aiohttp.web module
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._has_bundled_client = False

        # Background task that consumes the inbound audio track.
        self._consume_task: asyncio.Task[None] | None = None

        self._client_connected = asyncio.Event()

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

        # Serve static files if a directory was configured.
        if self._config.static_dir is not None:
            static_path = Path(self._config.static_dir)
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

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the remote WebRTC peer."""
        if self._pc is None or self._outbound_track is None:
            return

        from easycat.audio_utils import resample

        # Resample to 48 kHz for Opus encoding.
        if chunk.format.sample_rate != _WEBRTC_SAMPLE_RATE:
            pcm_data = resample(chunk.data, chunk.format.sample_rate, _WEBRTC_SAMPLE_RATE)
        else:
            pcm_data = chunk.data

        self._outbound.enqueue(pcm_data)

    async def clear_audio(self) -> None:
        """Discard queued outbound audio (useful during barge-in)."""
        self._outbound.clear()

    # ── Signaling handlers ────────────────────────────────────────

    async def _handle_offer(self, request: Any) -> Any:
        """Handle an SDP offer from the browser client."""
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

        # Close any existing peer connection.
        if self._pc is not None:
            if self._consume_task is not None and not self._consume_task.done():
                self._consume_task.cancel()
                try:
                    await self._consume_task
                except asyncio.CancelledError:
                    pass
                self._consume_task = None
            await self._pc.close()
            self._pc = None

        # Clear stale audio from the previous peer so it doesn't leak into
        # the new session's receive_audio() iterator.
        self._reset_audio_queue()

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
                if track.kind == "audio":
                    logger.info("WebRTC remote audio track received")
                    self._consume_task = asyncio.ensure_future(self._consume_audio(track))

                    @track.on("ended")
                    async def on_ended() -> None:
                        logger.info("WebRTC remote audio track ended")
                        self._enqueue_sentinel()

            @pc.on("connectionstatechange")
            async def on_connectionstatechange() -> None:
                state = pc.connectionState
                logger.info("WebRTC connection state: %s", state)
                if state == "connected":
                    self._client_connected.set()
                elif state in ("disconnected", "failed", "closed"):
                    self._client_connected.clear()
                    self._enqueue_sentinel()

            # Set remote offer and create answer.
            offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
            await pc.setRemoteDescription(offer)

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception as exc:
            logger.warning("WebRTC offer handling failed: %s", exc)
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

    async def _consume_audio(self, track: Any) -> None:
        """Read audio frames from the remote track and enqueue as AudioChunk."""
        from easycat.audio_utils import resample, to_mono

        target_rate = self._config.audio_format.sample_rate
        target_format = self._config.audio_format

        logger.info("Consuming WebRTC audio track (target %d Hz)", target_rate)

        try:
            while True:
                frame = await track.recv()

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

    # ── Properties ────────────────────────────────────────────────

    @property
    def has_client(self) -> bool:
        return self._pc is not None and self._pc.connectionState == "connected"

    async def wait_for_client(self, timeout: float | None = None) -> None:
        """Block until a WebRTC peer connects (or timeout expires)."""
        await asyncio.wait_for(self._client_connected.wait(), timeout=timeout)
