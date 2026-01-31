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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import websockets
from websockets.asyncio.server import Server, ServerConnection

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat

logger = logging.getLogger(__name__)


@dataclass
class WebSocketTransportConfig:
    """Configuration for :class:`WebSocketTransport`."""

    host: str = "0.0.0.0"
    port: int = 8765
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200


class WebSocketTransport:
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

    def __init__(self, config: WebSocketTransportConfig | None = None) -> None:
        self._config = config or WebSocketTransportConfig()
        self._audio_format = self._config.audio_format
        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._connected = False
        self._client_connected = asyncio.Event()

        self._in_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=self._config.max_pending_chunks,
        )

        # Task running the per-client receive loop.
        self._recv_task: asyncio.Task[None] | None = None

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket server and wait for one client to connect."""
        if self._connected:
            return

        self._server = await websockets.serve(
            self._handle_client,
            self._config.host,
            self._config.port,
        )
        self._connected = True
        logger.info(
            "WebSocket transport listening on ws://%s:%d",
            self._config.host,
            self._config.port,
        )

    async def disconnect(self) -> None:
        """Disconnect the current client and stop the server."""
        if not self._connected:
            return

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing client WebSocket", exc_info=True)
            self._ws = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Signal end of audio to any pending receive_audio iterators.
        try:
            self._in_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        self._connected = False
        self._client_connected.clear()

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Yield audio chunks received from the WebSocket client."""
        while self._connected or not self._in_queue.empty():
            chunk = await self._in_queue.get()
            if chunk is None:
                break
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the connected WebSocket client as a binary frame."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(chunk.data)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: client disconnected")

    # ── Server helpers ────────────────────────────────────────────

    async def _handle_client(self, ws: ServerConnection) -> None:
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
                pass

    async def _receive_loop(self, ws: ServerConnection) -> None:
        """Read messages from the client connection."""
        async for message in ws:
            if isinstance(message, bytes):
                chunk = AudioChunk(data=message, format=self._audio_format)
                try:
                    self._in_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    logger.warning("Inbound audio queue full — dropping frame")
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
    def is_connected(self) -> bool:
        return self._connected

    @property
    def has_client(self) -> bool:
        return self._ws is not None

    async def wait_for_client(self, timeout: float | None = None) -> None:
        """Block until a client connects (or timeout expires)."""
        await asyncio.wait_for(self._client_connected.wait(), timeout=timeout)
