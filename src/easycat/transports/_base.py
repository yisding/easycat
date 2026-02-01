"""Internal base class for server-backed WebSocket transports.

Provides the shared connect/disconnect/receive_audio logic used by
both :class:`WebSocketTransport` and :class:`TwilioTransport`.

Not part of the public API — ``__init__.py`` does not export this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import websockets
from websockets.asyncio.server import Server, ServerConnection

from easycat.audio_format import AudioChunk

logger = logging.getLogger(__name__)


class _ServerTransportBase:
    """Base for transports that host a ``websockets`` server.

    Subclasses must provide:
      - ``_transport_name`` (str) — used in log messages (e.g. ``"WebSocket"``).
      - ``_handle_connection(ws)`` — the per-connection coroutine passed to
        ``websockets.serve``.
    """

    _transport_name: str = "Server"

    def __init__(self, host: str, port: int, max_pending_chunks: int) -> None:
        self._host = host
        self._port = port
        self._max_pending_chunks = max_pending_chunks

        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._connected = False
        self._in_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=max_pending_chunks,
        )

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket server."""
        if self._connected:
            return

        # Reinitialize queue to clear any stale sentinels from a previous session.
        self._in_queue = asyncio.Queue(maxsize=self._max_pending_chunks)

        self._server = await websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
        )
        self._connected = True
        logger.info(
            "%s transport listening on ws://%s:%d",
            self._transport_name,
            self._host,
            self._port,
        )

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Override in subclasses to handle a new WebSocket connection."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Disconnect the current client and stop the server."""
        if not self._connected:
            return

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing %s WebSocket", self._transport_name, exc_info=True)
            self._ws = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Signal end of audio to any pending receive_audio iterators.
        try:
            self._in_queue.put_nowait(None)
        except asyncio.QueueFull:
            # Sentinel already enqueued or consumers stopped reading; safe to ignore.
            logger.debug("Input queue full when enqueueing sentinel; ignoring")

        self._connected = False

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Yield audio chunks received from the WebSocket client."""
        while self._connected or not self._in_queue.empty():
            chunk = await self._in_queue.get()
            if chunk is None:
                break
            yield chunk

    # ── Properties ────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected
