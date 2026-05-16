"""WebTransport (HTTP/3 over QUIC) transport.

Sits between :class:`~easycat.transports.websocket.WebSocketTransport` and
:class:`~easycat.transports.webrtc.WebRTCTransport`:

* vs. WebSocket: no cross-stream head-of-line blocking, faster handshake,
  connection migration when the client's IP changes.
* vs. WebRTC: a much smaller browser API (no SDP/ICE) and we keep PCM16
  end-to-end.

Three classes are exposed:

* :class:`WebTransportServer` — multi-client aioquic server.  Pass a
  ``session_handler`` coroutine and one is invoked per client with a
  fully-wired :class:`WebTransportConnectionTransport` ready to plug into
  ``create_session(...)``.  This is the recommended entry point.
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

``0x01`` — **audio stream** — raw PCM16 bytes (no further framing).
``0x02`` — **control stream** — repeated ``[4-byte BE length][UTF-8 JSON]`` frames.

The client opens two streams (audio + control) and writes mic PCM /
client-side control messages there.  The server, in turn, opens its own audio
and control streams via :meth:`H3Connection.create_webtransport_stream` and
writes TTS audio / server-side control messages there.  The browser
demultiplexes server-opened streams via ``incomingBidirectionalStreams`` and
reads the tag byte to dispatch.  Message shapes mirror
:class:`~easycat.transports.websocket.WebSocketTransport`:

* server→client: ``{"type":"ready"}``, ``{"type":"audio_format","sample_rate":N}``
* client→server: ``{"type":"config","sample_rate":N}``, ``{"type":"start"}``,
  ``{"type":"stop"}``

Loss behaviour (v1)
-------------------
All-reliable streams, no datagrams, no application NACK.  Within a single
stream, a packet loss costs ~1 RTT to recover (typically 30-100 ms), which is
what an application-level NACK round-trip would cost anyway.  The win over
WebSocket is that audio and control are independent QUIC streams: control
traffic never stalls audio (or vice versa), and each direction of a
bidirectional stream has independent flow control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.audio_utils import resample_chunk
from easycat.extras import require_module
from easycat.transports._base import _AudioQueueMixin
from easycat.transports.websocket import _valid_config_sample_rate

if TYPE_CHECKING:
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.asyncio.server import QuicServer
    from aioquic.h3.connection import H3Connection
    from aioquic.quic.configuration import QuicConfiguration

logger = logging.getLogger(__name__)

# Stream-purpose tags written as the first byte on each client-opened stream.
_TAG_AUDIO = 0x01
_TAG_CONTROL = 0x02

# Inbound (mic→server) queue size: prefer preserving user audio.  Higher than
# WebSocketTransport's 200.
_DEFAULT_INBOUND_MAX_PENDING = 500
# Outbound (TTS→client) queue size: still well above WebRTC defaults, but
# intentionally lower than inbound so we drop TTS more readily under pressure.
_DEFAULT_OUTBOUND_MAX_PENDING = 300

# Per-QUIC-stream flow-control window (~2 s of 16 kHz PCM16 audio).
_MAX_STREAM_DATA = 64 * 1024
# Voice turns can have multi-second silences between user/bot exchanges, so
# don't tear the QUIC connection down on short idle periods.
_IDLE_TIMEOUT_SEC = 30.0

# DoS bounds on the control framing layer.  A single JSON control frame is
# never larger than a few hundred bytes in practice; capping at 64 KiB lets us
# reject crafted length prefixes (a malicious uint32 can advertise up to 4 GB
# and pin app-side buffers indefinitely while bytes trickle in).
_MAX_CONTROL_FRAME_BYTES = 64 * 1024

# Cap on the number of streams whose purpose tag has not yet arrived.  A
# malicious client can open many bidi streams and never write the first byte;
# this bounds ``_pending_tags``.  No per-stream byte cap is needed — the tag
# is byte 0, so a stream is dispatched (and forgotten) the instant any byte
# arrives, and a single delivery is already bounded by the QUIC flow-control
# window (``_MAX_STREAM_DATA``).
_MAX_PENDING_TAG_STREAMS = 4

# Truncation cap for user-controlled values that end up in log messages.
_LOG_TRUNC = 64

_DEFAULT_PATH = "/easycat"

# Type alias for the user-supplied per-session handler. Module-private — not
# part of the public surface.
_SessionHandler = Callable[["WebTransportConnectionTransport"], Awaitable[None]]


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

    default_echo_cancellation_enabled: ClassVar[bool] = True

    host: str = "0.0.0.0"
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
    config.max_stream_data_bidi_local = _MAX_STREAM_DATA
    config.max_stream_data_bidi_remote = _MAX_STREAM_DATA
    config.max_stream_data_uni = _MAX_STREAM_DATA
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
            except (UnicodeDecodeError, json.JSONDecodeError):
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
    ) -> None:
        self._h3 = h3
        self._quic_protocol = quic_protocol
        self._session_id = session_id
        self._target_rate = target_sample_rate
        self._audio_format = audio_format
        self._inbound_format = audio_format
        self._outbound_rate: int | None = None
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._on_close = on_close

        # Client-opened stream ids (server reads from these halves).
        self._inbound_audio_stream_id: int | None = None
        self._inbound_control_stream_id: int | None = None
        # Server-initiated stream ids (server writes to these halves; the
        # client demultiplexes via its ``incomingBidirectionalStreams``).
        self._outbound_audio_stream_id: int | None = None
        self._outbound_control_stream_id: int | None = None
        self._control_codec = _ControlCodec()
        self._pending_tags: dict[int, bytearray] = {}
        self._writer_task: asyncio.Task[None] | None = None

    @property
    def session_id(self) -> int:
        return self._session_id

    async def start(self) -> None:
        self._writer_task = asyncio.create_task(self._outbound_writer())
        self._send_control({"type": "ready"})

    async def stop(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        self._writer_task = None

    def handle_stream_data(self, stream_id: int, data: bytes, ended: bool) -> None:
        if stream_id == self._inbound_audio_stream_id:
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
            if stream_id == self._inbound_audio_stream_id:
                self._inbound_audio_stream_id = None
            elif stream_id == self._inbound_control_stream_id:
                self._inbound_control_stream_id = None

    def _dispatch_untagged_stream(self, stream_id: int, data: bytes, ended: bool) -> None:
        """Identify a stream by its leading tag byte and route it.

        The purpose tag is always byte 0, so there is never a reason to
        accumulate bytes waiting for it — ``_pending_tags`` exists only to
        bridge zero-byte deliveries (an event with empty ``data`` before the
        first real byte).  As soon as any byte is present we dispatch the
        whole buffer (tag + however much payload arrived in the same event)
        and forget the stream.

        Memory is bounded without a per-stream byte cap: the number of
        not-yet-tagged streams is capped by ``_MAX_PENDING_TAG_STREAMS``, and
        a single delivery is bounded by the QUIC per-stream flow-control
        window (``_MAX_STREAM_DATA``).  A per-byte cap here would instead
        drop a legitimate large first delivery (e.g. a batched ``[0x01]`` +
        multi-KiB PCM frame) and, by discarding the tag with it, leave the
        stream permanently mis-routed.
        """
        if stream_id not in self._pending_tags:
            if len(self._pending_tags) >= _MAX_PENDING_TAG_STREAMS:
                logger.warning(
                    "Refusing untagged WebTransport stream %d — too many pending", stream_id
                )
                return
        buf = self._pending_tags.setdefault(stream_id, bytearray())
        buf.extend(data)
        if not buf:
            # Zero-byte delivery; wait for the next event. Bounded by the
            # pending-stream count cap above and the QUIC idle timeout.
            return
        tag = buf[0]
        payload = bytes(buf[1:])
        del self._pending_tags[stream_id]
        if tag == _TAG_AUDIO:
            if self._inbound_audio_stream_id is not None:
                logger.warning(
                    "Ignoring extra audio stream %d (already have %d)",
                    stream_id,
                    self._inbound_audio_stream_id,
                )
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
                return
            self._inbound_control_stream_id = stream_id
            if payload:
                self._handle_control_bytes(payload)
        else:
            logger.warning("Unknown WebTransport stream tag 0x%02x on %d", tag, stream_id)

    def _handle_audio_bytes(self, data: bytes) -> None:
        if not data:
            return
        chunk = AudioChunk(data=data, format=self._inbound_format)
        if chunk.format.sample_rate != self._target_rate:
            chunk = resample_chunk(chunk, self._target_rate)
        try:
            self._in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("Inbound WebTransport audio queue full — dropping frame")

    def _handle_control_bytes(self, data: bytes) -> None:
        for msg in self._control_codec.feed(data):
            self._handle_control_message(msg)

    def _handle_control_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "config":
            sample_rate = _valid_config_sample_rate(msg.get("sample_rate"))
            if sample_rate is not None:
                self._inbound_format = AudioFormat(
                    sample_rate=sample_rate,
                    channels=self._audio_format.channels,
                    sample_width=self._audio_format.sample_width,
                    encoding=self._audio_format.encoding,
                )
                logger.info(
                    "Client negotiated WebTransport audio format: %s",
                    self._inbound_format,
                )
            elif "sample_rate" in msg:
                logger.warning(
                    "Ignoring invalid WebTransport sample_rate: %s",
                    _trunc_for_log(msg["sample_rate"]),
                )
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
            return
        try:
            quic.reset_stream(self._outbound_audio_stream_id, error_code=0)
            self._quic_protocol.transmit()
        except Exception:
            # Promoted from debug to warning: if reset_stream silently fails,
            # the client will keep hearing in-flight TTS after a barge-in.
            logger.warning("reset_stream failed for audio stream", exc_info=True)
        finally:
            self._outbound_audio_stream_id = None

    def close_connection(self, *, reason: str = "") -> None:
        """Send CONNECTION_CLOSE and tear down the QUIC connection.

        ``QuicConnectionProtocol.close`` flushes the close frame itself.
        """
        try:
            self._quic_protocol.close(error_code=0, reason_phrase=reason)
        except Exception:
            logger.debug("Error closing WebTransport QUIC connection", exc_info=True)

    async def _outbound_writer(self) -> None:
        try:
            while True:
                chunk = await self._out_queue.get()
                if chunk is None:
                    return
                rate = chunk.format.sample_rate
                if rate != self._outbound_rate:
                    self._send_control({"type": "audio_format", "sample_rate": rate})
                    self._outbound_rate = rate
                if self._outbound_audio_stream_id is None:
                    self._outbound_audio_stream_id = self._h3.create_webtransport_stream(
                        self._session_id
                    )
                    self._send_stream_bytes(self._outbound_audio_stream_id, bytes([_TAG_AUDIO]))
                self._send_stream_bytes(self._outbound_audio_stream_id, chunk.data)
                self._quic_protocol.transmit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebTransport outbound writer crashed")
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

    aioquic_proto = require_module(
        "aioquic.asyncio.protocol", extra="webtransport", purpose="WebTransport transport"
    )
    h3_conn = require_module(
        "aioquic.h3.connection", extra="webtransport", purpose="WebTransport transport"
    )
    h3_events = require_module(
        "aioquic.h3.events", extra="webtransport", purpose="WebTransport transport"
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
            self._h3 = None
            # NOTE: do *not* name this ``self._wt_transport`` — the aioquic
            # ``QuicConnectionProtocol`` base class already owns that attribute
            # for the asyncio ``DatagramTransport`` (assigned in
            # ``connection_made``).  Shadowing it both breaks QUIC sending and
            # makes the "already have a session" check below always true, so
            # every CONNECT is rejected with 409.
            self._wt_transport: WebTransportConnectionTransport | None = None
            # Populated by the protocol factory before events flow.
            self._accept_path: str = ""
            self._on_session: Callable[[WebTransportConnectionTransport], None] = lambda _t: None
            self._session_config: WebTransportTransportConfig = WebTransportTransportConfig()

        def quic_event_received(self, event: Any) -> None:
            if self._h3 is None:
                self._h3 = h3_conn.H3Connection(self._quic, enable_webtransport=True)
            for h3_event in self._h3.handle_event(event):
                self._handle_h3_event(h3_event)

        def _handle_h3_event(self, event: Any) -> None:
            assert self._h3 is not None
            if isinstance(event, h3_events.HeadersReceived):
                self._handle_headers(event)
            elif isinstance(event, h3_events.WebTransportStreamDataReceived):
                if self._wt_transport is None:
                    return
                self._wt_transport._feed_stream_data(  # noqa: SLF001
                    event.stream_id, event.data, event.stream_ended
                )

        def _handle_headers(self, event: Any) -> None:
            assert self._h3 is not None
            headers = dict(event.headers)
            method = headers.get(b":method", b"").decode("ascii", errors="ignore")
            protocol = headers.get(b":protocol", b"").decode("ascii", errors="ignore")
            path = headers.get(b":path", b"").decode("ascii", errors="ignore")

            if method != "CONNECT" or protocol != "webtransport":
                self._h3.send_headers(event.stream_id, [(b":status", b"400")], end_stream=True)
                self.transmit()
                return

            if path != self._accept_path:
                self._h3.send_headers(event.stream_id, [(b":status", b"404")], end_stream=True)
                self.transmit()
                return

            if self._wt_transport is not None:
                # Reject additional WT sessions on the same QUIC connection.
                self._h3.send_headers(event.stream_id, [(b":status", b"409")], end_stream=True)
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
            self._on_session(transport)

        def connection_lost(self, exc: BaseException | None) -> None:
            if self._wt_transport is not None:
                self._wt_transport._mark_connection_lost()  # noqa: SLF001
            super().connection_lost(exc)

    _PROTOCOL_CLASS_CACHE = _EasyCatH3Protocol
    return _EasyCatH3Protocol


def _protocol_factory(
    *,
    accept_path: str,
    on_session: Callable[[WebTransportConnectionTransport], None],
    session_config: WebTransportTransportConfig,
) -> Callable[..., Any]:
    """Build the ``create_protocol`` callable for :func:`aioquic.asyncio.serve`."""

    protocol_cls = _get_protocol_class()

    def factory(*args: Any, **kwargs: Any) -> Any:
        proto = protocol_cls(*args, **kwargs)
        proto._accept_path = accept_path
        proto._on_session = on_session
        proto._session_config = session_config
        return proto

    return factory


# ── Per-session transport ──────────────────────────────────────────


class WebTransportConnectionTransport(_AudioQueueMixin):
    """Per-session :class:`~easycat.providers.Transport`.

    Normally yielded to your handler by :class:`WebTransportServer`.  You can
    also construct one directly if you're managing your own aioquic server —
    pass the H3Connection, the QuicConnectionProtocol, and the CONNECT stream
    id via the underscore-prefixed kwargs.
    """

    transport_kind = "webtransport"
    default_echo_cancellation_enabled = True

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
        self._init_audio_queue(self._config.max_pending_chunks)
        self._out_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=self._config.outbound_max_pending,
        )
        self._on_close = asyncio.Event()
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
            )
            self._needs_external_session = False

    # ── Transport protocol ────────────────────────────────────────

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def connect(self) -> None:
        if self._connected:
            return
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
        self._connected = True
        self._client_connected.set()
        await self._session.start()

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self._client_connected.clear()
        if self._session is not None:
            await self._session.stop()
            # Actively tear the QUIC connection down so a server-initiated
            # end-of-session reaches the client immediately rather than
            # lingering until the idle timeout.
            self._session.close_connection(reason="session ended")
        self._enqueue_sentinel()
        try:
            self._out_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self._on_close.set()

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
        if self._session is not None:
            self._session.close_connection(reason=reason)
        self._enqueue_sentinel()
        try:
            self._out_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self._on_close.set()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if not self._connected:
            return False
        try:
            self._out_queue.put_nowait(chunk)
            return True
        except asyncio.QueueFull:
            logger.debug("WebTransport outbound queue full — dropping TTS frame")
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

    def _mark_connection_lost(self) -> None:
        self._on_close.set()
        # Unblock receive_audio() and the outbound writer.
        self._enqueue_sentinel()
        try:
            self._out_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            aioquic_ver = version("aioquic")
        except Exception:
            aioquic_ver = "unknown"
        return {
            "provider": "webtransport-connection",
            "model": "unknown",
            "api_version": "h3",
            "sdk_version": aioquic_ver,
        }


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
            async with manager.connection(id(transport), session):
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
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._started = False

    def _dispatch_session(self, transport: WebTransportConnectionTransport) -> None:
        """Accept a new session or reject it when the concurrency cap is hit.

        Invoked synchronously from the aioquic protocol when a CONNECT-
        webtransport handshake completes.
        """
        if len(self._handler_tasks) >= self._config.max_concurrent_sessions:
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
        task = asyncio.create_task(self._run_handler(transport))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def start(self) -> None:
        if self._started:
            return
        quic_config = _build_quic_configuration(self._config.certfile, self._config.keyfile)

        factory = _protocol_factory(
            accept_path=self._config.path,
            on_session=self._dispatch_session,
            session_config=self._config,
        )
        aioquic_server = require_module(
            "aioquic.asyncio.server",
            extra="webtransport",
            purpose="WebTransport transport",
        )
        self._server = await aioquic_server.serve(
            self._config.host,
            self._config.port,
            configuration=quic_config,
            create_protocol=factory,
        )
        self._started = True
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
            except Exception:
                logger.debug("Error while disconnecting WebTransport session", exc_info=True)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        # Tear down in-flight handlers, but never await the current task
        # (which can happen if a handler calls back into ``stop()``).
        current = asyncio.current_task()
        others = [t for t in self._handler_tasks if t is not current]
        for task in others:
            task.cancel()
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        # ``current`` is removed from the set via its own done-callback when
        # it eventually exits; don't clear() blindly or we'd lose that
        # bookkeeping.
        self._handler_tasks.difference_update(others)

        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except AttributeError:
                pass
            self._server = None

    async def serve_forever(self) -> None:
        """Convenience: start the server and block until cancelled."""
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()


# ── Single-client convenience wrapper ─────────────────────────────


class WebTransportTransport(_AudioQueueMixin):
    """Single-client server :class:`~easycat.providers.Transport`.

    Parallels :class:`~easycat.transports.websocket.WebSocketTransport`'s
    shape: implements the Transport protocol directly, accepts at most one
    client.  Internally hosts a :class:`WebTransportServer` with a one-shot
    handler; once a client connects, ``send_audio`` / ``clear_audio`` /
    ``receive_audio`` delegate straight to the per-session
    :class:`WebTransportConnectionTransport` — no extra buffering between
    this outer transport and the inner session.

    For multi-client deployments, use :class:`WebTransportServer` directly
    and create one EasyCat ``Session`` per accepted
    :class:`WebTransportConnectionTransport`.
    """

    transport_kind = "webtransport"
    default_echo_cancellation_enabled = True

    def __init__(self, config: WebTransportTransportConfig | None = None) -> None:
        self._config = config or WebTransportTransportConfig()
        self._audio_format = self._config.audio_format
        # We don't push into the mixin's ``_in_queue`` (``receive_audio``
        # below delegates), but ``_init_audio_queue`` also sets up
        # ``_connected`` and ``_client_connected`` which we do use.
        self._init_audio_queue(self._config.max_pending_chunks)
        self._server: WebTransportServer | None = None
        self._active: WebTransportConnectionTransport | None = None

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def connect(self) -> None:
        if self._connected:
            return
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
            self._active = transport
            self._client_connected.set()
            try:
                await transport.wait_closed()
            finally:
                self._active = None

        # Pin the wrapped server to a single session so an over-cap client is
        # rejected at accept time (the server force-closes it) instead of
        # lingering behind the one-session ``handle`` closure above.
        single_client_config = replace(self._config, max_concurrent_sessions=1)
        self._server = WebTransportServer(single_client_config, handle)
        await self._server.start()
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        # Unblock any ``receive_audio`` caller that is waiting for the first
        # client — they'll see ``_connected`` is False and exit cleanly.
        self._client_connected.set()
        if self._server is not None:
            await self._server.stop()
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
        try:
            from importlib.metadata import version

            aioquic_ver = version("aioquic")
        except Exception:
            aioquic_ver = "unknown"
        return {
            "provider": "webtransport",
            "model": "unknown",
            "api_version": "h3",
            "sdk_version": aioquic_ver,
        }
