"""WebSocket transport: server-side WebSocket for browser/mobile clients.

Hosts a WebSocket server on a configurable port. Each client connection maps
to a single audio session. The wire protocol uses:
  - **Binary frames** for raw PCM16 audio chunks.
  - **Text frames** for JSON control messages (``start``, ``stop``, ``config``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import ClassVar

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._audio_utils import resample_chunk
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.transports._base import _AudioQueueMixin, _ServerTransportBase

logger = logging.getLogger(__name__)
_MIN_NEGOTIATED_SAMPLE_RATE = 8000
_MAX_NEGOTIATED_SAMPLE_RATE = 384000

# WebSocket-specific ``TransportDegraded.reason`` codes emitted on the session
# event bus (via the inherited ``_AudioQueueMixin._emit_degraded``).  These
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

    host: str = "0.0.0.0"
    port: int = 8765
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200


class WebSocketTransport(_ServerTransportBase):
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
      - Text frame: JSON control message (e.g., ``{"type": "ready"}``).
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
        logger.info("WebSocket client connected")

        try:
            await ws.send(json.dumps({"type": "ready"}))
            await self._receive_loop(ws)
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket client disconnected")
        finally:
            self._ws = None
            self._client_connected.clear()
            # Reset negotiated format so the next client starts fresh.
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


class WebSocketConnectionTransport(_AudioQueueMixin):
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
        await self._ws.send(json.dumps({"type": "ready"}))
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        if not self._connected:
            return
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
