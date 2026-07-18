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
import fractions
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from easycat._extras import require_module
from easycat._net import is_loopback_host, normalize_auth_token
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import EventBus, TransportAudioDelivered
from easycat.transports._base import AudioQueueMixin, make_version_info

if TYPE_CHECKING:
    from easycat.server._webrtc_handlers import WebRTCSignalingHandlers
    from easycat.server.auth import AuthPolicy

logger = logging.getLogger(__name__)

_WEBRTC_SAMPLE_RATE = 48000  # Opus standard
_FRAME_DURATION_MS = 20
_FRAME_SAMPLES = (_WEBRTC_SAMPLE_RATE * _FRAME_DURATION_MS) // 1000  # 960

# WebRTC-specific ``TransportDegraded.reason`` codes emitted on the session
# event bus (via the inherited ``AudioQueueMixin._emit_degraded``).  These
# mirror conditions that previously only reached ``logger.warning``; emitting
# them keeps the journal the single source of truth for observability.  The
# cross-transport ``inbound_queue_full`` code is emitted by ``_enqueue_chunk``
# in ``_base`` and needs no wiring here.  ``outbound_queue_full`` mirrors
# WebTransport: emitted from ``send_audio`` when the outbound TTS queue is full.
_DEGRADED_NEGOTIATION_FAILED = "negotiation_failed"
_DEGRADED_INBOUND_CONSUME_ERROR = "inbound_consume_error"
_DEGRADED_OUTBOUND_QUEUE_FULL = "outbound_queue_full"

_CORS_ALLOW_METHODS = "POST, GET, OPTIONS"
_CORS_ALLOW_HEADERS = "Content-Type, Authorization"

# Label of the browser-created data channel that carries session events
# (transcripts, interruptions, per-turn latency) to the playground page.
_EVENTS_CHANNEL_LABEL = "events"

_WEBRTC_STATS_TOP_LEVEL_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "sample_id",
        "sequence",
        "label",
        "captured_at",
        "connection_state",
        "ice_connection_state",
        "ice_gathering_state",
        "signaling_state",
    }
)
_WEBRTC_STATS_NESTED_FIELDS: dict[str, frozenset[str]] = {
    "candidate_pair": frozenset(
        {
            "available_incoming_bitrate",
            "available_outgoing_bitrate",
            "bytes_received",
            "bytes_sent",
            "consent_requests_sent",
            "current_round_trip_time_ms",
            "nominated",
            "packets_received",
            "packets_sent",
            "requests_received",
            "requests_sent",
            "responses_received",
            "responses_sent",
            "state",
        }
    ),
    "inbound_audio": frozenset(
        {
            "bytes_received",
            "concealed_samples",
            "concealment_events",
            "jitter_buffer_delay_ms",
            "jitter_ms",
            "packets_lost",
            "packets_received",
            "total_samples_received",
        }
    ),
    "outbound_audio": frozenset(
        {
            "bytes_sent",
            "packets_sent",
            "retransmitted_bytes_sent",
            "retransmitted_packets_sent",
            "target_bitrate",
            "total_packet_send_delay_ms",
        }
    ),
}


def _default_webrtc_stats_path() -> str | None:
    return os.environ.get("EASYCAT_WEBRTC_STATS_PATH") or None


def _safe_stats_scalar(value: object) -> object | None:
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ")[:200]
    return None


def _sanitize_webrtc_stats_snapshot(payload: object) -> dict[str, object]:
    """Keep only non-identifying browser WebRTC stats fields.

    Raw ``RTCPeerConnection.getStats()`` reports can include local/remote
    candidate addresses and implementation-specific IDs. The bundled browser
    client already summarizes safe fields, and this server-side filter keeps
    the validation artifact constrained even if a custom client posts more.
    """
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")

    snapshot: dict[str, object] = {}
    for field_name in _WEBRTC_STATS_TOP_LEVEL_FIELDS:
        safe_value = _safe_stats_scalar(payload.get(field_name))
        if safe_value is not None:
            snapshot[field_name] = safe_value

    for group_name, allowed_fields in _WEBRTC_STATS_NESTED_FIELDS.items():
        group = payload.get(group_name)
        if not isinstance(group, dict):
            continue
        safe_group: dict[str, object] = {}
        for field_name in allowed_fields:
            safe_value = _safe_stats_scalar(group.get(field_name))
            if safe_value is not None:
                safe_group[field_name] = safe_value
        if safe_group:
            snapshot[group_name] = safe_group

    snapshot.setdefault("kind", "webrtc_client_stats")
    snapshot.setdefault("schema_version", 1)
    return snapshot


def _append_webrtc_stats_record(stats_path: Path, snapshot: dict[str, object]) -> None:
    """Append one sanitized stats record to ``stats_path`` (blocking file I/O).

    Runs off the event loop via ``asyncio.to_thread`` from the stats handlers.
    """
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, sort_keys=True) + "\n")


@dataclass
class WebRTCStatsState:
    """Per-server stats rate-limit window + record counter.

    Owned by each signaling surface (the singleton :class:`WebRTCTransport`, or
    one per multi-session ``WebRTCRoutes`` unit) and passed to the shared
    :class:`easycat.server._webrtc_handlers.WebRTCSignalingHandlers` so the
    rate-limit / quota semantics stay per-server, matching the pre-convergence
    behavior exactly. Defined here (not in the server package) so the transport
    can own it without importing ``easycat.server`` at module load.
    """

    request_times: deque[float] = field(default_factory=deque)
    record_count: int | None = None
    # Serializes quota check + append + counter update in ``handle_stats``:
    # the append is offloaded to a thread, and that await would otherwise let
    # concurrent posts observe the same pre-write counters and exceed the
    # configured record / byte / rate quotas.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _audio_frame_pcm16_bytes(frame: Any) -> tuple[bytes, int, int]:
    """Extract valid interleaved PCM16 bytes from an ``av.AudioFrame``.

    ``bytes(frame.planes[0])`` can include PyAV line padding.  For decoded
    aiortc frames that padding can be several times larger than the actual
    samples, which makes the downstream pipeline see too much audio per RTP
    frame.  Slice by frame metadata instead of ``to_ndarray()`` so
    ``easycat[webrtc]`` does not need a NumPy dependency.
    """
    frame_rate = int(getattr(frame, "sample_rate", None) or _WEBRTC_SAMPLE_RATE)
    layout = getattr(frame, "layout", None)
    channels = len(getattr(layout, "channels", ()) or ()) or 1
    frame_format = getattr(frame, "format", None)
    sample_width = int(getattr(frame_format, "bytes", 2) or 2)
    samples = int(getattr(frame, "samples", 0) or 0)
    planes = list(getattr(frame, "planes", ()))
    if not planes:
        return b"", frame_rate, channels

    is_planar = bool(getattr(frame_format, "is_planar", False))
    if is_planar and channels > 1 and len(planes) >= channels and samples > 0:
        raw = _interleave_audio_planes(
            planes,
            samples=samples,
            channels=channels,
            sample_width=sample_width,
        )
    else:
        raw = bytes(planes[0])
        valid_bytes = samples * channels * sample_width
        if valid_bytes > 0:
            raw = raw[:valid_bytes]
    return raw, frame_rate, channels


def _interleave_audio_planes(
    planes: list[Any],
    *,
    samples: int,
    channels: int,
    sample_width: int,
) -> bytes:
    """Return interleaved bytes for planar PCM frames."""
    plane_bytes = []
    valid_plane_bytes = samples * sample_width
    for plane in planes[:channels]:
        data = bytes(plane)[:valid_plane_bytes]
        if len(data) < valid_plane_bytes:
            data += bytes(valid_plane_bytes - len(data))
        plane_bytes.append(data)

    interleaved = bytearray(samples * channels * sample_width)
    offset = 0
    for sample in range(samples):
        start = sample * sample_width
        end = start + sample_width
        for channel in plane_bytes:
            interleaved[offset : offset + sample_width] = channel[start:end]
            offset += sample_width
    return bytes(interleaved)


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
    expose_ice_credentials:
        Include ICE ``username`` and ``credential`` fields in the public
        ``/config`` response.  Leave this disabled for long-lived TURN
        credentials; enable it only for trusted/internal demos, authenticated
        config endpoints, or short-lived TURN credentials.
    cors_allowed_origins:
        Cross-origin browser origins allowed to call the signaling API.  The
        bundled browser client is same-origin and needs no CORS opt-in.  Use
        exact origins such as ``"https://voice.example.com"`` for custom
        hosted clients, or ``"*"`` only for controlled demos.
    stats_path:
        Optional JSONL file where browser clients can POST sanitized
        ``RTCPeerConnection.getStats()`` snapshots via ``/stats``.  Defaults to
        ``EASYCAT_WEBRTC_STATS_PATH`` when set so validation runs can advertise
        the artifact path without custom app wiring.
    auth_token:
        Optional shared secret required by ``/config``, ``/offer``, and
        ``/stats``.  Clients present it as ``Authorization: Bearer <token>``.
        A ``?token=`` query parameter is accepted only when
        ``allow_query_token=True`` (default off).  The bundled WebRTC client
        sends the ``Authorization`` header, so it is UNAFFECTED.  Mirrors the
        WebSocket/docker ``EASYCAT_WS_TOKEN`` security default — pair it with
        a non-loopback ``host``.
    allow_query_token:
        Whether a ``?token=`` query parameter is accepted in addition to the
        ``Authorization: Bearer`` header.  Default ``False`` (the secure
        posture): query tokens are a browser/dev opt-in only.
    max_sessions:
        Maximum concurrent browser offers accepted by
        :func:`easycat.server.serve_webrtc_config_sessions`. The single-client
        :class:`WebRTCTransport` compatibility wrapper ignores this value.
    stats_max_records:
        Maximum JSONL records written to ``stats_path`` by this process.
    stats_max_file_bytes:
        Maximum size of the stats artifact before new snapshots are rejected.
    stats_max_requests_per_minute:
        In-memory rate limit for accepted ``/stats`` snapshots.
    """

    _BUNDLED_STATIC_DIR: ClassVar[str] = str(Path(__file__).parent / "static")
    _USE_BUNDLED: ClassVar[str] = "__USE_BUNDLED__"

    host: str = "127.0.0.1"
    port: int = 8080
    ice_servers: list[ICEServer] = field(
        default_factory=lambda: [ICEServer(urls="stun:stun.l.google.com:19302")]
    )
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    static_dir: str | None = _USE_BUNDLED
    expose_ice_credentials: bool = False
    cors_allowed_origins: tuple[str, ...] = ()
    stats_path: str | None = field(default_factory=_default_webrtc_stats_path)
    auth_token: str | None = None
    allow_query_token: bool = False
    max_sessions: int = 64
    stats_max_records: int = 1_000
    stats_max_file_bytes: int = 1_048_576
    stats_max_requests_per_minute: int = 120


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _sanitize_webrtc_base(raw: str) -> str:
    """Return a safe same-origin path prefix from an untrusted ``?webrtc=`` value.

    The bundled client prepends ``?webrtc=<prefix>`` to its credentialed
    ``/offer`` / ``/stats`` POSTs, so an attacker-supplied value could
    misdirect them. Accept ONLY a clean same-origin absolute path prefix
    (allowlist): it must start with ``/`` and contain no scheme/host,
    backslash, query/fragment marker, protocol-relative or empty ``//``
    segment, or ``..`` traversal segment. Anything else collapses to ``""``
    (flat mode). Mirrors the ``safeWebRTCBase`` allowlist in
    ``static/webrtc_client.html`` so the server and client agree.
    """
    if not raw or not raw.startswith("/"):
        return ""
    if "//" in raw or "\\" in raw or ":" in raw or "?" in raw or "#" in raw:
        return ""
    if ".." in raw.split("/"):
        return ""
    return raw.rstrip("/")


def webrtc_ice_servers_from_env(
    *,
    turn_url_env: str = "TURN_SERVER_URL",
    turn_username_env: str = "TURN_USERNAME",
    turn_credential_env: str = "TURN_CREDENTIAL",
    include_public_stun: bool = True,
) -> list[ICEServer]:
    """Build STUN/TURN servers from the standard WebRTC demo environment."""
    servers: list[ICEServer] = []
    if include_public_stun:
        servers.append(ICEServer(urls="stun:stun.l.google.com:19302"))

    turn_url = os.getenv(turn_url_env, "").strip()
    if turn_url:
        servers.append(
            ICEServer(
                urls=turn_url,
                username=os.getenv(turn_username_env, ""),
                credential=os.getenv(turn_credential_env, ""),
            )
        )
    return servers


def webrtc_transport_config_from_env(
    *,
    host_env: str = "SIGNALING_HOST",
    port_env: str = "SIGNALING_PORT",
    auth_token_env: str = "WEBRTC_SIGNALING_TOKEN",
    max_sessions_env: str = "WEBRTC_MAX_SESSIONS",
    expose_ice_credentials_env: str = "WEBRTC_EXPOSE_ICE_CREDENTIALS",
    static_dir: str | None = WebRTCTransportConfig._USE_BUNDLED,
) -> WebRTCTransportConfig:
    """Build a browser WebRTC transport config from example/deployment env vars."""
    return WebRTCTransportConfig(
        host=os.getenv(host_env, "127.0.0.1"),
        port=int(os.getenv(port_env, "8080")),
        ice_servers=webrtc_ice_servers_from_env(),
        static_dir=static_dir,
        auth_token=normalize_auth_token(os.getenv(auth_token_env)),
        max_sessions=int(os.getenv(max_sessions_env, "64")),
        expose_ice_credentials=_env_flag(expose_ice_credentials_env),
    )


# ── Outbound audio track ─────────────────────────────────────────


@dataclass
class _QueuedOutboundChunk:
    transport_data: bytes
    original_chunk: AudioChunk
    session_id: str | None = None
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

    # Maximum number of AEC far-end reference frames buffered between mic
    # frames.  ``_recv`` runs on the event loop (no thread crossing), so the
    # ``deque(maxlen=...)`` drops the OLDEST entry on overflow for free, which
    # keeps the freshest reference available for cancellation.
    _AEC_REF_QUEUE_MAX: ClassVar[int] = 100

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_QueuedOutboundChunk] = asyncio.Queue(maxsize=100)
        self._pending: deque[_QueuedOutboundChunk] = deque()
        self._pts = 0
        self._start: float | None = None
        self._event_bus: EventBus | None = None
        # Deferred bus.emit work (TransportAudioDelivered).  Observability must
        # never block the RTP pacing hot path, so ``_recv`` enqueues events and
        # a single worker task drains them FIFO — off-loop like the previous
        # per-chunk fire-and-forget tasks, but delivery-ordered (a subscriber
        # that suspends mid-handler can no longer observe chunk N+1 before
        # chunk N) and one task instead of ~50/sec.  The worker is tracked in
        # ``_emit_tasks`` so it is not GC'd mid-flight and ``aclose`` drains it
        # (mirrors LocalTransport / AudioQueueMixin._emit_tasks).
        self._emit_queue: deque[TransportAudioDelivered] = deque()
        self._emit_worker: asyncio.Task[None] | None = None
        self._emit_tasks: set[asyncio.Task[None]] = set()
        # Cache the av.AudioFrame class to avoid per-frame import overhead.
        self._AudioFrame: type | None = None
        # AEC far-end reference drain queue.  ``_recv`` appends each delivered
        # (session-rate) chunk at playback time; AudioRouter drains it via
        # ``drain_aec_reference_frames`` before AudioStage.execute() so the AEC
        # far-end reference is always fed before the corresponding near-end mic
        # frame is processed.  Fully-silent render frames append a session-rate
        # silence frame too, so the far/near streams stay 1:1 during pauses.
        self._aec_ref_queue: deque[bytes] = deque(maxlen=self._AEC_REF_QUEUE_MAX)
        # Session-rate format of the most recently delivered far-end frame, used
        # to size silence reference frames during fully-silent ``_recv`` calls.
        # ``None`` until audio has played (no echo to cancel before then).
        self._ref_format: AudioFormat | None = None
        # Reference capture is armed only once a consumer (the AudioRouter)
        # first drains via ``drain_aec_reference_frames()``.  Until then ``_recv``
        # skips appending references entirely, so a session without AEC does no
        # per-frame reference allocation or deque churn.
        self._aec_reference_enabled: bool = False

    def create_track(self) -> Any:
        """Return an aiortc MediaStreamTrack wrapping this source."""
        transport_src = self
        aiortc: Any = require_module("aiortc", extra="webrtc", purpose="WebRTC transport")

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
        session_id: str | None = None,
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
                    session_id=session_id,
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
        delivered_chunks: list[tuple[AudioChunk, str | None, str | None, object | None]] = []

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
                delivered_data = queued.original_chunk.data[queued.original_reported : reported]
                delivered_chunks.append(
                    (
                        AudioChunk(
                            data=delivered_data,
                            format=queued.original_chunk.format,
                            timestamp=queued.original_chunk.timestamp,
                        ),
                        queued.session_id,
                        queued.turn_id,
                        queued.turn_ref,
                    )
                )
                # Capture the far-end reference at playback time in the same
                # session-rate format/order previously fed via the router's
                # _handle_audio_delivery path.  Drained before the near-end
                # frame by AudioRouter (shared AEC reference capability).
                #
                # KNOWN LIMITATION (server-side AEC is best-effort): this
                # captures the reference at the server's RTP *send* time, not at
                # the remote peer's actual speaker playout.  For a remote WebRTC
                # peer the true echo path adds the peer's jitter buffer, speaker,
                # room acoustics, and return-network latency, so this reference
                # may not align with the echo arriving back at the near end and
                # server-side AEC may not converge.  It is primarily effective
                # for local-mic / co-located setups where send time ≈ playout
                # time; remote-peer echo alignment is not guaranteed.  Most
                # browsers already run their own near-end AEC, so this is a
                # best-effort supplement rather than a guarantee.
                if delivered_data and self._aec_reference_enabled:
                    self._aec_ref_queue.append(delivered_data)
                    self._ref_format = queued.original_chunk.format
                queued.original_reported = reported

            if queued.transport_offset >= len(queued.transport_data):
                self._pending.popleft()

        if len(buf) < frame_bytes:
            # Pad with silence.
            buf.extend(bytes(frame_bytes - len(buf)))

        pcm_data = bytes(buf)

        # Silence-frame alignment: a render frame that carried no real audio
        # (queue empty) still played 20 ms of silence into the speaker, so
        # append a matching session-rate silence reference.  This keeps the
        # far-end stream 1:1 with the near-end mic stream during pauses,
        # mirroring LocalTransport's per-callback reference.  Skipped until a
        # session rate is known (nothing has played yet -> no echo to cancel).
        if self._aec_reference_enabled and not delivered_chunks and self._ref_format is not None:
            fmt = self._ref_format
            silence_samples = fmt.sample_rate * _FRAME_SAMPLES // _WEBRTC_SAMPLE_RATE
            self._aec_ref_queue.append(bytes(silence_samples * fmt.frame_size))

        frame = self._AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
        frame.sample_rate = _WEBRTC_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _WEBRTC_SAMPLE_RATE)
        frame.planes[0].update(pcm_data)

        self._pts += _FRAME_SAMPLES
        if self._event_bus is not None:
            for delivered_chunk, session_id, turn_id, turn_ref in delivered_chunks:
                if delivered_chunk.data:
                    self._emit_queue.append(
                        TransportAudioDelivered(
                            chunk=delivered_chunk,
                            session_id=session_id,
                            turn_id=turn_id,
                            turn_ref=turn_ref,
                        )
                    )
            if self._emit_queue and (self._emit_worker is None or self._emit_worker.done()):
                worker = asyncio.create_task(self._drain_emit_queue())
                self._emit_worker = worker
                self._emit_tasks.add(worker)
                worker.add_done_callback(self._emit_tasks.discard)
        return frame

    async def _drain_emit_queue(self) -> None:
        """Emit queued ``TransportAudioDelivered`` events in delivery order."""
        assert self._event_bus is not None
        while self._emit_queue:
            event = self._emit_queue.popleft()
            try:
                await self._event_bus.emit(event)
            except Exception:
                logger.exception("TransportAudioDelivered emit failed")

    def drain_aec_reference_frames(self) -> list[bytes]:
        """Return all pending AEC far-end reference frames, draining the queue.

        Each element is session-rate PCM16 captured at playback time, oldest
        first.  The LiveKitAEC reframes internally, so variable-size elements
        are fine.

        Calling this also *arms* reference capture: ``_recv`` only buffers
        far-end frames once a consumer has started draining, so a session
        without AEC never pays the per-frame reference cost.
        """
        self._aec_reference_enabled = True
        frames = list(self._aec_ref_queue)
        self._aec_ref_queue.clear()
        return frames

    def clear(self) -> None:
        """Discard all queued audio data (used for barge-in / interruption).

        The AEC reference deque is intentionally *not* cleared: residual echo
        of already-played audio is still arriving at the mic, so those refs
        must remain available for cancellation.  The deque is bounded, so it
        cannot grow without limit.
        """
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending.clear()

    def stop(self) -> None:
        """Signal that no more data will be enqueued.

        No-op: the track is discarded along with the peer connection on
        disconnect, so there is nothing to clean up here.  In-flight
        observability emits are drained separately via :meth:`aclose`.
        """

    async def aclose(self) -> None:
        """Await any in-flight ``TransportAudioDelivered`` emit tasks.

        ``_recv`` schedules these off the RTP pacing hot path (fire-and-forget
        so observability never blocks pacing), tracking them in
        ``self._emit_tasks``.  Teardown must drain that set so pending delivery
        emits are awaited rather than cancelled-and-lost at loop teardown
        ("Task was destroyed but it is pending").  Mirrors
        ``AudioQueueMixin._drain_emit_tasks`` / ``LocalTransport.stop()``.
        """
        if not self._emit_tasks:
            return
        # Snapshot: the done-callback mutates ``_emit_tasks`` during gather.
        await asyncio.gather(*list(self._emit_tasks), return_exceptions=True)
        self._emit_tasks.clear()


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
        self._outbound: _OutboundAudioSource = _OutboundAudioSource()
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
        if chunk.format.sample_rate != _WEBRTC_SAMPLE_RATE:
            pcm_data = resample(chunk.data, chunk.format.sample_rate, _WEBRTC_SAMPLE_RATE)
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
            outbound = _OutboundAudioSource()
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
                    # ``disconnected``/``failed`` are abnormal peer drops (ICE
                    # loss, connectivity failure); ``closed`` is the terminal
                    # state of an application-initiated teardown and is clean.
                    if state in ("disconnected", "failed"):
                        self._record_transport_disconnect(f"webrtc peer {state}")
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
                raw, frame_rate, channels = _audio_frame_pcm16_bytes(frame)

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
