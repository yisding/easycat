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

import websockets
from websockets.asyncio.server import ServerConnection

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.transports._base import _ServerTransportBase

logger = logging.getLogger(__name__)


@dataclass
class WebSocketTransportConfig:
    """Configuration for :class:`WebSocketTransport`."""

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

    _transport_name = "WebSocket"

    def __init__(self, config: WebSocketTransportConfig | None = None) -> None:
        self._config = config or WebSocketTransportConfig()
        super().__init__(
            host=self._config.host,
            port=self._config.port,
            max_pending_chunks=self._config.max_pending_chunks,
        )
        self._audio_format = self._config.audio_format
        self._client_connected = asyncio.Event()

    # ── Transport protocol ────────────────────────────────────────

    async def disconnect(self) -> None:
        """Disconnect the current client and stop the server."""
        await super().disconnect()
        self._client_connected.clear()

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the connected WebSocket client as a binary frame."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(chunk.data)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: client disconnected")

    async def clear_audio(self) -> None:
        """No-op — WebSocket sends frames immediately without buffering."""

    # ── Server helpers ────────────────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Handle a single client connection."""
        if self._ws is not None:
            logger.warning("Rejecting additional WebSocket client (only one session supported)")
            await ws.close(4000, "Only one session at a time")
            return

        self._ws = ws
        self._client_connected.set()
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
            # Signal end of stream.
            try:
                self._in_queue.put_nowait(None)
            except asyncio.QueueFull:
                # Sentinel already enqueued or consumer stopped reading; safe to ignore.
                logger.debug("Input queue full during client disconnect; dropping sentinel")

    async def _receive_loop(self, ws: ServerConnection) -> None:
        """Read messages from the client connection."""
        async for message in ws:
            if isinstance(message, bytes):
                chunk = AudioChunk(data=message, format=self._audio_format)
                self._enqueue_chunk(chunk, context="WebSocket")
            elif isinstance(message, str):
                self._handle_control_message(message)

    def _handle_control_message(self, raw: str) -> None:
        """Process a JSON control message from the client."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON control message")
            return

        msg_type = msg.get("type")
        if msg_type == "config":
            sample_rate = msg.get("sample_rate")
            if sample_rate and isinstance(sample_rate, int):
                self._audio_format = AudioFormat(
                    sample_rate=sample_rate,
                    channels=self._audio_format.channels,
                    sample_width=self._audio_format.sample_width,
                    encoding=self._audio_format.encoding,
                )
                logger.info("Client negotiated audio format: %s", self._audio_format)
        elif msg_type == "start":
            logger.debug("Client sent start signal")
        elif msg_type == "stop":
            logger.debug("Client sent stop signal")
        else:
            logger.debug("Unknown control message type: %s", msg_type)

    # ── Properties ────────────────────────────────────────────────

    @property
    def has_client(self) -> bool:
        return self._ws is not None

    async def wait_for_client(self, timeout: float | None = None) -> None:
        """Block until a client connects (or timeout expires)."""
        await asyncio.wait_for(self._client_connected.wait(), timeout=timeout)
