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
from typing import Any, ClassVar

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._audio_utils import PCM16StreamResampler
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.runtime.scope import BackgroundTaskScope
from easycat.teardown_budgets import (
    SERVER_DRAIN_TIMEOUT_S,
    SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
)
from easycat.transports._base import AudioQueueMixin, ServerTransportBase, make_version_info
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES

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

    # The server sees socket-write time, not the browser's playout clock, so it
    # cannot supply the continuous render reference AEC3 requires. Browser
    # clients should use getUserMedia({audio: {echoCancellation: true}}).
    default_echo_cancellation_enabled: ClassVar[bool] = False

    host: str = "127.0.0.1"
    port: int = 8765
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES


@dataclass(frozen=True)
class WebSocketSessionServerConfig:
    """Settings for :func:`easycat.server.serve_websocket_sessions`."""

    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = field(default=None, repr=False)
    max_sessions: int = 10
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S
    force_shutdown_timeout_s: float = SERVER_FORCE_SHUTDOWN_TIMEOUT_S


def websocket_session_server_config_from_env(
    prefix: str = "EASYCAT_WS",
) -> WebSocketSessionServerConfig:
    """Load WebSocket session-server settings from environment variables."""
    return WebSocketSessionServerConfig(
        host=os.getenv(f"{prefix}_HOST", "127.0.0.1"),
        port=int(os.getenv(f"{prefix}_PORT", "8765")),
        auth_token=os.getenv(f"{prefix}_TOKEN"),
        max_sessions=int(os.getenv(f"{prefix}_MAX_SESSIONS", "10")),
        drain_timeout_s=float(os.getenv(f"{prefix}_DRAIN_TIMEOUT_S", str(SERVER_DRAIN_TIMEOUT_S))),
        force_shutdown_timeout_s=float(
            os.getenv(
                f"{prefix}_FORCE_SHUTDOWN_TIMEOUT_S",
                str(SERVER_FORCE_SHUTDOWN_TIMEOUT_S),
            )
        ),
    )


class _WebSocketProtocolMixin(AudioQueueMixin):
    """Shared PCM/JSON wire protocol for both WebSocket lifecycle models."""

    _config: WebSocketTransportConfig
    _ws: ServerConnection | None
    _audio_format: AudioFormat
    _outbound_rate: int | None
    _connection_generation: int

    @property
    def audio_format(self) -> AudioFormat:
        """The current audio format for this transport."""
        return self._audio_format

    async def _send_ready(self, ws: ServerConnection) -> bool:
        """Send the handshake message without surfacing a normal close race."""
        try:
            await ws.send(json.dumps({"type": "ready"}))
            return True
        except websockets.exceptions.ConnectionClosed as exc:
            self._note_client_disconnected(exc)
            return False

    async def _run_receive_loop(
        self,
        ws: ServerConnection,
        connection_generation: int,
    ) -> None:
        """Run the shared receiver with common disconnect handling."""
        try:
            await self._receive_loop(ws, connection_generation)
        except websockets.exceptions.ConnectionClosed as exc:
            self._note_client_disconnected(exc)
        finally:
            self._finish_websocket(ws)

    def _note_client_disconnected(self, exc: websockets.exceptions.ConnectionClosed) -> None:
        logger.info("WebSocket client disconnected")
        if isinstance(exc, websockets.exceptions.ConnectionClosedError):
            self._record_transport_disconnect("websocket connection closed abnormally")

    def _finish_websocket(self, ws: ServerConnection) -> None:
        """Release protocol state only when *ws* still owns this transport."""
        if self._ws is not ws:
            return
        self._ws = None
        self._client_connected.clear()
        self._audio_format = self._config.audio_format
        self._outbound_rate = None
        self._enqueue_sentinel()
        self._after_websocket_finished()

    def _after_websocket_finished(self) -> None:
        """Lifecycle hook for the per-connection transport."""

    def _websocket_is_active(self, ws: ServerConnection) -> bool:
        return self._ws is ws

    async def _send_client_event(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or not self._websocket_is_active(ws):
            return
        try:
            await ws.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send client event: client disconnected")
            self._finish_websocket(ws)

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send audio, announcing sample-rate changes before binary PCM."""
        ws = self._ws
        if ws is None or not self._websocket_is_active(ws):
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
            self._finish_websocket(ws)
            return False

    async def clear_audio(self) -> None:
        """Tell the client to stop already-received and scheduled playback."""
        await self._send_client_event({"type": "clear"})

    async def _receive_loop(
        self,
        ws: ServerConnection | None = None,
        connection_generation: int | None = None,
    ) -> None:
        """Route inbound binary audio and JSON control messages."""
        manage_lifecycle = ws is None
        if ws is None:
            ws = self._ws
        if ws is None:
            return
        if connection_generation is None:
            connection_generation = self._connection_generation
        target_rate = self._config.audio_format.sample_rate
        resampler = PCM16StreamResampler(target_rate)
        try:
            async for message in ws:
                self._route_inbound_message(message, resampler)
        except websockets.exceptions.ConnectionClosed as exc:
            if not manage_lifecycle:
                raise
            self._note_client_disconnected(exc)
        finally:
            tail = resampler.finish()
            if (
                tail
                and connection_generation == self._connection_generation
                and self._websocket_is_active(ws)
            ):
                self._enqueue_chunk(
                    AudioChunk(data=tail, format=self._config.audio_format),
                    context="WebSocket",
                )
            if manage_lifecycle:
                self._finish_websocket(ws)

    def _route_inbound_message(
        self,
        message: str | bytes,
        resampler: PCM16StreamResampler,
    ) -> None:
        if isinstance(message, bytes):
            if not message:
                logger.debug("Dropping empty WebSocket audio frame")
                return
            data = resampler.process(message, self._audio_format.sample_rate)
            if data:
                self._enqueue_chunk(
                    AudioChunk(data=data, format=self._config.audio_format),
                    context="WebSocket",
                )
        else:
            self._handle_control_message(message)

    def _handle_control_message(self, raw: str) -> None:
        """Process one JSON control message from the client."""
        try:
            msg = json.loads(raw)
        except (RecursionError, ValueError):
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


class WebSocketTransport(_WebSocketProtocolMixin, ServerTransportBase):
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
      - Text frame: JSON control message (``{"type": "ready"}``,
        ``{"type": "clear"}``) or session event message (``stt_partial``,
        ``stt_final``, ``agent_delta``, ``agent_final``, ``turn_started``,
        ``interruption``, ``turn_latency``; see
        :mod:`easycat.transports._browser_events`).

    The maintained reader-facing description of this protocol lives in
    ``docs/browser-playground.md``.
    """

    transport_kind = "websocket"
    default_echo_cancellation_enabled = False
    _transport_name = "WebSocket"

    def __init__(self, config: WebSocketTransportConfig | None = None) -> None:
        self._config = config or WebSocketTransportConfig()
        super().__init__(
            host=self._config.host,
            port=self._config.port,
            max_pending_chunks=self._config.max_pending_chunks,
            max_pending_bytes=self._config.max_pending_bytes,
        )
        self._audio_format = self._config.audio_format
        self._outbound_rate: int | None = None
        self._connection_generation = 0

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        await super().connect()
        self._ensure_browser_event_forwarder()

    async def disconnect(self) -> None:
        self._close_browser_event_forwarder()
        try:
            await super().disconnect()
        finally:
            # A concurrent connect may finish and install its forwarder after
            # the pre-lock close above but before the serialized server
            # teardown acquires the lifecycle lock. Close again after teardown
            # so the losing connect cannot leave an event-bus subscription
            # attached to a disconnected transport.
            self._close_browser_event_forwarder()

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

        self._connection_generation += 1
        connection_generation = self._connection_generation
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
            if await self._send_ready(ws):
                await self._run_receive_loop(ws, connection_generation)
        finally:
            self._finish_websocket(ws)

    def version_info(self) -> dict[str, str]:
        return make_version_info("websocket", "websockets")


class WebSocketConnectionTransport(_WebSocketProtocolMixin):
    """Transport bound to a single existing WebSocket connection.

    Useful for servers that already own the WebSocket accept loop and want
    one EasyCat Session per client connection.
    """

    transport_kind = "websocket"
    default_echo_cancellation_enabled = False

    def __init__(
        self,
        ws: ServerConnection,
        config: WebSocketTransportConfig | None = None,
    ) -> None:
        self._ws: ServerConnection | None = ws
        self._config = config or WebSocketTransportConfig()
        self._audio_format = self._config.audio_format
        self._outbound_rate: int | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._disconnect_task: asyncio.Task[None] | None = None
        self._lifecycle_tasks = BackgroundTaskScope(name="websocket-connection-lifecycle")
        self._connection_generation = 0
        # The constructor-supplied accepted socket supports one lifecycle.
        # Once connect starts, remote EOF or local teardown is terminal; a
        # later connect must not report success with no socket.
        self._socket_consumed = False
        self._disconnect_cleanup_error: Exception | None = None
        self._init_audio_queue(
            self._config.max_pending_chunks,
            self._config.max_pending_bytes,
        )

    @property
    def request(self) -> Any | None:
        """Accepted WebSocket handshake request, when exposed by ``websockets``."""
        return getattr(self._ws, "request", None)

    async def connect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._connect_task is current:
            return
        connect_task = self._connect_task
        leader = connect_task is None or connect_task.done()
        if leader:
            connect_task = self._lifecycle_tasks.create_task(
                "websocket-connection-connect",
                self._connect_transaction(),
                log_errors=False,
            )
            self._connect_task = connect_task
        assert connect_task is not None
        if leader:
            await connect_task
        else:
            await asyncio.shield(connect_task)

    async def _connect_transaction(self) -> None:
        """Run one shared ready-handshake attempt for all concurrent callers."""
        if self._connected:
            return
        if self._disconnect_cleanup_error is not None:
            raise RuntimeError(
                "WebSocket connection cleanup is incomplete; call disconnect() "
                "again before reconnecting"
            ) from self._disconnect_cleanup_error
        ws = self._ws
        if self._socket_consumed:
            if ws is not None:
                raise RuntimeError(
                    "WebSocket accepted connection has ended; call disconnect() "
                    "to finish socket cleanup"
                )
            raise RuntimeError("WebSocket accepted connection is already closed")
        if ws is None:
            raise RuntimeError("WebSocket accepted connection is already closed")
        self._socket_consumed = True
        self._connection_generation += 1
        generation = self._connection_generation
        self._reset_audio_queue()
        self._connected = True
        self._client_connected.set()
        self._audio_format = self._config.audio_format
        self._outbound_rate = None
        self._ensure_browser_event_forwarder()
        try:
            if not await self._send_ready(ws):
                self._finish_websocket(ws)
                return
        except BaseException:
            # Keep the accepted socket reachable so disconnect() can close it.
            # Clearing _ws here leaks the connection when ready-send
            # cancellation or a non-ConnectionClosed send error interrupts
            # connect().
            self._connected = False
            self._client_connected.clear()
            self._audio_format = self._config.audio_format
            self._outbound_rate = None
            self._enqueue_sentinel()
            self._after_websocket_finished()
            raise
        if generation != self._connection_generation or not self._connected or self._ws is not ws:
            raise ConnectionError("WebSocket transport disconnected during connect")
        self._receive_task = asyncio.create_task(self._run_receive_loop(ws, generation))

    async def disconnect(self) -> None:
        current = asyncio.current_task()
        disconnect_task = self._disconnect_task
        if current is not None and disconnect_task is current:
            return
        if (
            current is not None
            and current in self._emit_tasks
            and disconnect_task is not None
            and not disconnect_task.done()
        ):
            # The shared transaction is draining this tracked emit task. An
            # Error subscriber that re-enters disconnect must not join the
            # transaction which is itself waiting for that subscriber.
            return
        leader = disconnect_task is None or disconnect_task.done()
        if leader:
            emit_initiator = current if current in self._emit_tasks else None
            disconnect_task = self._lifecycle_tasks.create_task(
                "websocket-connection-disconnect",
                self._disconnect_transaction(emit_initiator=emit_initiator),
                log_errors=False,
            )
            self._disconnect_task = disconnect_task
        assert disconnect_task is not None
        if leader:
            # Preserve the existing contract: cancelling the initiating caller
            # cancels cleanup and publishes retry ownership.
            await disconnect_task
        else:
            # A follower may abandon its wait without cancelling the cleanup
            # transaction owned by the initiating caller.
            await asyncio.shield(disconnect_task)

    async def _disconnect_transaction(
        self,
        *,
        emit_initiator: asyncio.Task[Any] | None,
    ) -> None:
        """Run one shared teardown attempt for all concurrent callers."""
        receive_task = self._receive_task
        if (
            self._disconnect_cleanup_error is None
            and not self._connected
            and self._ws is None
            and (receive_task is None or receive_task.done())
            and self._browser_event_forwarder is None
            and not self._emit_tasks
        ):
            return
        self._connection_generation += 1
        self._close_browser_event_forwarder()
        self._connected = False
        self._client_connected.clear()
        ws = self._ws
        self._receive_task = None
        cleanup_error: Exception | None = None
        try:
            await self._reap_receive_task_for_disconnect(receive_task)
            if ws is not None:
                try:
                    await ws.close()
                except Exception as exc:
                    logger.exception("Error closing WebSocket connection")
                    cleanup_error = exc
                    self._disconnect_cleanup_error = exc
                    # The cancelled receive loop's ``finally`` may already have
                    # cleared this reference. Restore cleanup ownership while
                    # keeping the transport publicly disconnected so a later
                    # ``disconnect`` can retry the failed socket close.
                    self._ws = ws
                    self._audio_format = self._config.audio_format
                    self._outbound_rate = None
                    self._enqueue_sentinel()
                    self._after_websocket_finished()
                else:
                    self._finish_websocket(ws)
            if emit_initiator is None:
                await self._drain_emit_tasks()
            else:
                # The initiating Error subscriber is awaiting this child
                # transaction. Exclude that exact task from the drain snapshot
                # so the transaction cannot wait on its own caller.
                emit_tasks = [task for task in self._emit_tasks if task is not emit_initiator]
                if emit_tasks:
                    await asyncio.gather(*emit_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            self._publish_interrupted_disconnect(ws)
            raise
        self._disconnect_cleanup_error = cleanup_error
        if cleanup_error is not None:
            raise cleanup_error

    async def _reap_receive_task_for_disconnect(
        self,
        receive_task: asyncio.Task[None] | None,
    ) -> None:
        """Cancel the receive loop without consuming caller cancellation."""
        if receive_task is None or receive_task is asyncio.current_task():
            return
        current = asyncio.current_task()
        cancellation_count = current.cancelling() if current is not None else 0
        if not receive_task.done():
            receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            if current is not None and current.cancelling() > cancellation_count:
                raise
        except Exception:
            logger.debug(
                "WebSocket receive loop failed during disconnect",
                exc_info=True,
            )
        if current is not None and current.cancelling() > cancellation_count:
            raise asyncio.CancelledError

    def _publish_interrupted_disconnect(self, ws: ServerConnection | None) -> None:
        """Retain the accepted socket so a later disconnect can retry cleanup."""
        if ws is not None:
            self._ws = ws
        self._audio_format = self._config.audio_format
        self._outbound_rate = None
        self._enqueue_sentinel()
        self._after_websocket_finished()
        self._disconnect_cleanup_error = RuntimeError(
            "WebSocket connection disconnect was interrupted by cancellation"
        )

    def _after_websocket_finished(self) -> None:
        self._connected = False
        if self._receive_task is asyncio.current_task():
            self._receive_task = None
        self._close_browser_event_forwarder()

    def _websocket_is_active(self, ws: ServerConnection) -> bool:
        return self._connected and super()._websocket_is_active(ws)

    def version_info(self) -> dict[str, str]:
        return make_version_info("websocket-connection", "websockets")
