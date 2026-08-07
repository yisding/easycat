"""WebTransport (HTTP/3 over QUIC) transport.

Sits between :class:`~easycat.transports.websocket.WebSocketTransport` and
:class:`~easycat.transports.webrtc.WebRTCTransport`:

* vs. WebSocket: no cross-stream head-of-line blocking, faster handshake,
  connection migration when the client's IP changes.
* vs. WebRTC: a much smaller browser API (no SDP/ICE) and we keep PCM16
  end-to-end.

Public entry points:

* :class:`WebTransportServer` — multi-client aioquic server.  Pass a
  ``session_handler`` coroutine and one is invoked per client with a
  fully-wired :class:`WebTransportConnectionTransport` ready to plug into
  ``create_session(...)``.  Use this lower-level form when you need custom
  per-client orchestration beyond the config helper.
* :class:`WebTransportTransport` — single-client server.  Implements the
  :class:`~easycat.providers.Transport` protocol directly, mirroring
  :class:`~easycat.transports.websocket.WebSocketTransport`'s ergonomics for
  one-shot deployments.  Under the hood it just spins up a
  :class:`WebTransportServer` with a one-session handler.
* :class:`WebTransportConnectionTransport` — per-session
  :class:`~easycat.providers.Transport`.  Yielded by the server to your
  handler; you normally don't construct this yourself.

Wire protocol
-------------
Each peer opens its **own** bidirectional QUIC streams; we never share a
stream's two halves between application directions.  The first byte on every
stream is a 1-byte tag that identifies its purpose:

``0x01`` — **audio stream**

  * server→client: ``[1-byte 0x01][4-byte BE sample-rate][raw PCM16…]``.  The
    rate is **inline** (not a separate control message) so the client can
    never play TTS at the wrong rate by racing a cross-stream
    ``audio_format`` against the audio bytes.  The rate is constant for a
    stream's lifetime; a TTS sample-rate change FINs the current stream and
    opens a fresh one whose header carries the new rate.
  * client→server: ``[1-byte 0x01][4-byte BE sample-rate][raw PCM16…]``.
    Symmetric with server→client: the mic rate is **inline**, not a separate
    ``config`` control message, so it can never race the audio bytes on an
    independent QUIC stream and have early mic PCM wrapped at the wrong rate.
    The rate is constant for a stream's lifetime; a re-opened audio stream
    re-reads its own header.

``0x02`` — **control stream** — repeated ``[4-byte BE length][UTF-8 JSON]`` frames.

The client opens two streams (audio + control) and writes mic PCM /
client-side control messages there.  The server, in turn, opens its own audio
and control streams via :meth:`H3Connection.create_webtransport_stream` and
writes TTS audio / server-side control messages there.  The browser
demultiplexes server-opened streams via ``incomingBidirectionalStreams`` and
reads the tag byte to dispatch.  Control message shapes mirror
:class:`~easycat.transports.websocket.WebSocketTransport`:

* server→client: ``{"type":"ready"}`` (the outbound sample rate travels
  inline on the audio stream, see above — there is no ``audio_format``
  control message)
* client→server: ``{"type":"start"}``, ``{"type":"stop"}``.  A
  ``{"type":"config","sample_rate":N}`` frame is still accepted for
  backward tolerance but is informational only — the mic rate travels
  inline on the audio stream (see above), not via this frame.

Loss behaviour (v1)
-------------------
All-reliable streams, no datagrams, no application NACK.  Within a single
stream, a packet loss costs ~1 RTT to recover (typically 30-100 ms), which is
what an application-level NACK round-trip would cost anyway.  The win over
WebSocket is that audio and control are independent QUIC streams: control
traffic never stalls audio (or vice versa), and each direction of a
bidirectional stream has independent flow control.  The flip side of
independent streams is that there is **no cross-stream ordering**, which is
why the sample rate is carried inline on the audio stream — in *both*
directions — rather than as a separate control frame.

Connection bounding
-------------------
``max_concurrent_sessions`` bounds accepted WebTransport *sessions* (each
backed by a handler task + queues).  A QUIC connection that completes the
TLS/QUIC handshake but never sends a valid CONNECT (or targets a wrong
``:path`` and gets a 404/503) holds no session resources and is torn down by
QUIC's ``idle_timeout`` (``_IDLE_TIMEOUT_SEC``); that timeout is the only
bound on such lingering connections.

Authentication and bind safety
------------------------------
The default bind is loopback-only. A non-loopback bind requires
``auth_token`` unless ``unsafe_allow_no_auth=True`` is set explicitly.
Token-bearing servers authorize the HTTP/3 CONNECT request before allocating a
session. ``Authorization: Bearer`` is the default credential path;
``?token=`` is accepted only with ``allow_query_token=True`` for browser
clients that cannot set arbitrary CONNECT headers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn
from urllib.parse import urlsplit

from easycat._audio_utils import PCM16StreamResampler
from easycat._concurrency import shielded_cleanup
from easycat._extras import require_module
from easycat._net import normalize_auth_token
from easycat._numeric import is_finite_number
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.runtime.scope import RuntimeScope
from easycat.teardown_budgets import SERVER_FORCE_SHUTDOWN_TIMEOUT_S
from easycat.transports._base import (
    _DEGRADED_INBOUND_QUEUE_FULL as _DEGRADED_INBOUND_QUEUE_FULL,  # noqa: PLC0414 compatibility export
)
from easycat.transports._base import (
    AudioQueueMixin,
    _enqueue_inbound_chunk,
    _raise_rollback_cancellation,
    make_version_info,
)
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES
from easycat.transports.websocket import _valid_config_sample_rate

if TYPE_CHECKING:
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.asyncio.server import QuicServer
    from aioquic.h3.connection import H3Connection
    from aioquic.quic.configuration import QuicConfiguration

    from easycat.server.auth import AuthPolicy

logger = logging.getLogger(__name__)


async def _await_with_hard_timeout(
    future: asyncio.Task[object],
    *,
    timeout_s: float,
) -> bool:
    """Await cleanup without waiting for cancellation-resistant work.

    ``asyncio.wait_for`` requests cancellation at its deadline but then waits
    for cancellation cleanup. A user session handler is allowed to catch
    ``CancelledError``, so use ``asyncio.wait`` and retain any survivor in a
    background ledger instead. The owner still retains its primary resource
    reference and refuses restart until a later ``stop()`` completes cleanup.
    """
    try:
        done, _pending = await asyncio.wait({future}, timeout=max(timeout_s, 0.0))
    except asyncio.CancelledError:
        future.cancel()
        raise
    if future in done:
        await future
        return True
    future.cancel()
    # Give cooperative cleanup one event-loop turn without waiting for a
    # coroutine that intentionally ignores cancellation. The caller's runtime
    # scope retains the exact future for a later retry.
    await asyncio.sleep(0)
    return False


# Stream-purpose tags written as the first byte on each client-opened stream.
_TAG_AUDIO = 0x01
_TAG_CONTROL = 0x02

# Inbound (mic→server) queue size: prefer preserving user audio.  Higher than
# WebSocketTransport's 200.
_DEFAULT_INBOUND_MAX_PENDING = 500
# Outbound (TTS→client) queue size: still well above WebRTC defaults, but
# intentionally lower than inbound so we drop TTS more readily under pressure.
_DEFAULT_OUTBOUND_MAX_PENDING = 300

# Per-QUIC-stream flow-control window (~2 s of 16 kHz PCM16 audio).  This is
# the *initial* receive window we advertise per stream; aioquic auto-grows it
# as we actually consume, so a slow legitimate stream is never starved while a
# burst from a stalled/malicious peer is still bounded.
_MAX_STREAM_DATA = 64 * 1024
# Connection-wide initial receive window.  A client opens at most an inbound
# audio + control stream (each capped at ``_MAX_STREAM_DATA``); four windows
# leaves headroom for a re-opened audio stream during a sample-rate change
# while keeping the connection-level bound well under aioquic's 1 MiB default
# (aioquic auto-doubles this as data is consumed, so long sessions are fine).
_MAX_CONNECTION_DATA = 4 * _MAX_STREAM_DATA
# High-water mark (bytes) on aioquic's *unsent + unacked* per-stream send
# buffer for the server→client audio stream.  ``send_stream_data`` only
# appends to that buffer; bytes leave only as QUIC flow control / congestion
# permit.  A stalled or slow-reading client lets the buffer grow without
# bound, so the outbound writer pauses draining ``_out_queue`` once it crosses
# this mark — restoring ``outbound_max_pending`` as the real memory bound.
# Four windows tolerates a healthy bandwidth-delay product while still capping
# a stalled client at ~256 KiB of buffered TTS.
_OUTBOUND_SEND_BUFFER_HIGH_WATER = 4 * _MAX_STREAM_DATA
# Limit each write handed to aioquic to one stream window.  The writer reserves
# this full amount before dequeuing a fragment, so even an unexpectedly large
# TTS chunk cannot jump the QUIC send buffer past the high-water mark in one
# call to ``send_stream_data``.
_OUTBOUND_SEND_WRITE_MAX_BYTES = _MAX_STREAM_DATA
# Poll interval while waiting for the per-stream send buffer to drain below
# the high-water mark.  Short enough to stay responsive to a recovering
# client; cheap because it only runs while actually backpressured.
_OUTBOUND_BACKPRESSURE_POLL_SEC = 0.05
# Voice turns can have multi-second silences between user/bot exchanges, so
# don't tear the QUIC connection down on short idle periods.
_IDLE_TIMEOUT_SEC = 30.0

# DoS bounds on the control framing layer.  A single JSON control frame is
# never larger than a few hundred bytes in practice; capping at 64 KiB lets us
# reject crafted length prefixes (a malicious uint32 can advertise up to 4 GB
# and pin app-side buffers indefinitely while bytes trickle in).
_MAX_CONTROL_FRAME_BYTES = 64 * 1024
_WEBTRANSPORT_WRITER_TASK_NAME = "webtransport_writer"
_WEBTRANSPORT_WRITER_COHORT = "transport-write"
_WEBTRANSPORT_HANDLER_TASK_NAME = "webtransport_handler"
_WEBTRANSPORT_HANDLER_COHORT = "transport-handlers"
_WEBTRANSPORT_LISTENER_TASK_NAME = "webtransport_listener_close"
_WEBTRANSPORT_LISTENER_COHORT = "transport-listener"

# Cap on the number of streams whose purpose tag has not yet arrived.  A
# malicious client can open many bidi streams and never write the first byte;
# this bounds ``_pending_tags``.  No per-stream byte cap is needed — the tag
# is byte 0, so a stream is dispatched (and forgotten) the instant any byte
# arrives, and a single delivery is already bounded by the QUIC flow-control
# window (``_MAX_STREAM_DATA``).
_MAX_PENDING_TAG_STREAMS = 4

# Cap on the number of classified-but-rejected client streams tracked at
# once (duplicate audio/control streams, or streams with an unknown tag).  A
# rejected id must stay ignored until its QUIC stream FINs — see
# ``_reject_stream`` — so a misbehaving client that opens and abandons many
# such streams could otherwise grow this set without bound.  A legitimate
# client never produces a single rejected stream; a flood is a malicious-peer
# signal, so the session is torn down past this cap (mirrors the poisoned
# control-codec path) rather than silently dropping tracking and reopening
# the misroute the set exists to prevent.
_MAX_REJECTED_STREAMS = 32

# Truncation cap for user-controlled values that end up in log messages.
_LOG_TRUNC = 64

_DEFAULT_PATH = "/easycat"

# WebTransport-specific ``TransportDegraded.reason`` codes emitted on the
# session event bus.  These mirror conditions that previously only reached
# ``logger.warning``; emitting them keeps the journal the single source of
# truth for observability (a dropped frame / torn-down session is now visible
# in an exported debug bundle, not just the process log).  The cross-transport
# ``inbound_queue_full`` code is shared from ``_base`` (imported above).
_DEGRADED_OUTBOUND_QUEUE_FULL = "outbound_queue_full"
_DEGRADED_CONTROL_CODEC_POISONED = "control_codec_poisoned"
_DEGRADED_REJECTED_STREAM_FLOOD = "rejected_stream_flood"
_DEGRADED_OUTBOUND_WRITER_CRASHED = "outbound_writer_crashed"
_DEGRADED_BARGE_IN_RESET_FAILED = "barge_in_reset_failed"
# Outbound backpressure relies on reading aioquic's private per-stream send
# buffer (``_quic`` / ``_streams`` / ``sender._buffer``). Server startup
# preflights that exact path and refuses to bind when it is incompatible. This
# degraded event remains as defense in depth for directly-constructed sessions
# and unexpected runtime mutation.
_DEGRADED_OUTBOUND_BACKPRESSURE_BLIND = "outbound_backpressure_blind"
_AIOQUIC_BACKPRESSURE_ACCESS_PATH = (
    "QuicConnectionProtocol._quic -> QuicConnection._streams -> QuicStream.sender._buffer"
)

# Signature of the per-session degraded-event emitter injected by
# :class:`WebTransportConnectionTransport`.  ``fatal`` marks conditions that
# tore the session down (vs. a recoverable single-frame drop).
_DegradedEmitter = Callable[..., None]

# Type alias for the user-supplied per-session handler. Module-private — not
# part of the public surface.
_SessionHandler = Callable[["WebTransportConnectionTransport"], Awaitable[None]]


class _AioquicBackpressureCompatibilityError(RuntimeError):
    """Installed aioquic cannot support the bounded WebTransport writer."""


def _read_aioquic_send_buffer(
    quic_protocol: object,
    stream_id: int,
) -> tuple[Any | None, str | None]:
    """Resolve aioquic's private per-stream send buffer.

    The second tuple item is an incompatibility reason. A missing stream is a
    legitimate lifecycle state and returns ``(None, None)``.
    """
    quic = getattr(quic_protocol, "_quic", None)
    if quic is None:
        return None, "QuicConnectionProtocol._quic missing"
    streams = getattr(quic, "_streams", None)
    if not isinstance(streams, dict):
        return None, "QuicConnection._streams missing"
    stream = streams.get(stream_id)
    if stream is None:
        return None, None
    sender = getattr(stream, "sender", None)
    raw_buffer = getattr(sender, "_buffer", None) if sender is not None else None
    if raw_buffer is None:
        return None, "QuicStream.sender._buffer missing"
    try:
        len(raw_buffer)
    except TypeError:
        return None, "QuicStream.sender._buffer is not sized"
    return raw_buffer, None


def _raise_aioquic_backpressure_incompatible(reason: str) -> NoReturn:
    raise _AioquicBackpressureCompatibilityError(
        "Installed aioquic is incompatible with EasyCat's bounded WebTransport "
        f"writer: {reason}. Required access path: {_AIOQUIC_BACKPRESSURE_ACCESS_PATH}. "
        "Install a supported aioquic version or upgrade EasyCat before serving traffic."
    )


def _noop_degraded(reason: str, detail: str = "", *, fatal: bool = False) -> None:
    """Default :data:`_DegradedEmitter` — used when no event bus is wired
    (e.g. a directly-constructed session in a unit test)."""


def _trunc_for_log(value: object) -> str:
    """``repr(value)`` truncated to keep adversarial inputs out of large log
    lines.  ``repr`` already escapes control characters, so this only bounds
    size, not content sanitization."""
    s = repr(value)
    return s if len(s) <= _LOG_TRUNC else s[:_LOG_TRUNC] + "...(truncated)"


@dataclass
class WebTransportTransportConfig:
    """Shared configuration for :class:`WebTransportTransport` and
    :class:`WebTransportServer`.
    """

    # The server sees datagram/write time, not the browser's playout clock, so
    # browser endpoint echo cancellation is the safe automatic default.
    default_echo_cancellation_enabled: ClassVar[bool] = False

    host: str = "127.0.0.1"
    port: int = 4433
    certfile: str = ""
    keyfile: str = ""
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = _DEFAULT_INBOUND_MAX_PENDING
    outbound_max_pending: int = _DEFAULT_OUTBOUND_MAX_PENDING
    path: str = _DEFAULT_PATH
    # Hard cap on concurrent accepted WebTransport sessions on a single
    # ``WebTransportServer``.  Each session retains a QUIC connection plus
    # inbound/outbound queues; without a cap a single client IP can open
    # arbitrarily many sessions and exhaust process memory.
    max_concurrent_sessions: int = 64
    # Bearer auth is enforced on the HTTP/3 CONNECT request before any session
    # transport or provider-backed EasyCat session is created. These auth fields
    # stay after the original positional config fields for compatibility.
    auth_token: str | None = None
    # Browser WebTransport cannot set arbitrary CONNECT headers. Query-token
    # auth therefore exists as an explicit opt-in and remains off by default.
    allow_query_token: bool = False
    # The only way to bind a non-loopback interface without ``auth_token``.
    unsafe_allow_no_auth: bool = False
    max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES
    # Hard bound for server-side handler/listener teardown. A user-provided
    # session handler can suppress cancellation, and aioquic's listener can
    # retain one such handler while waiting to close; neither may wedge
    # ``WebTransportServer.stop()`` indefinitely.
    force_shutdown_timeout_s: float = SERVER_FORCE_SHUTDOWN_TIMEOUT_S

    def __post_init__(self) -> None:
        if (
            isinstance(self.outbound_max_pending, bool)
            or not isinstance(self.outbound_max_pending, int)
            or self.outbound_max_pending < 1
        ):
            raise ValueError("outbound_max_pending must be an integer >= 1")
        if (
            isinstance(self.max_concurrent_sessions, bool)
            or not isinstance(self.max_concurrent_sessions, int)
            or self.max_concurrent_sessions < 1
        ):
            raise ValueError("max_concurrent_sessions must be an integer >= 1")
        if (
            not is_finite_number(self.force_shutdown_timeout_s)
            or self.force_shutdown_timeout_s < 0
        ):
            raise ValueError("force_shutdown_timeout_s must be a finite number >= 0")
        if (
            self.audio_format.encoding != "pcm"
            or self.audio_format.sample_width != 2
            or self.audio_format.channels != 1
        ):
            raise ValueError(
                "audio_format must be mono PCM16 audio "
                f"(got encoding={self.audio_format.encoding!r}, "
                f"sample_width={self.audio_format.sample_width!r}, "
                f"channels={self.audio_format.channels!r})"
            )


def _build_quic_configuration(certfile: str, keyfile: str) -> QuicConfiguration:
    if not certfile or not keyfile:
        raise ValueError(
            "WebTransport requires certfile and keyfile paths (TLS is mandatory). "
            "Generate a local cert with: openssl req -x509 -newkey rsa:2048 "
            '-keyout key.pem -out cert.pem -days 1 -nodes -subj "/CN=localhost"'
        )
    quic_config_mod = require_module(
        "aioquic.quic.configuration", extra="webtransport", purpose="WebTransport transport"
    )
    config = quic_config_mod.QuicConfiguration(
        alpn_protocols=["h3"],
        is_client=False,
        # Required, not optional: aioquic's H3 settings validation rejects
        # ENABLE_WEBTRANSPORT unless H3_DATAGRAM is also negotiated, and
        # H3_DATAGRAM in turn requires the max_datagram_frame_size transport
        # parameter.  We still don't *send* datagrams (v1 is all-reliable
        # streams); this only satisfies the handshake contract.
        max_datagram_frame_size=65536,
        idle_timeout=_IDLE_TIMEOUT_SEC,
    )
    config.load_cert_chain(certfile, keyfile)
    # aioquic 1.x exposes a single ``max_stream_data`` field that seeds the
    # bidi-local / bidi-remote / uni per-stream windows; the older
    # ``max_stream_data_bidi_local`` / ``_remote`` / ``_uni`` names are NOT
    # ``QuicConfiguration`` attributes, so assigning them only created unused
    # attributes and left the default 1 MiB window in place.  Set the real
    # fields so the intended 64 KiB per-stream and bounded connection-wide
    # windows are actually advertised.
    config.max_stream_data = _MAX_STREAM_DATA
    config.max_data = _MAX_CONNECTION_DATA
    return config


# ── Framing helpers ────────────────────────────────────────────────


class _ControlCodec:
    """Length-prefixed (4-byte BE) UTF-8 JSON framing.

    Bounded: a length prefix above ``_MAX_CONTROL_FRAME_BYTES`` poisons the
    codec.  Once poisoned, no further frames are decoded — callers should
    treat a poisoned codec as a malicious peer signal and tear down the
    inbound control stream.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._poisoned = False

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        if self._poisoned:
            return []
        self._buf.extend(data)
        out: list[dict[str, Any]] = []
        while True:
            if len(self._buf) < 4:
                break
            (length,) = struct.unpack_from(">I", self._buf, 0)
            if length > _MAX_CONTROL_FRAME_BYTES:
                logger.warning(
                    "WebTransport control frame length %d exceeds %d-byte cap — poisoning codec",
                    length,
                    _MAX_CONTROL_FRAME_BYTES,
                )
                self._poisoned = True
                self._buf.clear()
                break
            if len(self._buf) < 4 + length:
                break
            payload = bytes(self._buf[4 : 4 + length])
            del self._buf[: 4 + length]
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (RecursionError, ValueError):
                logger.warning("Ignoring malformed WebTransport control frame")
                continue
            if isinstance(msg, dict):
                out.append(msg)
        return out

    @staticmethod
    def encode(msg: dict[str, Any]) -> bytes:
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        return struct.pack(">I", len(body)) + body


# ── Per-session state ──────────────────────────────────────────────


class _WebTransportSession:
    """State for one WebTransport CONNECT session.

    Multiplexes audio and control streams on top of an :class:`H3Connection`.
    Inbound bytes are pushed into ``in_queue``; outbound audio chunks are
    pulled from ``out_queue`` by a background writer task.
    """

    def __init__(
        self,
        *,
        h3: H3Connection,
        quic_protocol: QuicConnectionProtocol,
        session_id: int,
        target_sample_rate: int,
        audio_format: AudioFormat,
        in_queue: asyncio.Queue[AudioChunk | None],
        out_queue: asyncio.Queue[AudioChunk | None],
        on_close: asyncio.Event,
        emit_degraded: _DegradedEmitter | None = None,
        writer_tasks: RuntimeTaskScope | None = None,
    ) -> None:
        self._h3 = h3
        # Surfaces drop / poison / abort conditions on the session event bus
        # so they land in the journal (see ``_DEGRADED_*``).  No-op until a
        # bus is wired, so a directly-constructed session stays inert.
        self._emit_degraded: _DegradedEmitter = emit_degraded or _noop_degraded
        self._quic_protocol = quic_protocol
        self._session_id = session_id
        self._target_rate = target_sample_rate
        self._audio_format = audio_format
        # Placeholder until the inline mic-rate header is parsed; never used
        # to wrap audio before then (see ``_handle_audio_bytes``).
        self._inbound_format = audio_format
        # Inbound (mic) audio streams are self-describing: each carries a
        # 4-byte BE sample-rate header right after its tag byte, mirroring
        # the server→client framing.  This removes the cross-stream race
        # where mic PCM overtakes a ``config`` control frame and gets
        # wrapped at the wrong rate.  Parsed once per inbound audio stream;
        # reset when that stream ends so a re-opened one re-reads its header.
        self._inbound_rate: int | None = None
        self._inbound_rate_hdr = bytearray()
        self._inbound_resampler = PCM16StreamResampler(target_sample_rate)
        # Sample rate of the currently-open server→client audio stream, or
        # None when no audio stream is open.  A change opens a fresh stream
        # (see ``_outbound_writer`` / ``_end_audio_stream``).
        self._outbound_rate: int | None = None
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._on_close = on_close
        self._writer_tasks = writer_tasks or RuntimeTaskScope(
            owner_label="webtransport-session-writer",
            member_name=_WEBTRANSPORT_WRITER_TASK_NAME,
            cohort=_WEBTRANSPORT_WRITER_COHORT,
            logger=logger,
            failure_message="WebTransport outbound writer failed",
            drop_if_closed=False,
        )

        # Client-opened stream ids (server reads from these halves).
        self._inbound_audio_stream_id: int | None = None
        self._inbound_control_stream_id: int | None = None
        # Server-initiated stream ids (server writes to these halves; the
        # client demultiplexes via its ``incomingBidirectionalStreams``).
        self._outbound_audio_stream_id: int | None = None
        self._outbound_control_stream_id: int | None = None
        self._control_codec = _ControlCodec()
        self._pending_tags: dict[int, bytearray] = {}
        # Client stream ids we classified and rejected (duplicate
        # audio/control, or unknown tag).  Their leading tag/header byte was
        # already consumed by the rejecting dispatch, so later chunks must be
        # ignored — not re-dispatched — until the stream FINs.
        self._rejected_stream_ids: set[int] = set()
        self._writer_task: asyncio.Task[None] | None = None
        # Latches once the outbound backpressure probe finds aioquic's private
        # send-buffer accessors missing/renamed, so we emit the degraded signal
        # at most once per session instead of on every poll (see
        # ``_await_outbound_capacity`` / ``_DEGRADED_OUTBOUND_BACKPRESSURE_BLIND``).
        self._backpressure_blind_reported = False

    @property
    def session_id(self) -> int:
        return self._session_id

    async def start(self) -> None:
        writer_task = self._writer_tasks.create_task(
            self._outbound_writer(),
            task_name="webtransport-outbound-writer",
        )
        assert writer_task is not None
        self._writer_task = writer_task
        self._send_control({"type": "ready"})

    async def stop(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            current = asyncio.current_task()
            cancellation_count = current.cancelling() if current is not None else 0
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                if current is not None and current.cancelling() > cancellation_count:
                    raise
            if current is not None and current.cancelling() > cancellation_count:
                raise asyncio.CancelledError
        self._writer_task = None
        self._inbound_resampler.reset()
        await self._writer_tasks.release_standalone_if_empty()

    def handle_stream_data(self, stream_id: int, data: bytes, ended: bool) -> None:
        if stream_id in self._rejected_stream_ids:
            # Already classified and rejected (duplicate audio/control
            # stream, or an unknown tag).  The rejecting dispatch consumed
            # this stream's leading tag/header byte, so routing the
            # remainder back through ``_dispatch_untagged_stream`` could
            # misread a PCM byte that happens to equal 0x01/0x02 as a fresh
            # audio/control header.  Keep ignoring every chunk until FIN.
            pass
        elif stream_id == self._inbound_audio_stream_id:
            self._handle_audio_bytes(data)
        elif stream_id == self._inbound_control_stream_id:
            self._handle_control_bytes(data)
        else:
            self._dispatch_untagged_stream(stream_id, data, ended)

        if ended:
            # A single data stream closing does NOT end the WebTransport
            # session — the session lives as long as the QUIC connection /
            # CONNECT stream.  Tearing the whole session down here would let
            # a client that half-closes just its audio (or control) stream
            # kill an otherwise healthy session.  Session teardown happens in
            # ``connection_lost`` -> ``_mark_connection_lost``.  Here we only
            # release per-stream bookkeeping: drop any pending tag buffer (so
            # a half-tagged client can't pin ``_pending_tags`` entries) and
            # forget the inbound stream id so a re-opened stream is accepted.
            self._pending_tags.pop(stream_id, None)
            self._rejected_stream_ids.discard(stream_id)
            if stream_id == self._inbound_audio_stream_id:
                tail = self._inbound_resampler.finish()
                if tail:
                    _enqueue_inbound_chunk(
                        self._in_queue,
                        AudioChunk(data=tail, format=self._audio_format),
                        emit_degraded=self._emit_degraded,
                        context="WebTransport",
                    )
                self._inbound_audio_stream_id = None
                # A re-opened audio stream is a fresh, self-describing
                # stream; force its inline rate header to be re-read.
                self._inbound_rate = None
                self._inbound_rate_hdr.clear()
            elif stream_id == self._inbound_control_stream_id:
                self._inbound_control_stream_id = None
                # A control stream that closes mid-frame leaves a partial
                # length/payload in the codec.  A re-opened control stream
                # must start from clean framing state, or its first frame is
                # parsed against the previous stream's stale bytes — silently
                # dropped, or (if the stale prefix decodes to an oversized
                # length) poisoning the codec and tearing the session down.
                self._control_codec = _ControlCodec()

    def _dispatch_untagged_stream(self, stream_id: int, data: bytes, ended: bool) -> None:
        """Identify a stream by its leading tag byte and route it.

        The purpose tag is always byte 0, so there is never a reason to
        accumulate bytes waiting for it — ``_pending_tags`` exists only to
        bridge zero-byte deliveries (an event with empty ``data`` before the
        first real byte).  As soon as any byte is present we dispatch the
        whole buffer (tag + however much payload arrived in the same event)
        and forget the stream.

        The ``_MAX_PENDING_TAG_STREAMS`` cap is applied **only** to zero-byte
        pending streams — exactly the unbounded-growth vector (a client that
        opens many bidi streams and never writes).  A *non-empty* first
        delivery is always dispatched immediately and never refused, so the
        tag byte can never be dropped for a well-behaved client (a per-stream
        byte cap, or refusing a non-empty delivery here, would discard the
        tag with the payload and leave the stream permanently mis-routed).
        A single delivery is itself bounded by the QUIC per-stream
        flow-control window (``_MAX_STREAM_DATA``).
        """
        buf = self._pending_tags.pop(stream_id, None)
        if buf is None:
            if not data:
                # Zero-byte delivery before the first real byte: this is the
                # only path that consumes a (capped) pending slot.
                if len(self._pending_tags) >= _MAX_PENDING_TAG_STREAMS:
                    logger.warning(
                        "Refusing untagged WebTransport stream %d — too many pending",
                        stream_id,
                    )
                    return
                self._pending_tags[stream_id] = bytearray()
                return
            buf = bytearray(data)
        else:
            buf.extend(data)
            if not buf:
                # Still zero-byte; re-park (slot already counted).
                self._pending_tags[stream_id] = buf
                return
        tag = buf[0]
        payload = bytes(buf[1:])
        if tag == _TAG_AUDIO:
            if self._inbound_audio_stream_id is not None:
                logger.warning(
                    "Ignoring extra audio stream %d (already have %d)",
                    stream_id,
                    self._inbound_audio_stream_id,
                )
                self._reject_stream(stream_id)
                return
            self._inbound_audio_stream_id = stream_id
            if payload:
                self._handle_audio_bytes(payload)
        elif tag == _TAG_CONTROL:
            if self._inbound_control_stream_id is not None:
                logger.warning(
                    "Ignoring extra control stream %d (already have %d)",
                    stream_id,
                    self._inbound_control_stream_id,
                )
                self._reject_stream(stream_id)
                return
            self._inbound_control_stream_id = stream_id
            if payload:
                self._handle_control_bytes(payload)
        else:
            logger.warning("Unknown WebTransport stream tag 0x%02x on %d", tag, stream_id)
            self._reject_stream(stream_id)

    def _reject_stream(self, stream_id: int) -> None:
        """Remember a classified-but-unusable stream so every later chunk on
        it is ignored until it FINs.

        The rejecting branch in :meth:`_dispatch_untagged_stream` has already
        consumed (and discarded) this stream's leading tag/header byte.  Left
        untracked, a later chunk would re-enter ``_dispatch_untagged_stream``
        with the tag gone, so a PCM byte that happens to equal
        ``0x01``/``0x02`` could be misread as a fresh audio/control header —
        accepted as a real stream once the original one has ended.  A
        legitimate client never produces a rejected stream; a flood is a
        malicious-peer signal, so the session is torn down past
        ``_MAX_REJECTED_STREAMS`` (mirrors the poisoned control-codec path)
        rather than silently dropping tracking and reopening that misroute.
        """
        self._rejected_stream_ids.add(stream_id)
        if len(self._rejected_stream_ids) > _MAX_REJECTED_STREAMS and not self._on_close.is_set():
            logger.warning(
                "WebTransport session %d exceeded %d rejected streams — closing",
                self._session_id,
                _MAX_REJECTED_STREAMS,
            )
            self._emit_degraded(
                _DEGRADED_REJECTED_STREAM_FLOOD,
                f"session {self._session_id} exceeded {_MAX_REJECTED_STREAMS} rejected streams",
                fatal=True,
            )
            try:
                self.close_connection(reason="too many rejected streams")
            finally:
                self._on_close.set()

    def _handle_audio_bytes(self, data: bytes) -> None:
        if not data:
            return
        if self._inbound_rate is None:
            # Consume the inline [4-byte BE sample-rate] header that
            # prefixes every client→server audio stream (symmetric with the
            # server→client framing).  It may be split across deliveries.
            self._inbound_rate_hdr.extend(data)
            if len(self._inbound_rate_hdr) < 4:
                return
            (rate,) = struct.unpack_from(">I", self._inbound_rate_hdr, 0)
            pcm = bytes(self._inbound_rate_hdr[4:])
            self._inbound_rate_hdr.clear()
            valid = _valid_config_sample_rate(rate)
            if valid is None:
                logger.warning(
                    "Invalid WebTransport inbound sample rate %s — assuming %d",
                    _trunc_for_log(rate),
                    self._target_rate,
                )
                valid = self._target_rate
            self._inbound_rate = valid
            self._inbound_format = AudioFormat(
                sample_rate=valid,
                channels=self._audio_format.channels,
                sample_width=self._audio_format.sample_width,
                encoding=self._audio_format.encoding,
            )
            logger.info("Client WebTransport mic format: %s", self._inbound_format)
            if not pcm:
                return
            data = pcm
        converted = self._inbound_resampler.process(
            data,
            self._inbound_format.sample_rate,
        )
        if converted:
            _enqueue_inbound_chunk(
                self._in_queue,
                AudioChunk(data=converted, format=self._audio_format),
                emit_degraded=self._emit_degraded,
                context="WebTransport",
            )

    def _handle_control_bytes(self, data: bytes) -> None:
        for msg in self._control_codec.feed(data):
            self._handle_control_message(msg)
        if self._control_codec.poisoned and not self._on_close.is_set():
            # An oversized length prefix is a malicious-peer signal.  Honor
            # the codec's documented contract: tear the session down rather
            # than silently swallowing all further control frames. The
            # ``_on_close`` guard keeps queued QUIC events dispatched after
            # the close from re-counting the same incident.
            logger.warning(
                "WebTransport control codec poisoned (oversized frame) — closing session %d",
                self._session_id,
            )
            self._emit_degraded(
                _DEGRADED_CONTROL_CODEC_POISONED,
                f"oversized control frame poisoned session {self._session_id}",
                fatal=True,
            )
            try:
                self.close_connection(reason="control framing violation")
            finally:
                self._on_close.set()

    def _handle_control_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "config":
            # The mic sample rate now travels inline on the audio stream
            # (see ``_handle_audio_bytes``) so it can't race this frame on
            # an independent QUIC stream.  ``config`` is still accepted for
            # backward tolerance but no longer drives inbound resampling.
            logger.debug("Client sent WebTransport config: %s", _trunc_for_log(msg))
        elif msg_type in ("start", "stop"):
            logger.debug("Client sent WebTransport %s signal", msg_type)
        else:
            logger.debug("Unknown WebTransport control message type: %s", _trunc_for_log(msg_type))

    def _send_stream_bytes(self, stream_id: int, data: bytes) -> None:
        """Write raw bytes onto a WebTransport stream.

        ``H3Connection.create_webtransport_stream`` emits the
        ``WEBTRANSPORT_STREAM`` frame header; everything after it is opaque
        payload that must go out as plain QUIC stream data.  Using
        ``H3Connection.send_data`` here would wrap the bytes in an HTTP/3
        ``DATA`` frame, which the peer rejects with ``FrameUnexpected`` ("DATA
        frame is not allowed in this state") because no response headers were
        sent on a WebTransport stream.
        """
        quic = getattr(self._quic_protocol, "_quic", None)
        if quic is None:
            return
        quic.send_stream_data(stream_id, data, end_stream=False)

    def _send_control(self, msg: dict[str, Any]) -> None:
        if self._outbound_control_stream_id is None:
            self._outbound_control_stream_id = self._h3.create_webtransport_stream(
                self._session_id
            )
            self._send_stream_bytes(self._outbound_control_stream_id, bytes([_TAG_CONTROL]))
        self._send_stream_bytes(self._outbound_control_stream_id, _ControlCodec.encode(msg))
        self._quic_protocol.transmit()

    def _end_audio_stream(self) -> None:
        """FIN the current server→client audio stream (clean end, keep
        already-buffered bytes flowing).

        Used on a TTS sample-rate change: the old-rate bytes still in flight
        should finish playing, so we don't ``reset`` — we FIN, the client
        drains and closes that reader, and the next chunk opens a fresh,
        self-describing stream carrying the new rate in its header.
        """
        if self._outbound_audio_stream_id is None:
            return
        quic = getattr(self._quic_protocol, "_quic", None)
        if quic is not None:
            try:
                quic.send_stream_data(self._outbound_audio_stream_id, b"", end_stream=True)
                self._quic_protocol.transmit()
            except Exception:
                logger.warning("ending WebTransport audio stream failed", exc_info=True)
        self._outbound_audio_stream_id = None
        self._outbound_rate = None

    def reset_audio_stream(self) -> None:
        """Abort the server→client audio stream so already-buffered bytes are
        discarded (barge-in semantics).

        ``QuicConnection.send_stream_data`` writes into aioquic's per-stream
        buffer immediately; once handed off, bytes are transmitted as flow
        control permits — draining the application queue alone is not
        sufficient to stop the client from hearing the next ~2 s of TTS (the
        ``max_stream_data`` window).  Resetting the stream via the underlying
        :class:`QuicConnection` aborts in-flight bytes and frees the slot;
        the next outbound chunk opens a fresh stream.
        """
        if self._outbound_audio_stream_id is None:
            return
        quic = getattr(self._quic_protocol, "_quic", None)
        if quic is None:
            self._outbound_audio_stream_id = None
            self._outbound_rate = None
            return
        try:
            quic.reset_stream(self._outbound_audio_stream_id, error_code=0)
            self._quic_protocol.transmit()
        except Exception:
            # Promoted from debug to warning: if reset_stream silently fails,
            # the client will keep hearing in-flight TTS after a barge-in.
            logger.warning("reset_stream failed for audio stream", exc_info=True)
            self._emit_degraded(
                _DEGRADED_BARGE_IN_RESET_FAILED,
                "reset_stream failed; client may keep hearing TTS after barge-in",
            )
        finally:
            self._outbound_audio_stream_id = None
            # Next chunk opens a fresh stream; force it to re-emit the inline
            # rate header even if the rate is unchanged.
            self._outbound_rate = None

    def close_connection(self, *, reason: str = "") -> None:
        """Send CONNECTION_CLOSE and tear down the QUIC connection.

        ``QuicConnectionProtocol.close`` flushes the close frame itself.
        """
        self._quic_protocol.close(error_code=0, reason_phrase=reason)

    async def _await_outbound_capacity(self) -> None:
        """Reserve room for one bounded write in the current audio stream.

        ``QuicConnection.send_stream_data`` only appends to that buffer; the
        bytes leave the process only as QUIC flow control / congestion
        permit.  When a client stops reading (or its flow-control window
        closes), nothing drains it, so without this gate the writer would
        keep pulling from ``_out_queue`` and aioquic's unsent buffer — and
        process memory — would grow without bound, defeating
        ``outbound_max_pending``.

        Polling happens here, *before* the queue ``get()`` in
        :meth:`_outbound_writer`, so the documented no-await invariant
        between ``get()`` and ``transmit()`` stays intact: a barge-in
        (:meth:`reset_audio_stream`) still can't race a half-written chunk
        because the writer is parked here, not suspended mid-send.  While we
        wait, ``_out_queue`` fills and ``send_audio`` starts returning False
        — i.e. TTS is dropped under sustained backpressure, which is the
        documented behaviour. There is no public aioquic accessor for
        per-stream buffered bytes, so server startup preflights the private
        ``_quic`` / ``_streams`` / ``sender._buffer`` path before binding. The
        defensive checks here still emit
        ``_DEGRADED_OUTBOUND_BACKPRESSURE_BLIND`` for directly-constructed
        sessions or unexpected runtime mutation.
        """
        while not self._on_close.is_set():
            sid = self._outbound_audio_stream_id
            if sid is None:
                # No outbound audio stream open yet — a legitimate idle state,
                # not an aioquic rename, so don't flag the probe as blind.
                return
            raw_buffer, incompatibility = _read_aioquic_send_buffer(
                self._quic_protocol,
                sid,
            )
            if incompatibility is not None:
                self._report_backpressure_blind(incompatibility)
                return
            if raw_buffer is None:
                # Stream not yet registered or already finished — legitimate.
                return
            # Each queued fragment is at most ``_OUTBOUND_SEND_WRITE_MAX_BYTES``.
            # Leave room for that complete write rather than merely checking
            # whether the buffer has already crossed the high-water mark.  A
            # producer is free to emit arbitrarily large ``AudioChunk``
            # objects, so without this reservation one giant chunk could add
            # megabytes to aioquic in a single call before the next loop gets
            # a chance to apply backpressure.
            if len(raw_buffer) <= (
                _OUTBOUND_SEND_BUFFER_HIGH_WATER - _OUTBOUND_SEND_WRITE_MAX_BYTES
            ):
                return
            await asyncio.sleep(_OUTBOUND_BACKPRESSURE_POLL_SEC)

    def _report_backpressure_blind(self, reason: str) -> None:
        """Emit the backpressure-blind degraded signal at most once per session.

        Server startup rejects known-incompatible aioquic versions before
        binding. This runtime signal is defense in depth for custom
        integrations that construct sessions directly or for an accessor that
        disappears unexpectedly after startup.
        """
        if self._backpressure_blind_reported:
            return
        self._backpressure_blind_reported = True
        logger.warning(
            "WebTransport outbound backpressure probe blind (%s) — send-buffer "
            "high-water gate is a no-op; outbound memory is no longer bounded by "
            "this gate",
            reason,
        )
        self._emit_degraded(
            _DEGRADED_OUTBOUND_BACKPRESSURE_BLIND,
            f"outbound backpressure probe blind: {reason}",
        )

    async def _outbound_writer(self) -> None:
        try:
            while True:
                # Apply QUIC send-capacity backpressure *before* taking the
                # next chunk so a slow/stalled client can't grow memory
                # without bound (see _await_outbound_capacity).
                await self._await_outbound_capacity()
                chunk = await self._out_queue.get()
                if chunk is None:
                    return
                # INVARIANT: there must be no ``await`` between the queue
                # ``get()`` above and the ``transmit()`` below.  ``clear_audio``
                # / ``reset_audio_stream`` run synchronously (no await) and rely
                # on this task always being parked at ``get()`` (or in the
                # capacity gate above) — never suspended mid-send — so a
                # barge-in can't race a half-written audio chunk onto the wire.
                rate = chunk.format.sample_rate
                if self._outbound_audio_stream_id is not None and rate != self._outbound_rate:
                    # TTS sample rate changed mid-session: FIN the current
                    # (old-rate) stream so the client closes that reader, then
                    # fall through to open a fresh stream whose inline header
                    # carries the new rate.  Each audio stream is thus
                    # self-describing and rate-constant for its lifetime.
                    self._end_audio_stream()
                if self._outbound_audio_stream_id is None:
                    self._outbound_audio_stream_id = self._h3.create_webtransport_stream(
                        self._session_id
                    )
                    self._outbound_rate = rate
                    self._send_stream_bytes(
                        self._outbound_audio_stream_id,
                        bytes([_TAG_AUDIO]) + struct.pack(">I", rate),
                    )
                self._send_stream_bytes(self._outbound_audio_stream_id, chunk.data)
                self._quic_protocol.transmit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("WebTransport outbound writer crashed")
            self._emit_degraded(
                _DEGRADED_OUTBOUND_WRITER_CRASHED,
                f"outbound writer crashed: {type(exc).__name__}",
                fatal=True,
            )
            # Signal session teardown so the owning transport disconnects
            # cleanly instead of wedging with send_audio() still returning
            # True while no bytes ever reach the peer.
            self._on_close.set()


# ── Per-connection aioquic protocol class ─────────────────────────


# ``_EasyCatH3Protocol`` subclasses ``aioquic.asyncio.QuicConnectionProtocol``,
# which is only available when the optional ``[webtransport]`` extra is
# installed.  We build the class lazily on first use so that importing this
# module (e.g. for the public-API snapshot or unit tests with fake H3
# objects) does not require aioquic.
_PROTOCOL_CLASS_CACHE: type | None = None


def _get_protocol_class() -> type:
    global _PROTOCOL_CLASS_CACHE
    if _PROTOCOL_CLASS_CACHE is not None:
        return _PROTOCOL_CLASS_CACHE

    aioquic_proto: Any = require_module(
        "aioquic.asyncio.protocol", extra="webtransport", purpose="WebTransport transport"
    )
    h3_conn = require_module(
        "aioquic.h3.connection", extra="webtransport", purpose="WebTransport transport"
    )
    h3_events = require_module(
        "aioquic.h3.events", extra="webtransport", purpose="WebTransport transport"
    )
    quic_events = require_module(
        "aioquic.quic.events", extra="webtransport", purpose="WebTransport transport"
    )

    class _EasyCatH3Protocol(aioquic_proto.QuicConnectionProtocol):
        """aioquic protocol that dispatches WebTransport sessions.

        One instance per QUIC connection.  When a CONNECT-webtransport
        request arrives on the expected path, builds a
        :class:`WebTransportConnectionTransport` and hands it to the configured
        session-accepted callback.  v1 accepts one WebTransport session per
        QUIC connection (matches browser usage).
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._h3: Any = None
            # NOTE: do *not* name this ``self._wt_transport`` — the aioquic
            # ``QuicConnectionProtocol`` base class already owns that attribute
            # for the asyncio ``DatagramTransport`` (assigned in
            # ``connection_made``).  Shadowing it both breaks QUIC sending and
            # makes the "already have a session" check below always true, so
            # every CONNECT is rejected with 409.
            self._wt_transport: WebTransportConnectionTransport | None = None
            # CONNECT stream id of the one accepted WebTransport session on
            # this QUIC connection.  Stream-data events carry their own
            # ``session_id``; anything not matching this is for a different
            # (e.g. 409-rejected) session and must not be fed into ours.
            self._accepted_session_id: int | None = None
            # Populated by the protocol factory before events flow.
            self._accept_path: str = ""
            self._on_session: Callable[[WebTransportConnectionTransport], None] = lambda _t: None
            # Capacity gate, checked *before* the 200 is sent so an over-cap
            # client gets a clean HTTP/3 503 instead of a 200 immediately
            # followed by CONNECTION_CLOSE.
            self._can_accept: Callable[[], bool] = lambda: True
            self._session_config: WebTransportTransportConfig = WebTransportTransportConfig()
            self._auth_policy: AuthPolicy | None = None

        def quic_event_received(self, event: Any) -> None:
            if isinstance(event, quic_events.ConnectionTerminated):
                # A peer QUIC CONNECTION_CLOSE (or idle timeout) is delivered
                # here as a QUIC event, NOT as asyncio ``connection_lost()``:
                # the aioquic server demultiplexes one UDP socket across many
                # connections, so ``connection_lost()`` is never called per
                # connection.  Surface it so ``wait_closed()`` unblocks and the
                # session slot is released instead of lingering until server
                # shutdown.
                self._mark_session_lost()
                return
            if self._h3 is None:
                self._h3 = h3_conn.H3Connection(self._quic, enable_webtransport=True)
            for h3_event in self._h3.handle_event(event):
                self._handle_h3_event(h3_event)

        def _mark_session_lost(self) -> None:
            """Tell the per-session transport its QUIC connection is gone.

            Idempotent (``_mark_connection_lost`` only sets state / enqueues
            teardown sentinels), so the multiple termination paths —
            ``ConnectionTerminated``, the CONNECT-stream FIN, and asyncio
            ``connection_lost`` — can all funnel through here safely.
            """
            if self._wt_transport is not None:
                self._wt_transport._mark_connection_lost()

        def _handle_h3_event(self, event: Any) -> None:
            assert self._h3 is not None
            if isinstance(event, h3_events.HeadersReceived):
                self._handle_headers(event)
            elif isinstance(event, h3_events.WebTransportStreamDataReceived):
                if self._wt_transport is None:
                    return
                if event.session_id != self._accepted_session_id:
                    # Stream data targeting a different WebTransport session
                    # on this QUIC connection (e.g. a stream opened against a
                    # CONNECT we rejected with 409).  Never feed another
                    # session's bytes into the one accepted session.
                    logger.warning(
                        "Ignoring WebTransport stream %d for session %s (accepted session is %s)",
                        event.stream_id,
                        event.session_id,
                        self._accepted_session_id,
                    )
                    return
                self._wt_transport._feed_stream_data(
                    event.stream_id, event.data, event.stream_ended
                )
            elif isinstance(event, h3_events.DataReceived):  # noqa: SIM102 nested branches preserve decision context
                # A browser ``transport.close()`` closes the WebTransport
                # session by FINning the CONNECT stream; aioquic surfaces that
                # as a ``DataReceived`` with ``stream_ended`` on the CONNECT /
                # session stream id (the same id as the accepted session).
                # This does not go through ``connection_lost()`` either, so
                # without handling it here ``wait_closed()`` hangs and the
                # session slot leaks until the QUIC idle timeout.
                if (
                    event.stream_ended
                    and self._accepted_session_id is not None
                    and event.stream_id == self._accepted_session_id
                ):
                    self._mark_session_lost()

        def _handle_headers(self, event: Any) -> None:
            assert self._h3 is not None
            headers = dict(event.headers)
            method = headers.get(b":method", b"").decode("ascii", errors="ignore")
            protocol = headers.get(b":protocol", b"").decode("ascii", errors="ignore")
            path = headers.get(b":path", b"").decode("ascii", errors="ignore")
            try:
                request_path = urlsplit(path).path
            except ValueError:
                self._h3.send_headers(
                    event.stream_id,
                    [(b":status", b"400")],
                    end_stream=True,
                )
                self.transmit()
                return

            if method != "CONNECT" or protocol != "webtransport":
                self._h3.send_headers(event.stream_id, [(b":status", b"400")], end_stream=True)
                self.transmit()
                return

            auth_policy = getattr(self, "_auth_policy", None)
            if auth_policy is not None:
                from easycat.server.auth import from_h3_headers

                auth_result = auth_policy.authorize(from_h3_headers(event.headers, path))
                if not auth_result.allowed:
                    logger.warning(
                        "Rejecting WebTransport CONNECT — bearer authentication %s",
                        auth_result.reason,
                    )
                    self._h3.send_headers(
                        event.stream_id,
                        [(b":status", b"401"), (b"www-authenticate", b"Bearer")],
                        end_stream=True,
                    )
                    self.transmit()
                    return

            if request_path != self._accept_path:
                self._h3.send_headers(event.stream_id, [(b":status", b"404")], end_stream=True)
                self.transmit()
                return

            if event.stream_ended:
                # A valid WebTransport CONNECT keeps its stream open for the
                # lifetime of the session.  If the HEADERS arrive with
                # END_STREAM set the client has already half-closed the
                # CONNECT stream, and aioquic surfaces that *only* here (no
                # later ``DataReceived`` FIN ever fires for an empty
                # half-closed stream).  Accepting it would create a
                # transport whose ``wait_closed()`` never unblocks until the
                # QUIC idle timeout, pinning a session slot and letting a
                # malformed client exhaust ``max_concurrent_sessions``.
                # Reject before allocating any session resources.
                logger.warning("Rejecting WebTransport CONNECT — HEADERS arrived with END_STREAM")
                self._h3.send_headers(event.stream_id, [(b":status", b"400")], end_stream=True)
                self.transmit()
                return

            if self._wt_transport is not None:
                # Reject additional WT sessions on the same QUIC connection.
                self._h3.send_headers(event.stream_id, [(b":status", b"409")], end_stream=True)
                self.transmit()
                return

            if not self._can_accept():
                # At the concurrent-session cap.  Reject *before* the 200 so
                # the client sees a clean rejection rather than an accepted
                # session that is force-closed a moment later.  No transport
                # is created and ``_on_session`` is not called, so this
                # connection holds no session resources (only QUIC idle
                # timeout bounds it).
                logger.warning("Rejecting WebTransport CONNECT — session cap reached")
                self._h3.send_headers(event.stream_id, [(b":status", b"503")], end_stream=True)
                self.transmit()
                return

            self._h3.send_headers(
                event.stream_id,
                [(b":status", b"200"), (b"sec-webtransport-http3-draft", b"draft02")],
                end_stream=False,
            )
            self.transmit()

            transport = WebTransportConnectionTransport(
                config=self._session_config,
                _h3=self._h3,
                _quic_protocol=self,
                _session_id=event.stream_id,
            )
            self._wt_transport = transport
            self._accepted_session_id = event.stream_id
            self._on_session(transport)

        def connection_lost(self, exc: BaseException | None) -> None:
            self._mark_session_lost()
            super().connection_lost(exc)

    _PROTOCOL_CLASS_CACHE = _EasyCatH3Protocol
    return _EasyCatH3Protocol


def _protocol_factory(
    *,
    accept_path: str,
    on_session: Callable[[WebTransportConnectionTransport], None],
    can_accept: Callable[[], bool],
    session_config: WebTransportTransportConfig,
    auth_policy: AuthPolicy | None,
) -> Callable[..., Any]:
    """Build the ``create_protocol`` callable for :func:`aioquic.asyncio.serve`."""

    protocol_cls = _get_protocol_class()

    def factory(*args: Any, **kwargs: Any) -> Any:
        proto = protocol_cls(*args, **kwargs)
        proto._accept_path = accept_path
        proto._on_session = on_session
        proto._can_accept = can_accept
        proto._session_config = session_config
        proto._auth_policy = auth_policy
        return proto

    return factory


def _preflight_aioquic_backpressure_api() -> None:
    """Fail startup when aioquic no longer exposes the bounded-writer probe.

    The probe uses a disposable client-side ``QuicConnection`` to create one
    real stream, then resolves the exact private access path used by live
    WebTransport sessions. No socket is opened and no traffic is sent.
    """
    configuration_module = require_module(
        "aioquic.quic.configuration",
        extra="webtransport",
        purpose="WebTransport transport",
    )
    connection_module = require_module(
        "aioquic.quic.connection",
        extra="webtransport",
        purpose="WebTransport transport",
    )
    try:
        configuration = configuration_module.QuicConfiguration(is_client=True)
        quic = connection_module.QuicConnection(configuration=configuration)
        protocol = _get_protocol_class()(quic)
        stream_id = quic.get_next_available_stream_id()
        quic.send_stream_data(stream_id, b"\0")
        raw_buffer, incompatibility = _read_aioquic_send_buffer(protocol, stream_id)
        if incompatibility is not None:
            _raise_aioquic_backpressure_incompatible(incompatibility)
        if raw_buffer is None:
            _raise_aioquic_backpressure_incompatible(
                "probe stream missing after QuicConnection.send_stream_data"
            )
    except _AioquicBackpressureCompatibilityError:
        raise
    except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
        _raise_aioquic_backpressure_incompatible(
            f"compatibility probe raised {type(exc).__name__}"
        )


# ── Per-session transport ──────────────────────────────────────────


class WebTransportConnectionTransport(AudioQueueMixin):
    """Per-session :class:`~easycat.providers.Transport`.

    Normally yielded to your config factory by
    :func:`easycat.server.run_webtransport_config_server` /
    :func:`easycat.server.serve_webtransport_config_sessions` or to your lower-level handler
    by :class:`WebTransportServer`.  You can also construct one directly if
    you're managing your own aioquic server — pass the H3Connection, the
    QuicConnectionProtocol, and the CONNECT stream id via the
    underscore-prefixed kwargs.
    """

    transport_kind = "webtransport"
    default_echo_cancellation_enabled = False

    def __init__(
        self,
        *,
        config: WebTransportTransportConfig | None = None,
        _h3: H3Connection | None = None,
        _quic_protocol: QuicConnectionProtocol | None = None,
        _session_id: int | None = None,
    ) -> None:
        self._config = config or WebTransportTransportConfig()
        self._audio_format = self._config.audio_format
        self._init_audio_queue(
            self._config.max_pending_chunks,
            self._config.max_pending_bytes,
        )
        self._out_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=self._config.outbound_max_pending,
        )
        self._on_close = asyncio.Event()
        self._writer_tasks = RuntimeTaskScope(
            owner_label="webtransport-connection-writer",
            member_name=_WEBTRANSPORT_WRITER_TASK_NAME,
            cohort=_WEBTRANSPORT_WRITER_COHORT,
            logger=logger,
            failure_message="WebTransport outbound writer failed",
            drop_if_closed=False,
        )
        # Cleanup ownership survives the public connected-state flip. A failed
        # provider/session stop must remain retryable instead of making the next
        # disconnect silently return.
        self._session_stop_pending = False
        self._connection_close_pending = False
        self._disconnect_cleanup_error: Exception | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        # ``_event_bus`` / ``_emit_tasks`` / ``_emit_degraded`` come from
        # ``AudioQueueMixin`` (initialised by ``_init_audio_queue`` above).
        # Session attaches the bus post-construction via
        # ``_maybe_attach_event_bus``; the session built below is handed the
        # bound ``_emit_degraded`` and reads the bus live at emit time.
        if _h3 is None or _quic_protocol is None or _session_id is None:
            self._session: _WebTransportSession | None = None
            self._needs_external_session = True
        else:
            self._session = _WebTransportSession(
                h3=_h3,
                quic_protocol=_quic_protocol,
                session_id=_session_id,
                target_sample_rate=self._audio_format.sample_rate,
                audio_format=self._audio_format,
                in_queue=self._in_queue,
                out_queue=self._out_queue,
                on_close=self._on_close,
                emit_degraded=self._emit_degraded,
                writer_tasks=self._writer_tasks,
            )
            self._needs_external_session = False

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach writer and event work to the owning transport scope."""
        super().set_runtime_scope(parent, name=name)
        scope = self._emit_scope
        assert scope is not None
        self._writer_tasks.bind(scope)

    def _bind_runtime_scope(self, scope: RuntimeScope) -> None:
        """Share an existing transport child with a wrapper owner."""
        self._event_tasks.bind(scope)
        self._writer_tasks.bind(scope)

    # ── Transport protocol ────────────────────────────────────────

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def connect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "connect":
                return
            raise RuntimeError(
                "WebTransportConnectionTransport.connect() cannot run during disconnect()"
            )
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
                "WebTransport connection cleanup is incomplete; call disconnect() "
                "again before reconnecting"
            ) from self._disconnect_cleanup_error
        if self._session is None:
            raise RuntimeError(
                "WebTransportConnectionTransport has no underlying session. "
                "Use WebTransportServer or pass _h3/_quic_protocol/_session_id."
            )
        # Do NOT reset the inbound queue here.  This transport is built
        # fresh per accepted CONNECT session, and the aioquic protocol can
        # feed early mic frames via ``_feed_stream_data`` into ``_in_queue``
        # before this coroutine — scheduled as a task by the server — runs.
        # Resetting would discard the start of the user's first utterance.
        # There is no stale per-session state to clear (a fresh queue was
        # created in ``__init__``; sentinels are only enqueued at teardown).
        while not self._out_queue.empty():
            try:
                self._out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._session_stop_pending = True
        self._connection_close_pending = True
        try:
            await self._session.start()
        except BaseException as startup_error:
            settlement = await shielded_cleanup(
                self._disconnect_unlocked,
            )
            cancellation = (
                asyncio.CancelledError()
                if settlement.cancellation_requests
                and not isinstance(startup_error, asyncio.CancelledError)
                else None
            )
            if settlement.error is not None:
                if self._disconnect_cleanup_error is None:
                    self._disconnect_cleanup_error = RuntimeError(
                        "WebTransport connect rollback was interrupted"
                    )
                _raise_rollback_cancellation(cancellation, startup_error, settlement.error)
                raise startup_error from settlement.error
            _raise_rollback_cancellation(cancellation, startup_error)
            raise
        self._connected = True
        self._client_connected.set()

    async def disconnect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "disconnect":
                return
            raise RuntimeError(
                "WebTransportConnectionTransport.disconnect() cannot run during connect()"
            )
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
        if (
            not self._connected
            and not self._session_stop_pending
            and not self._connection_close_pending
            and not self._emit_tasks
            and self._disconnect_cleanup_error is None
        ):
            return
        cleanup_errors: list[Exception] = []
        self._connected = False
        self._client_connected.clear()
        session = self._session
        if session is not None and self._session_stop_pending:
            try:
                await session.stop()
            except Exception as exc:
                logger.exception("WebTransport session cleanup failed", exc_info=exc)
                cleanup_errors.append(exc)
            else:
                self._session_stop_pending = False
        if session is not None and self._connection_close_pending:
            try:
                # Actively tear the QUIC connection down so a server-initiated
                # end-of-session reaches the client immediately rather than
                # lingering until the idle timeout.
                session.close_connection(reason="session ended")
            except Exception as exc:
                logger.exception("WebTransport connection close failed", exc_info=exc)
                cleanup_errors.append(exc)
            else:
                self._connection_close_pending = False
        self._enqueue_sentinel()
        self._enqueue_out_sentinel()
        self._on_close.set()
        try:
            await self._drain_emit_tasks()
        except Exception as exc:
            logger.exception("WebTransport diagnostic cleanup failed", exc_info=exc)
            cleanup_errors.append(exc)
        self._disconnect_cleanup_error = cleanup_errors[0] if cleanup_errors else None
        if cleanup_errors:
            raise cleanup_errors[0]

    def _publish_interrupted_disconnect(self) -> None:
        """Retain retry ownership before preserving caller cancellation."""
        self._connected = False
        self._client_connected.clear()
        self._enqueue_sentinel()
        self._enqueue_out_sentinel()
        self._on_close.set()
        self._disconnect_cleanup_error = RuntimeError(
            "WebTransport disconnect was interrupted by cancellation"
        )

    def force_close(self, *, reason: str = "") -> None:
        """Actively terminate the QUIC connection, even before ``connect()``.

        :meth:`disconnect` early-returns when ``_connected`` is False, so it
        cannot tear down a session that was accepted at the HTTP/3 layer but
        never handed to a handler (e.g. one rejected by the
        ``max_concurrent_sessions`` cap).  This sends CONNECTION_CLOSE so the
        over-cap connection is released immediately instead of lingering
        until its idle timeout.  Safe to call regardless of connect state and
        idempotent (the eventual ``connection_lost`` is a no-op once closed).
        """
        self._connected = False
        self._client_connected.clear()
        try:
            if self._session is not None:
                self._session.close_connection(reason=reason)
        finally:
            self._enqueue_sentinel()
            self._enqueue_out_sentinel()
            self._on_close.set()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if not self._connected:
            return False
        # Keep one application queue slot and one aioquic write bounded to a
        # stream window.  Splitting here (rather than while the writer holds a
        # chunk) preserves its no-await get→send invariant, so ``clear_audio``
        # can still synchronously discard every not-yet-written fragment.
        fragment_count = max(
            1,
            (len(chunk.data) + _OUTBOUND_SEND_WRITE_MAX_BYTES - 1)
            // _OUTBOUND_SEND_WRITE_MAX_BYTES,
        )
        available_slots = self._out_queue.maxsize - self._out_queue.qsize()
        if fragment_count > available_slots:
            logger.debug("WebTransport outbound queue full — dropping TTS frame")
            self._emit_degraded(
                _DEGRADED_OUTBOUND_QUEUE_FULL,
                f"dropped {len(chunk.data)}-byte TTS frame; outbound queue full",
            )
            return False
        try:
            if not chunk.data:
                self._out_queue.put_nowait(chunk)
            else:
                for offset in range(0, len(chunk.data), _OUTBOUND_SEND_WRITE_MAX_BYTES):
                    self._out_queue.put_nowait(
                        replace(
                            chunk,
                            data=chunk.data[offset : offset + _OUTBOUND_SEND_WRITE_MAX_BYTES],
                        )
                    )
            return True
        except asyncio.QueueFull:
            # ``send_audio`` has no suspension points, so the capacity check
            # above is atomic with the puts.  Keep this defensive fallback for
            # alternative queue implementations.
            logger.debug("WebTransport outbound queue full — dropping TTS frame")
            self._emit_degraded(
                _DEGRADED_OUTBOUND_QUEUE_FULL,
                f"dropped {len(chunk.data)}-byte TTS frame; outbound queue full",
            )
            return False

    async def clear_audio(self) -> None:
        drained = 0
        while True:
            try:
                self._out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            drained += 1
        # Aborting the QUIC audio stream is what actually stops the client
        # from hearing already-handed-off bytes — draining the app queue
        # alone leaves up to ``max_stream_data`` (~2 s @ 16 kHz) buffered.
        if self._session is not None:
            self._session.reset_audio_stream()
        if drained:
            logger.debug("Cleared %d pending WebTransport TTS frames", drained)

    # ── Lifetime helpers used by the server ───────────────────────

    async def wait_closed(self, timeout: float | None = None) -> None:
        """Block until the underlying QUIC connection terminates."""
        if timeout is None:
            await self._on_close.wait()
        else:
            await asyncio.wait_for(self._on_close.wait(), timeout=timeout)

    def _feed_stream_data(self, stream_id: int, data: bytes, ended: bool) -> None:
        if self._session is not None:
            self._session.handle_stream_data(stream_id, data, ended)

    def _enqueue_out_sentinel(self) -> None:
        """Put the ``None`` writer sentinel on the outbound queue, making
        room if it is full.

        The sentinel is what lets
        :meth:`_WebTransportSession._outbound_writer` exit; a full
        ``_out_queue`` (e.g. a stalled client) must not be allowed to
        swallow it, otherwise the writer wedges.  Mirrors
        :meth:`AudioQueueMixin._enqueue_sentinel`.
        """
        try:
            self._out_queue.put_nowait(None)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._out_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self._out_queue.put_nowait(None)
        except asyncio.QueueFull:
            logger.debug("Outbound queue full when enqueueing writer sentinel; ignoring")

    def _mark_connection_lost(self) -> None:
        # The QUIC connection is gone: bytes can no longer reach the peer,
        # so mark this transport disconnected.  Leaving ``_connected`` True
        # would let ``send_audio()`` keep returning True and enqueuing TTS
        # that can never be delivered, and would leave handlers that watch
        # transport state wedged until a later explicit ``disconnect()``.
        self._connected = False
        self._client_connected.clear()
        # The peer is already gone, but the local writer/session task still
        # needs its normal async stop when the handler reaches disconnect().
        self._connection_close_pending = False
        self._on_close.set()
        # Unblock receive_audio() and the outbound writer.
        self._enqueue_sentinel()
        self._enqueue_out_sentinel()

    # ``_emit_degraded`` is inherited from ``AudioQueueMixin`` — it reads
    # ``self._event_bus`` live and tags events with ``transport_kind``.

    def version_info(self) -> dict[str, str]:
        return make_version_info("webtransport-connection", "aioquic", api_version="h3")


# ── Multi-client server ────────────────────────────────────────────


class WebTransportServer:
    """Multi-client WebTransport server.

    Wraps :func:`aioquic.asyncio.serve` and dispatches each accepted
    WebTransport session to ``session_handler``.  Each handler invocation
    receives a fresh :class:`WebTransportConnectionTransport` ready to be
    passed to :func:`~easycat.create_session`.

    Example::

        async def handle(transport: WebTransportConnectionTransport) -> None:
            session = create_session(EasyConfig(transport=transport, agent=...))
            async with manager.connection(id(transport), session, runtime_feedback=True):
                await transport.wait_closed()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            handle,
        )
        await server.start()
        await stop_event.wait()
        await server.stop()
    """

    def __init__(
        self,
        config: WebTransportTransportConfig,
        session_handler: _SessionHandler,
    ) -> None:
        self._config = config
        self._session_handler = session_handler
        self._server: QuicServer | None = None
        # A cancellation-resistant ``wait_closed()`` must remain owned by this
        # exact bound-server generation. Retrying it through a fresh call can
        # overlap two listener waiters against the same aioquic server.
        self._server_wait_closed_task: asyncio.Task[object] | None = None
        self._handler_task_scope = RuntimeTaskScope(
            owner_label="webtransport-server-handlers",
            member_name=_WEBTRANSPORT_HANDLER_TASK_NAME,
            cohort=_WEBTRANSPORT_HANDLER_COHORT,
            logger=logger,
            failure_message="WebTransport session handler failed",
            drop_if_closed=False,
        )
        self._listener_task_scope = RuntimeTaskScope(
            owner_label="webtransport-server-listener",
            member_name=_WEBTRANSPORT_LISTENER_TASK_NAME,
            cohort=_WEBTRANSPORT_LISTENER_COHORT,
            logger=logger,
            failure_message="WebTransport listener close failed",
            drop_if_closed=False,
        )
        self._detached_listener_task_scope = RuntimeTaskScope(
            owner_label="webtransport-detached-listener",
            member_name=_WEBTRANSPORT_LISTENER_TASK_NAME,
            cohort=_WEBTRANSPORT_LISTENER_COHORT,
            logger=logger,
            failure_message="Detached WebTransport listener close failed",
            drop_if_closed=False,
        )
        # A handler task can finish after its transport's disconnect fails.
        # Keep the exact transport reachable so stop() can retry that owned
        # cleanup instead of discarding it with the completed handler task.
        self._pending_transport_cleanup: set[WebTransportConnectionTransport] = set()
        self._started = False
        # Protocol admission is distinct from the bound-server handle: stop()
        # closes it synchronously before snapshotting handlers, so a queued
        # post-accept callback cannot create work after that ownership cut.
        self._accepting_sessions = False
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        self._cleanup_error: Exception | None = None
        token = normalize_auth_token(config.auth_token)
        if token is None:
            self._auth_policy: AuthPolicy | None = None
        else:
            from easycat.server.auth import BearerTokenAuth

            self._auth_policy = BearerTokenAuth(
                token=token,
                allow_query_token=config.allow_query_token,
            )
        # Per-session transports need media settings, never the server secret.
        self._session_config = replace(config, auth_token=None)

    @property
    def _handler_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility view of currently owned session-handler tasks."""
        return set(self._handler_task_scope.tasks())

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach session-handler work beneath an application lifecycle."""
        self._handler_task_scope.attach(parent, name=name)
        scope = self._handler_task_scope.scope
        assert scope is not None
        self._listener_task_scope.bind(scope)

    async def _release_standalone_task_scopes(self) -> None:
        """Close empty standalone roots used by handlers and listener wait."""
        await self._listener_task_scope.release_standalone_if_empty()
        await self._detached_listener_task_scope.release_standalone_if_empty()
        await self._handler_task_scope.release_standalone_if_empty()

    def _bind_runtime_scope(self, scope: RuntimeScope) -> None:
        """Share an already-created handler scope owned by a wrapper."""
        self._handler_task_scope.bind(scope)
        self._listener_task_scope.bind(scope)

    def _can_accept_session(self) -> bool:
        """Capacity gate consulted by the protocol *before* it sends the 200.

        Single-threaded event loop + synchronous CONNECT handling means the
        check and the subsequent ``_dispatch_session`` task creation are
        atomic relative to each other (no TOCTOU).
        """
        return (
            self._started
            and self._accepting_sessions
            and len(self._handler_tasks) + len(self._pending_transport_cleanup)
            < self._config.max_concurrent_sessions
        )

    def _dispatch_session(self, transport: WebTransportConnectionTransport) -> None:
        """Accept a new session or reject it when the concurrency cap is hit.

        Invoked synchronously from the aioquic protocol when a CONNECT-
        webtransport handshake completes.  The protocol already gates on
        :meth:`_can_accept_session` *before* the 200, so a healthy path never
        reaches the cap branch below — it is kept purely as defense-in-depth
        (and for the direct unit test) in case this is ever driven without
        the pre-200 check.
        """
        if not self._started or not self._accepting_sessions:
            logger.warning("Rejecting WebTransport session — server is not accepting sessions")
            transport.force_close(reason="server not accepting sessions")
            return
        owned_sessions = len(self._handler_tasks) + len(self._pending_transport_cleanup)
        if owned_sessions >= self._config.max_concurrent_sessions:
            logger.warning(
                "Rejecting WebTransport session — %d concurrent cap reached",
                self._config.max_concurrent_sessions,
            )
            # ``disconnect()`` is a no-op pre-``connect()`` (it early-returns
            # on ``_connected is False``), so it would leave the over-cap
            # connection alive until idle timeout.  Force a CONNECTION_CLOSE
            # now so the cap is actually enforced.
            transport.force_close(reason="session cap reached")
            return
        task = self._handler_task_scope.create_task(
            self._run_handler(transport),
            task_name=f"webtransport-session-{id(transport)}",
        )
        assert task is not None

    async def start(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "start":
                return
            raise RuntimeError("WebTransportServer.start() cannot run reentrantly during stop()")
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
        if self._cleanup_error is not None:
            raise RuntimeError(
                "WebTransportServer cannot start because previous cleanup is "
                "incomplete; call stop() again to retry cleanup"
            ) from self._cleanup_error
        if self._started:
            return
        if (
            self._server is not None
            or self._server_wait_closed_task is not None
            or self._handler_tasks
            or self._pending_transport_cleanup
        ):
            raise RuntimeError(
                "WebTransportServer cannot start while previous resources "
                "remain; call stop() again to retry cleanup"
            )
        self._accepting_sessions = False
        from easycat.server.auth import authorized_bind, enforce_bind_guard

        enforce_bind_guard(
            self._config.host,
            auth=self._auth_policy,
            unsafe_allow_no_auth=self._config.unsafe_allow_no_auth,
        )
        quic_config = _build_quic_configuration(self._config.certfile, self._config.keyfile)
        _preflight_aioquic_backpressure_api()

        factory = _protocol_factory(
            accept_path=self._config.path,
            on_session=self._dispatch_session,
            can_accept=self._can_accept_session,
            session_config=self._session_config,
            auth_policy=self._auth_policy,
        )
        aioquic_server = require_module(
            "aioquic.asyncio.server",
            extra="webtransport",
            purpose="WebTransport transport",
        )
        self._server = await authorized_bind(
            self._config.host,
            auth=self._auth_policy,
            unsafe_allow_no_auth=self._config.unsafe_allow_no_auth,
            binder=lambda bind_host: aioquic_server.serve(
                bind_host,
                self._config.port,
                configuration=quic_config,
                create_protocol=factory,
            ),
        )
        self._started = True
        self._accepting_sessions = True
        logger.info(
            "WebTransport server listening on https://%s:%d%s",
            self._config.host,
            self._config.port,
            self._config.path,
        )

    async def _run_handler(self, transport: WebTransportConnectionTransport) -> None:
        try:
            await transport.connect()
            await self._session_handler(transport)
        except Exception:
            logger.exception("WebTransport session handler raised")
        finally:
            try:
                await transport.disconnect()
            except asyncio.CancelledError as exc:
                self._record_handler_cleanup_failure(transport, exc)
                raise
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                self._record_handler_cleanup_failure(transport, exc)

    def _record_handler_cleanup_failure(
        self,
        transport: WebTransportConnectionTransport,
        exc: Exception | asyncio.CancelledError,
    ) -> None:
        """Retain retryable handler cleanup without swallowing process control."""
        # A completed handler task is removed from _handler_tasks, so retain
        # the exact transport separately until server stop can retry it.
        self._pending_transport_cleanup.add(transport)
        cleanup_error = transport._disconnect_cleanup_error
        if cleanup_error is None:
            cleanup_error = (
                exc
                if isinstance(exc, Exception)
                else RuntimeError(
                    "WebTransport handler disconnect was interrupted by cancellation"
                )
            )
        if self._cleanup_error is None:
            self._cleanup_error = cleanup_error
        logger.debug("Error while disconnecting WebTransport session", exc_info=exc)

    async def stop(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "stop":
                return
            raise RuntimeError("WebTransportServer.stop() cannot run reentrantly during start()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "stop"
            try:
                try:
                    await self._stop_unlocked()
                except asyncio.CancelledError:
                    self._started = False
                    self._cleanup_error = RuntimeError(
                        "WebTransportServer stop was interrupted by cancellation"
                    )
                    raise
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _retry_pending_transport_cleanup(self) -> list[Exception]:
        """Retry exact transports retained after handler disconnect failure."""
        cleanup_errors: list[Exception] = []
        for transport in list(self._pending_transport_cleanup):
            try:
                await transport.disconnect()
            except asyncio.CancelledError:
                # The connection transport retains its own interrupted-cleanup
                # ledger. Keep it in the server ledger and preserve caller
                # cancellation through stop().
                raise
            except Exception as exc:
                logger.exception(
                    "WebTransport retained session cleanup failed",
                    exc_info=exc,
                )
                cleanup_errors.append(exc)
            else:
                self._pending_transport_cleanup.discard(transport)
        return cleanup_errors

    async def _cancel_and_reap_handler_tasks(self) -> list[Exception]:
        """Cancel handlers without letting an uncooperative one wedge ``stop``.

        Keep a task that outlives the hard shutdown budget in
        ``_handler_tasks``. That ownership blocks restart and lets a later
        ``stop()`` retry cancellation after the user handler becomes
        cooperative.
        """
        current = asyncio.current_task()
        tasks = [task for task in self._handler_task_scope.tasks() if task is not current]
        for task in tasks:
            task.cancel()
        if not tasks:
            return []
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(self._config.force_shutdown_timeout_s, 0.0),
        )
        if done:
            # Reap completed tasks so an exception that races teardown does
            # not become an unobserved task exception.
            await asyncio.gather(*done, return_exceptions=True)
            for task in done:
                self._handler_task_scope.discard_task(task)
        if not pending:
            return []
        timeout_error = RuntimeError(
            "WebTransport session handler(s) did not stop within "
            f"force_shutdown_timeout_s={self._config.force_shutdown_timeout_s}s"
        )
        logger.warning("WebTransport server: %s", timeout_error)
        return [timeout_error]

    async def _wait_for_server_close(self, wait_closed: Callable[[], Awaitable[object]]) -> bool:
        """Await one listener-close task at a time across ``stop()`` retries."""
        task = self._server_wait_closed_task
        if task is not None and task.done():
            self._server_wait_closed_task = None
            if not task.cancelled():
                try:
                    task.result()
                except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                    # The preceding stop already surfaced this listener
                    # failure. It is now safe to make one fresh retry because
                    # the old waiter is complete, not concurrent.
                    pass
                else:
                    self._listener_task_scope.discard_task(task)
                    self._detached_listener_task_scope.discard_task(task)
                    await self._listener_task_scope.release_standalone_if_empty()
                    await self._detached_listener_task_scope.release_standalone_if_empty()
                    return True
            task = None
        if task is None:
            handler_scope = self._handler_task_scope.scope
            if handler_scope is not None and self._listener_task_scope.scope is None:
                self._listener_task_scope.bind(handler_scope)

            async def await_listener_close() -> object:
                return await wait_closed()

            task = self._listener_task_scope.create_task(
                await_listener_close(),
                task_name="webtransport-listener-close",
            )
            assert task is not None
            self._server_wait_closed_task = task
        closed = await _await_with_hard_timeout(
            task,
            timeout_s=self._config.force_shutdown_timeout_s,
        )
        if closed:
            self._server_wait_closed_task = None
            self._listener_task_scope.discard_task(task)
            self._detached_listener_task_scope.discard_task(task)
            await self._listener_task_scope.release_standalone_if_empty()
            await self._detached_listener_task_scope.release_standalone_if_empty()
        elif not self._listener_task_scope.owns_root:
            self._listener_task_scope.discard_task(task)
            self._detached_listener_task_scope.adopt_task(task)
        return closed

    async def _stop_unlocked(self) -> None:
        """Stop while the caller owns ``_lifecycle_lock``."""
        # Close admission before inspecting/snapshotting handlers. Protocol
        # callbacks are synchronous on this event loop, so every dispatch after
        # this line observes rejection and force-closes its accepted transport.
        self._accepting_sessions = False
        if (
            not self._started
            and self._server is None
            and self._server_wait_closed_task is None
            and not self._handler_tasks
            and not self._pending_transport_cleanup
            and self._cleanup_error is None
        ):
            await self._release_standalone_task_scopes()
            return
        self._started = False
        # Tear down in-flight handlers, but never await the current task
        # (which can happen if a handler calls back into ``stop()``). A
        # user handler can catch cancellation, so retain a survivor rather
        # than waiting forever or losing its cleanup ownership.
        cleanup_errors = await self._cancel_and_reap_handler_tasks()
        cleanup_errors.extend(await self._retry_pending_transport_cleanup())
        server = self._server
        server_cleanup_errors: list[Exception] = []
        if server is not None:
            try:
                server.close()
            except Exception as exc:
                logger.exception("WebTransport server close failed", exc_info=exc)
                server_cleanup_errors.append(exc)
            wait_closed = getattr(server, "wait_closed", None)
            if wait_closed is not None:
                try:
                    closed = await self._wait_for_server_close(wait_closed)
                except Exception as exc:
                    logger.exception("WebTransport server wait_closed failed", exc_info=exc)
                    server_cleanup_errors.append(exc)
                else:
                    if not closed:
                        timeout_error = RuntimeError(
                            "WebTransport listener did not close within "
                            "force_shutdown_timeout_s="
                            f"{self._config.force_shutdown_timeout_s}s"
                        )
                        logger.warning("WebTransport server: %s", timeout_error)
                        server_cleanup_errors.append(timeout_error)
            if not server_cleanup_errors and self._server is server:
                self._server = None
        cleanup_errors.extend(server_cleanup_errors)
        self._cleanup_error = cleanup_errors[0] if cleanup_errors else None
        if cleanup_errors:
            raise cleanup_errors[0]
        await self._release_standalone_task_scopes()

    async def serve_forever(self) -> None:
        """Convenience: start the server and block until cancelled."""
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()


# ── Single-client convenience wrapper ─────────────────────────────


class WebTransportTransport(AudioQueueMixin):
    """Single-client server :class:`~easycat.providers.Transport`.

    Parallels :class:`~easycat.transports.websocket.WebSocketTransport`'s
    shape: implements the Transport protocol directly, accepts at most one
    client.  Internally hosts a :class:`WebTransportServer` with a one-shot
    handler; once a client connects, ``send_audio`` / ``clear_audio`` /
    ``receive_audio`` delegate straight to the per-session
    :class:`WebTransportConnectionTransport` — no extra buffering between
    this outer transport and the inner session.

    For multi-client deployments, prefer
    :func:`easycat.server.serve_webtransport_config_sessions`; drop to
    :class:`WebTransportServer` only when you need custom per-client
    orchestration beyond returning an ``EasyConfig``.
    """

    transport_kind = "webtransport"
    default_echo_cancellation_enabled = False

    def __init__(self, config: WebTransportTransportConfig | None = None) -> None:
        self._config = config or WebTransportTransportConfig()
        self._audio_format = self._config.audio_format
        # We don't push into the mixin's ``_in_queue`` (``receive_audio``
        # below delegates), but ``_init_audio_queue`` also sets up
        # ``_connected`` and ``_client_connected`` which we do use.
        self._init_audio_queue(
            self._config.max_pending_chunks,
            self._config.max_pending_bytes,
        )
        self._server: WebTransportServer | None = None
        self._server_runtime_scope: RuntimeScope | None = None
        self._active: WebTransportConnectionTransport | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        # ``_event_bus`` comes from ``AudioQueueMixin`` (``_init_audio_queue``
        # above).  Session attaches it post-construction
        # (``_maybe_attach_event_bus``); ``connect``'s ``handle`` closure
        # forwards it to the inner per-session transport so degraded events
        # still reach the journal in the single-client path.

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    def _bind_connection_runtime(self, transport: WebTransportConnectionTransport) -> None:
        """Attach an accepted connection to this wrapper's transport child."""
        scope = self._emit_scope
        if scope is not None:
            transport._bind_runtime_scope(scope)

    def _bind_server_runtime(self, server: WebTransportServer) -> None:
        """Attach the internal server's handler work to this transport."""
        scope = self._emit_scope
        if scope is not None:
            server_scope = self._server_runtime_scope
            if server_scope is None:
                server_scope = scope.create_child("webtransport-server-runtime")
                self._server_runtime_scope = server_scope
            server._bind_runtime_scope(server_scope)

    async def connect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "connect":
                return
            raise RuntimeError("WebTransportTransport.connect() cannot run during disconnect()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "connect"
            try:
                await self._connect_unlocked()
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _connect_unlocked(self) -> None:
        """Connect while the wrapper lifecycle lock is held."""
        if self._connected:
            return
        if self._server is not None:
            if (
                self._server._cleanup_error is not None
                or self._server._server is not None
                or self._server._started
            ):
                raise RuntimeError(
                    "WebTransport server cleanup is incomplete; call disconnect() "
                    "again before reconnecting"
                )
            self._server = None
        self._reset_audio_queue()
        # If a previous run set the event, clear it so receive_audio waits
        # for *this* run's client.
        self._client_connected.clear()

        async def handle(transport: WebTransportConnectionTransport) -> None:
            if self._active is not None:
                logger.warning(
                    "Rejecting additional WebTransport client (only one session supported)"
                )
                return
            self._bind_connection_runtime(transport)
            self._active = transport
            # Forward the (late-attached) session bus so the inner session's
            # drop/poison/abort conditions are journaled in this path too.
            transport._event_bus = self._event_bus
            self._client_connected.set()
            try:
                await transport.wait_closed()
            finally:
                self._active = None
                # The client went away while we're still serving. Reset
                # the "client connected" signal so a later
                # ``wait_for_client()`` blocks for the *next* client and
                # ``receive_audio()`` doesn't wake up to a cleared
                # ``_active`` and return early. Skip this when
                # ``disconnect()`` is tearing us down: it deliberately
                # sets the event (with ``_connected`` already False) to
                # release waiters, and clearing it here would re-block them.
                if self._connected:
                    self._client_connected.clear()

        # Pin the wrapped server to a single session so an over-cap client is
        # rejected at accept time (the server force-closes it) instead of
        # lingering behind the one-session ``handle`` closure above.
        single_client_config = replace(self._config, max_concurrent_sessions=1)
        server = WebTransportServer(single_client_config, handle)
        self._bind_server_runtime(server)
        self._server = server
        try:
            await server.start()
        except BaseException:
            if (
                not server._started
                and server._server is None
                and server._cleanup_error is None
                and self._server is server
            ):
                self._server = None
            raise
        if self._server is not server:
            raise RuntimeError("WebTransport server ownership changed during connect")
        self._connected = True

    async def disconnect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "disconnect":
                return
            raise RuntimeError("WebTransportTransport.disconnect() cannot run during connect()")
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "disconnect"
            try:
                await self._disconnect_unlocked()
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _disconnect_unlocked(self) -> None:
        """Disconnect while the wrapper lifecycle lock is held."""
        if not self._connected and self._server is None:
            return
        self._connected = False
        # Unblock any ``receive_audio`` caller that is waiting for the first
        # client — they'll see ``_connected`` is False and exit cleanly.
        self._client_connected.set()
        server = self._server
        if server is not None:
            await server.stop()
            if self._server is server:
                self._server = None
        self._active = None

    async def send_audio(self, chunk: AudioChunk) -> bool:
        active = self._active
        if not self._connected or active is None:
            return False
        return await active.send_audio(chunk)

    async def clear_audio(self) -> None:
        active = self._active
        if active is not None:
            await active.clear_audio()

    async def receive_audio(self):
        """Yield inbound audio chunks once a client connects.

        Blocks on ``_client_connected`` until the first session arrives, then
        forwards directly from the inner connection transport — no
        intermediate queue.  Exits cleanly when the session ends or
        ``disconnect()`` runs before any client arrives.
        """
        await self._client_connected.wait()
        active = self._active
        if not self._connected or active is None:
            return
        async for chunk in active.receive_audio():
            yield chunk

    def version_info(self) -> dict[str, str]:
        return make_version_info("webtransport", "aioquic", api_version="h3")
