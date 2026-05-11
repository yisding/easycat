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
After the WebTransport session is established, both endpoints multiplex two
client-opened bidirectional QUIC streams, each starting with a 1-byte tag:

``0x01`` — **audio stream**.  Both directions carry raw PCM16 bytes
(client→server = mic, server→client = TTS).
``0x02`` — **control stream**.  Both directions carry length-prefixed JSON
frames (4-byte big-endian length, then UTF-8).  Message shapes mirror
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
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.asyncio.server import QuicServer, serve
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import (
    H3Event,
    HeadersReceived,
    WebTransportStreamDataReceived,
)
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.audio_utils import resample_chunk
from easycat.transports._base import _AudioQueueMixin
from easycat.transports.websocket import _valid_config_sample_rate

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
_IDLE_TIMEOUT_SEC = 15.0

_DEFAULT_PATH = "/easycat"

# Type alias for the user-supplied per-session handler.
SessionHandler = Callable[["WebTransportConnectionTransport"], Awaitable[None]]


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


def _build_quic_configuration(certfile: str, keyfile: str) -> QuicConfiguration:
    if not certfile or not keyfile:
        raise ValueError(
            "WebTransport requires certfile and keyfile paths (TLS is mandatory). "
            "Generate a local cert with: openssl req -x509 -newkey rsa:2048 "
            '-keyout key.pem -out cert.pem -days 1 -nodes -subj "/CN=localhost"'
        )
    config = QuicConfiguration(
        alpn_protocols=["h3"],
        is_client=False,
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
    """Length-prefixed (4-byte BE) UTF-8 JSON framing."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buf.extend(data)
        out: list[dict[str, Any]] = []
        while True:
            if len(self._buf) < 4:
                break
            (length,) = struct.unpack_from(">I", self._buf, 0)
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

        self._audio_stream_id: int | None = None
        self._control_stream_id: int | None = None
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
        if stream_id == self._audio_stream_id:
            self._handle_audio_bytes(data)
        elif stream_id == self._control_stream_id:
            self._handle_control_bytes(data)
        else:
            buf = self._pending_tags.setdefault(stream_id, bytearray())
            buf.extend(data)
            if not buf:
                return
            tag = buf[0]
            payload = bytes(buf[1:])
            del self._pending_tags[stream_id]
            if tag == _TAG_AUDIO:
                if self._audio_stream_id is not None:
                    logger.warning(
                        "Ignoring extra audio stream %d (already have %d)",
                        stream_id,
                        self._audio_stream_id,
                    )
                    return
                self._audio_stream_id = stream_id
                if payload:
                    self._handle_audio_bytes(payload)
            elif tag == _TAG_CONTROL:
                if self._control_stream_id is not None:
                    logger.warning(
                        "Ignoring extra control stream %d (already have %d)",
                        stream_id,
                        self._control_stream_id,
                    )
                    return
                self._control_stream_id = stream_id
                if payload:
                    self._handle_control_bytes(payload)
            else:
                logger.warning("Unknown WebTransport stream tag 0x%02x on %d", tag, stream_id)

        if ended:
            self._on_close.set()

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
                logger.warning("Ignoring invalid WebTransport sample_rate: %r", msg["sample_rate"])
        elif msg_type in ("start", "stop"):
            logger.debug("Client sent WebTransport %s signal", msg_type)
        else:
            logger.debug("Unknown WebTransport control message type: %r", msg_type)

    def _send_control(self, msg: dict[str, Any]) -> None:
        if self._control_stream_id is None:
            self._control_stream_id = self._h3.create_webtransport_stream(self._session_id)
            self._h3.send_data(self._control_stream_id, bytes([_TAG_CONTROL]), end_stream=False)
        self._h3.send_data(
            self._control_stream_id,
            _ControlCodec.encode(msg),
            end_stream=False,
        )
        self._quic_protocol.transmit()

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
                if self._audio_stream_id is None:
                    self._audio_stream_id = self._h3.create_webtransport_stream(self._session_id)
                    self._h3.send_data(
                        self._audio_stream_id, bytes([_TAG_AUDIO]), end_stream=False
                    )
                self._h3.send_data(self._audio_stream_id, chunk.data, end_stream=False)
                self._quic_protocol.transmit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebTransport outbound writer crashed")


# ── Per-connection aioquic protocol class ─────────────────────────


class _EasyCatH3Protocol(QuicConnectionProtocol):
    """aioquic protocol that dispatches WebTransport sessions.

    One instance per QUIC connection.  When a CONNECT-webtransport request
    arrives on the expected path, builds a :class:`WebTransportConnectionTransport`
    and hands it to the configured session-accepted callback.  In v1 we accept
    one WebTransport session per QUIC connection (matches browser usage).
    """

    # Populated by ``_protocol_factory`` before the instance handles events.
    _accept_path: str
    _on_session: Callable[[WebTransportConnectionTransport], None]
    _session_config: WebTransportTransportConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._h3: H3Connection | None = None
        self._transport: WebTransportConnectionTransport | None = None

    def quic_event_received(self, event: QuicEvent) -> None:
        if self._h3 is None:
            self._h3 = H3Connection(self._quic, enable_webtransport=True)
        for h3_event in self._h3.handle_event(event):
            self._handle_h3_event(h3_event)

    def _handle_h3_event(self, event: H3Event) -> None:
        assert self._h3 is not None
        if isinstance(event, HeadersReceived):
            self._handle_headers(event)
        elif isinstance(event, WebTransportStreamDataReceived):
            if self._transport is None:
                return
            self._transport._feed_stream_data(  # noqa: SLF001
                event.stream_id, event.data, event.stream_ended
            )

    def _handle_headers(self, event: HeadersReceived) -> None:
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

        if self._transport is not None:
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
        self._transport = transport
        self._on_session(transport)

    def connection_lost(self, exc: BaseException | None) -> None:
        if self._transport is not None:
            self._transport._mark_connection_lost()  # noqa: SLF001
        super().connection_lost(exc)


def _protocol_factory(
    *,
    accept_path: str,
    on_session: Callable[[WebTransportConnectionTransport], None],
    session_config: WebTransportTransportConfig,
) -> Callable[..., QuicConnectionProtocol]:
    """Build the ``create_protocol`` callable for :func:`aioquic.asyncio.serve`."""

    def factory(*args: Any, **kwargs: Any) -> QuicConnectionProtocol:
        proto = _EasyCatH3Protocol(*args, **kwargs)
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
        self._reset_audio_queue()
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
        session_handler: SessionHandler,
    ) -> None:
        self._config = config
        self._session_handler = session_handler
        self._server: QuicServer | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        quic_config = _build_quic_configuration(self._config.certfile, self._config.keyfile)

        def _on_session(transport: WebTransportConnectionTransport) -> None:
            task = asyncio.create_task(self._run_handler(transport))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

        factory = _protocol_factory(
            accept_path=self._config.path,
            on_session=_on_session,
            session_config=self._config,
        )
        self._server = await serve(
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
        # Tear down all in-flight handlers first so they don't race the
        # server shutdown.
        for task in list(self._handler_tasks):
            task.cancel()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)
        self._handler_tasks.clear()

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
    client, and bridges its inbound/outbound queues to the single accepted
    session.  Under the hood it spawns a :class:`WebTransportServer`.

    For multi-client deployments, use :class:`WebTransportServer` and create
    one EasyCat ``Session`` per accepted
    :class:`WebTransportConnectionTransport`.
    """

    transport_kind = "webtransport"
    default_echo_cancellation_enabled = True

    def __init__(self, config: WebTransportTransportConfig | None = None) -> None:
        self._config = config or WebTransportTransportConfig()
        self._audio_format = self._config.audio_format
        self._init_audio_queue(self._config.max_pending_chunks)
        self._out_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=self._config.outbound_max_pending,
        )
        self._server: WebTransportServer | None = None
        self._active: WebTransportConnectionTransport | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def connect(self) -> None:
        if self._connected:
            return
        self._reset_audio_queue()
        while not self._out_queue.empty():
            try:
                self._out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        async def handle(transport: WebTransportConnectionTransport) -> None:
            if self._active is not None:
                logger.warning(
                    "Rejecting additional WebTransport client (only one session supported)"
                )
                return
            self._active = transport
            self._client_connected.set()
            try:
                self._pump_task = asyncio.create_task(self._pump_inbound(transport))
                self._writer_task = asyncio.create_task(self._pump_outbound(transport))
                await transport.wait_closed()
            finally:
                self._client_connected.clear()
                if self._pump_task is not None:
                    self._pump_task.cancel()
                    try:
                        await self._pump_task
                    except asyncio.CancelledError:
                        pass
                if self._writer_task is not None:
                    self._writer_task.cancel()
                    try:
                        await self._writer_task
                    except asyncio.CancelledError:
                        pass
                self._pump_task = None
                self._writer_task = None
                self._active = None

        self._server = WebTransportServer(self._config, handle)
        await self._server.start()
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self._client_connected.clear()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._enqueue_sentinel()
        try:
            self._out_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if self._active is None:
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
        if drained:
            logger.debug("Cleared %d pending WebTransport TTS frames", drained)

    async def _pump_inbound(self, source: WebTransportConnectionTransport) -> None:
        try:
            async for chunk in source.receive_audio():
                try:
                    self._in_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    logger.warning("Inbound WebTransport audio queue full — dropping frame")
        except asyncio.CancelledError:
            raise

    async def _pump_outbound(self, sink: WebTransportConnectionTransport) -> None:
        try:
            while True:
                chunk = await self._out_queue.get()
                if chunk is None:
                    return
                ok = await sink.send_audio(chunk)
                if not ok:
                    logger.debug("Forwarded send_audio reported drop")
        except asyncio.CancelledError:
            raise

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
