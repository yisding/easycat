"""WebSocket transport: server-side WebSocket for browser/mobile clients.

Hosts a WebSocket server on a configurable port. Each client connection maps
to a single audio session. The wire protocol uses:
  - **Binary frames** for raw PCM16 audio chunks.
  - **Text frames** for JSON control messages (``start``, ``stop``, ``config``)
    and server → browser event messages (transcripts, interruptions, per-turn
    latency; see ``transports/_browser_events.py``).

The maintained reader-facing description of the wire protocol lives in
``docs/browser-playground.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from hmac import compare_digest
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from easycat._audio_utils import resample_chunk
from easycat._signals import create_shutdown_event
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.session_manager import SessionManager
from easycat.transports._base import AudioQueueMixin, ServerTransportBase

if TYPE_CHECKING:
    from collections.abc import Callable

    from easycat.session._session import Session

logger = logging.getLogger(__name__)
_MIN_NEGOTIATED_SAMPLE_RATE = 8000
_MAX_NEGOTIATED_SAMPLE_RATE = 384000

# WebSocket-specific ``TransportDegraded.reason`` codes emitted on the session
# event bus (via the inherited ``AudioQueueMixin._emit_degraded``).  These
# mirror conditions that previously only reached ``logger.warning``; emitting
# them keeps the journal the single source of truth for observability.  The
# cross-transport ``inbound_queue_full`` code is emitted by ``_enqueue_chunk``
# in ``_base`` and needs no wiring here.
_DEGRADED_EXTRA_CLIENT_REJECTED = "extra_client_rejected"
_DEGRADED_CONTROL_DECODE_FAILED = "control_decode_failed"
_DEGRADED_INVALID_SAMPLE_RATE = "invalid_sample_rate"


def _valid_config_sample_rate(value: object) -> int | None:
    """Return a negotiated sample rate only for sane integer values."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < _MIN_NEGOTIATED_SAMPLE_RATE or value > _MAX_NEGOTIATED_SAMPLE_RATE:
        return None
    return value


@dataclass
class WebSocketTransportConfig:
    """Configuration for :class:`WebSocketTransport`."""

    default_echo_cancellation_enabled: ClassVar[bool] = True

    host: str = "127.0.0.1"
    port: int = 8765
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200


@dataclass(frozen=True)
class WebSocketSessionServerConfig:
    """Settings for :func:`serve_websocket_sessions`."""

    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = None
    max_sessions: int = 10


def websocket_session_server_config_from_env(
    prefix: str = "EASYCAT_WS",
) -> WebSocketSessionServerConfig:
    """Load WebSocket session-server settings from environment variables."""
    return WebSocketSessionServerConfig(
        host=os.getenv(f"{prefix}_HOST", "127.0.0.1"),
        port=int(os.getenv(f"{prefix}_PORT", "8765")),
        auth_token=os.getenv(f"{prefix}_TOKEN"),
        max_sessions=int(os.getenv(f"{prefix}_MAX_SESSIONS", "10")),
    )


def websocket_server_authorized(
    headers: Headers,
    path: str,
    token: str | None,
    *,
    allow_query_token: bool = False,
) -> bool:
    """Authorize a WebSocket request against an optional bearer/query token.

    A ``?token=`` query value is accepted ONLY when ``allow_query_token=True``
    (default OFF). This is a deliberate breaking change for the bundled WS
    browser client (``examples/ws_browser_client.html``) — browsers cannot set
    handshake headers, so they relied on the query token. Pass
    ``allow_query_token=True`` as the loopback/dev opt-in to keep that client
    working locally.
    """
    if token is None:
        return True
    value = headers.get("Authorization")
    if value is not None:
        scheme, separator, credential = value.partition(" ")
        if separator == " " and scheme.lower() == "bearer":
            return compare_digest(credential, token)

    if allow_query_token:
        query_token = parse_qs(urlsplit(path).query).get("token", [None])[0]
        return query_token is not None and compare_digest(query_token, token)
    return False


def _plain_response(status: HTTPStatus, body: str) -> Response:
    payload = body.encode()
    return Response(
        status.value,
        status.phrase,
        Headers(
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(payload))),
            ]
        ),
        payload,
    )


async def serve_websocket_sessions(
    session_factory: Callable[[ServerConnection], Session],
    config: WebSocketSessionServerConfig | None = None,
    *,
    stop_event: asyncio.Event | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    allow_query_token: bool = False,
) -> None:
    """Serve one EasyCat session per accepted WebSocket connection.

    ``session_factory`` receives the accepted ``ServerConnection`` and returns
    a created, not-yet-started :class:`~easycat.Session`. This helper owns
    session start/stop, optional bearer-token auth, session-limit rejection,
    and process shutdown.

    A non-loopback bind requires ``config.auth_token``: binding beyond loopback
    without a token raises :class:`ValueError` via the shared
    :func:`easycat.server.auth.enforce_bind_guard` (the SAME structured guard
    :func:`~easycat.transports.webrtc.serve_webrtc_config_sessions` uses) unless
    ``unsafe_allow_no_auth=True`` is passed to explicitly opt into an
    unauthenticated endpoint.

    ``allow_query_token`` (default OFF) gates the ``?token=`` query auth. It is
    OFF by default — a breaking change for the bundled WS browser client, which
    cannot set handshake headers; pass ``allow_query_token=True`` as the
    loopback/dev opt-in. Capacity is owned by the shared
    :class:`~easycat.server.transports.CapacityGate` collaborator (lifted out of
    the inline ``Semaphore``) so it behaves identically to the WebRTC helper.
    """
    from easycat.server.auth import BearerTokenAuth, enforce_bind_guard
    from easycat.server.transports import CapacityGate

    settings = config or WebSocketSessionServerConfig()
    bind_auth = (
        BearerTokenAuth(token=settings.auth_token, allow_query_token=allow_query_token)
        if settings.auth_token is not None
        else None
    )
    enforce_bind_guard(
        settings.host,
        auth=bind_auth,
        unsafe_allow_no_auth=unsafe_allow_no_auth,
    )
    manager: SessionManager[int] = SessionManager()
    gate: CapacityGate[int] = CapacityGate(settings.max_sessions)

    def process_request(_ws: ServerConnection, request: Request) -> Response | None:
        if not websocket_server_authorized(
            request.headers,
            request.path,
            settings.auth_token,
            allow_query_token=allow_query_token,
        ):
            return _plain_response(HTTPStatus.UNAUTHORIZED, "Missing or invalid bearer token.\n")
        return None

    async def handle_connection(ws: ServerConnection) -> None:
        if not gate.try_acquire():
            await ws.close(code=1013, reason="Server is at the configured session limit")
            return
        try:
            session = session_factory(ws)
            async with manager.connection(id(ws), session, runtime_feedback=runtime_feedback):
                await ws.wait_closed()
        finally:
            gate.release()

    server = await websockets.serve(
        handle_connection,
        settings.host,
        settings.port,
        process_request=process_request,
        compression=None,
    )
    if announce:
        print(f"\nServer ready. Connect WebSocket clients to ws://{settings.host}:{settings.port}")
        print("Press Ctrl+C to stop.\n")

    event = stop_event or create_shutdown_event()
    try:
        await event.wait()
    finally:
        server.close()
        await server.wait_closed()
        await manager.stop_all()


async def serve_websocket_config_sessions(
    config_factory: Callable[[WebSocketConnectionTransport], Any],
    config: WebSocketSessionServerConfig | None = None,
    *,
    transport_config: WebSocketTransportConfig | None = None,
    stop_event: asyncio.Event | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    allow_query_token: bool = False,
) -> None:
    """Serve one EasyCat session per connection using an EasyConfig factory.

    ``config_factory`` receives a per-client
    :class:`WebSocketConnectionTransport` and returns the app config passed to
    :func:`easycat.create_session`. Use :func:`serve_websocket_sessions` when
    callers need to construct or own the ``Session`` object directly.

    Like :func:`serve_websocket_sessions`, a non-loopback bind requires a token
    unless ``unsafe_allow_no_auth=True``, and ``?token=`` query auth is OFF
    unless ``allow_query_token=True``.
    """
    from easycat.config import create_session

    def session_factory(ws: ServerConnection) -> Session:
        transport = WebSocketConnectionTransport(ws, transport_config)
        return create_session(config_factory(transport))

    await serve_websocket_sessions(
        session_factory,
        config,
        stop_event=stop_event,
        runtime_feedback=runtime_feedback,
        announce=announce,
        unsafe_allow_no_auth=unsafe_allow_no_auth,
        allow_query_token=allow_query_token,
    )


def run_websocket_config_server(
    config_factory: Callable[[WebSocketConnectionTransport], Any],
    config: WebSocketSessionServerConfig | None = None,
    *,
    transport_config: WebSocketTransportConfig | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    allow_query_token: bool = False,
) -> None:
    """Run a WebSocket server using ``EASYCAT_WS_*`` env defaults.

    This synchronous wrapper is the shortest path for examples and starter
    apps. It reads ``EASYCAT_WS_HOST``, ``EASYCAT_WS_PORT``,
    ``EASYCAT_WS_TOKEN``, and ``EASYCAT_WS_MAX_SESSIONS`` when *config* is not
    supplied, then delegates to :func:`serve_websocket_config_sessions`.

    A non-loopback bind requires a token unless ``unsafe_allow_no_auth=True``.
    ``?token=`` query auth is OFF unless ``allow_query_token=True`` (the
    loopback/dev opt-in for the bundled browser client).
    """
    settings = config or websocket_session_server_config_from_env()
    asyncio.run(
        serve_websocket_config_sessions(
            config_factory,
            settings,
            transport_config=transport_config,
            runtime_feedback=runtime_feedback,
            announce=announce,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
            allow_query_token=allow_query_token,
        )
    )


class WebSocketTransport(ServerTransportBase):
    """Transport that accepts a single WebSocket client connection.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Wire protocol
    -------------
    **Inbound (client -> server):**
      - Binary frame: raw PCM16 audio bytes.
      - Text frame: JSON control message.
        ``{"type": "start"}``  — client signals session start.
        ``{"type": "stop"}``   — client signals session end.
        ``{"type": "config", "sample_rate": 16000, ...}`` — negotiate format.

    **Outbound (server -> client):**
      - Binary frame: raw PCM16 audio bytes.
      - Text frame: JSON control message (e.g., ``{"type": "ready"}``) or
        session event message (``stt_partial``, ``stt_final``, ``agent_delta``,
        ``agent_final``, ``turn_started``, ``interruption``, ``turn_latency``;
        see :mod:`easycat.transports._browser_events`).

    The maintained reader-facing description of this protocol lives in
    ``docs/browser-playground.md``.
    """

    transport_kind = "websocket"
    default_echo_cancellation_enabled = True
    _transport_name = "WebSocket"

    def __init__(self, config: WebSocketTransportConfig | None = None) -> None:
        self._config = config or WebSocketTransportConfig()
        super().__init__(
            host=self._config.host,
            port=self._config.port,
            max_pending_chunks=self._config.max_pending_chunks,
        )
        self._audio_format = self._config.audio_format
        self._outbound_rate: int | None = None

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        await super().connect()
        self._ensure_browser_event_forwarder()

    async def disconnect(self) -> None:
        self._close_browser_event_forwarder()
        await super().disconnect()

    async def _send_client_event(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps(payload))

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send an audio chunk to the connected WebSocket client as a binary frame.

        When the outbound sample rate changes (e.g. TTS provider switch), an
        ``audio_format`` JSON control message is sent first so the client can
        create playback buffers at the correct rate.
        """
        ws = self._ws
        if ws is None:
            return False
        try:
            rate = chunk.format.sample_rate
            if rate != self._outbound_rate:
                await ws.send(json.dumps({"type": "audio_format", "sample_rate": rate}))
                self._outbound_rate = rate
            await ws.send(chunk.data)
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: client disconnected")
            if self._ws is ws:
                self._ws = None
                self._client_connected.clear()
            return False

    async def clear_audio(self) -> None:
        """No-op — WebSocket sends frames immediately without buffering."""

    # ── Server helpers ────────────────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Handle a single client connection."""
        if self._ws is not None:
            logger.warning("Rejecting additional WebSocket client (only one session supported)")
            self._emit_degraded(
                _DEGRADED_EXTRA_CLIENT_REJECTED,
                "rejected additional client; one session at a time",
            )
            await ws.close(4000, "Only one session at a time")
            return

        self._ws = ws
        self._client_connected.set()
        self._outbound_rate = None
        # Reset negotiated format so every accepted client starts from the
        # configured default. The prior connection's ``finally`` cleanup may not
        # have run (e.g. when ``send_audio`` won the disconnect race and cleared
        # ``_ws`` first), so reset here rather than relying on teardown. A client
        # that negotiates its own rate still overrides this via
        # ``_handle_control_message`` before any of its audio is processed.
        self._audio_format = self._config.audio_format
        logger.info("WebSocket client connected")

        try:
            await ws.send(json.dumps({"type": "ready"}))
            await self._receive_loop(ws)
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket client disconnected")
        finally:
            if self._ws is ws:
                self._ws = None
                self._client_connected.clear()
                # Reset negotiated format so the next client starts fresh.
                self._audio_format = self._config.audio_format
                self._outbound_rate = None
                self._enqueue_sentinel()
            elif self._ws is None:
                # ``send_audio`` may already have noticed this connection is
                # closed and cleared the slot. Finish cleanup only if no newer
                # client has claimed it in the meantime.
                self._audio_format = self._config.audio_format
                self._outbound_rate = None
                self._enqueue_sentinel()

    async def _receive_loop(self, ws: ServerConnection) -> None:
        """Read messages from the client connection.

        If the client negotiated a sample rate different from the configured
        pipeline rate (e.g. browser at 48 kHz vs. pipeline at 16 kHz), inbound
        audio is automatically resampled before being enqueued.
        """
        target_rate = self._config.audio_format.sample_rate
        async for message in ws:
            if isinstance(message, bytes):
                if not message:
                    logger.debug("Dropping empty WebSocket audio frame")
                    continue
                chunk = AudioChunk(data=message, format=self._audio_format)
                if chunk.format.sample_rate != target_rate:
                    # Hot path: each inbound binary frame is resampled when the
                    # client rate differs from the pipeline rate (the common
                    # browser-48kHz-to-16kHz case). ``resample`` re-resolves its
                    # numpy/soxr/scipy backend per call, so there is some
                    # per-frame allocation/import-probe churn here. This is
                    # acceptable because frames are ~20ms (low call frequency);
                    # if a higher-throughput backend is needed, cache the
                    # chosen resampler callable in ``_audio_utils``.
                    chunk = resample_chunk(chunk, target_rate)
                self._enqueue_chunk(chunk, context="WebSocket")
            elif isinstance(message, str):
                self._handle_control_message(message)

    def _handle_control_message(self, raw: str) -> None:
        """Process a JSON control message from the client."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON control message")
            self._emit_degraded(_DEGRADED_CONTROL_DECODE_FAILED, "control frame is not valid JSON")
            return
        if not isinstance(msg, dict):
            logger.warning("Ignoring non-object JSON control message")
            self._emit_degraded(
                _DEGRADED_CONTROL_DECODE_FAILED,
                "control frame is not a JSON object",
            )
            return

        msg_type = msg.get("type")
        if msg_type == "config":
            sample_rate = _valid_config_sample_rate(msg.get("sample_rate"))
            if sample_rate is not None:
                self._audio_format = AudioFormat(
                    sample_rate=sample_rate,
                    channels=self._audio_format.channels,
                    sample_width=self._audio_format.sample_width,
                    encoding=self._audio_format.encoding,
                )
                logger.info("Client negotiated audio format: %s", self._audio_format)
            elif "sample_rate" in msg:
                logger.warning("Ignoring invalid WebSocket sample_rate: %r", msg["sample_rate"])
                self._emit_degraded(
                    _DEGRADED_INVALID_SAMPLE_RATE,
                    f"ignored invalid negotiated sample_rate {msg['sample_rate']!r}",
                )
        elif msg_type == "start":
            logger.debug("Client sent start signal")
        elif msg_type == "stop":
            logger.debug("Client sent stop signal")
        else:
            logger.debug("Unknown control message type: %s", msg_type)

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            ws_ver = version("websockets")
        except Exception:
            ws_ver = "unknown"
        return {
            "provider": "websocket",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": ws_ver,
        }


class WebSocketConnectionTransport(AudioQueueMixin):
    """Transport bound to a single existing WebSocket connection.

    Useful for servers that already own the WebSocket accept loop and want
    one EasyCat Session per client connection.
    """

    transport_kind = "websocket"
    default_echo_cancellation_enabled = True

    def __init__(
        self,
        ws: ServerConnection,
        config: WebSocketTransportConfig | None = None,
    ) -> None:
        self._ws = ws
        self._config = config or WebSocketTransportConfig()
        self._audio_format = self._config.audio_format
        self._outbound_rate: int | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._init_audio_queue(self._config.max_pending_chunks)

    @property
    def audio_format(self) -> AudioFormat:
        """The current audio format for this transport."""
        return self._audio_format

    async def connect(self) -> None:
        if self._connected:
            return
        self._reset_audio_queue()
        self._connected = True
        self._client_connected.set()
        self._outbound_rate = None
        self._ensure_browser_event_forwarder()
        await self._ws.send(json.dumps({"type": "ready"}))
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._close_browser_event_forwarder()
        self._connected = False
        self._client_connected.clear()
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._receive_task = None
        try:
            await self._ws.close()
        except Exception:
            logger.debug("Error closing WebSocket connection", exc_info=True)
        self._enqueue_sentinel()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if not self._connected:
            return False
        try:
            rate = chunk.format.sample_rate
            if rate != self._outbound_rate:
                await self._ws.send(json.dumps({"type": "audio_format", "sample_rate": rate}))
                self._outbound_rate = rate
            await self._ws.send(chunk.data)
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: client disconnected")
            self._connected = False
            self._client_connected.clear()
            return False

    async def clear_audio(self) -> None:
        """No-op — WebSocket sends frames immediately without buffering."""

    async def _send_client_event(self, payload: dict[str, Any]) -> None:
        if not self._connected:
            return
        await self._ws.send(json.dumps(payload))

    async def _receive_loop(self) -> None:
        target_rate = self._config.audio_format.sample_rate
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    if not message:
                        logger.debug("Dropping empty WebSocket audio frame")
                        continue
                    chunk = AudioChunk(data=message, format=self._audio_format)
                    if chunk.format.sample_rate != target_rate:
                        # Hot path: resampled per inbound frame when the client
                        # rate differs from the pipeline rate. ``resample``
                        # re-resolves its numpy/soxr/scipy backend per call, but
                        # this is acceptable because frames are ~20ms; cache the
                        # chosen resampler in ``_audio_utils`` if throughput
                        # becomes a concern.
                        chunk = resample_chunk(chunk, target_rate)
                    self._enqueue_chunk(chunk, context="WebSocket")
                elif isinstance(message, str):
                    self._handle_control_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket client disconnected")
        finally:
            self._connected = False
            self._client_connected.clear()
            self._audio_format = self._config.audio_format
            self._enqueue_sentinel()

    def _handle_control_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON control message")
            self._emit_degraded(_DEGRADED_CONTROL_DECODE_FAILED, "control frame is not valid JSON")
            return
        if not isinstance(msg, dict):
            logger.warning("Ignoring non-object JSON control message")
            self._emit_degraded(
                _DEGRADED_CONTROL_DECODE_FAILED,
                "control frame is not a JSON object",
            )
            return

        if msg.get("type") == "config":
            sample_rate = _valid_config_sample_rate(msg.get("sample_rate"))
            if sample_rate is not None:
                self._audio_format = AudioFormat(
                    sample_rate=sample_rate,
                    channels=self._audio_format.channels,
                    sample_width=self._audio_format.sample_width,
                    encoding=self._audio_format.encoding,
                )
            elif "sample_rate" in msg:
                logger.warning("Ignoring invalid WebSocket sample_rate: %r", msg["sample_rate"])
                self._emit_degraded(
                    _DEGRADED_INVALID_SAMPLE_RATE,
                    f"ignored invalid negotiated sample_rate {msg['sample_rate']!r}",
                )

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            ws_ver = version("websockets")
        except Exception:
            ws_ver = "unknown"
        return {
            "provider": "websocket-connection",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": ws_ver,
        }
