"""Internal base classes for transports.

Provides shared infrastructure used by multiple transport implementations.
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


# ── Shared queue / receive_audio logic ────────────────────────────


class _AudioQueueMixin:
    """Mixin that provides the inbound audio queue and ``receive_audio`` iterator.

    Transports that accept audio chunks from an external source can inherit
    this mixin to get the queue management, sentinel-based shutdown, and
    ``receive_audio()`` async iterator for free.

    Also provides a ``_client_connected`` :class:`asyncio.Event` and a
    ``wait_for_client`` helper so that server-style transports can signal
    when a remote peer has connected.

    Users must:
      - Call ``_init_audio_queue(max_pending_chunks)`` during ``__init__``.
      - Set ``self._connected`` to ``True``/``False`` in ``connect``/``disconnect``.
      - Call ``_enqueue_sentinel()`` during ``disconnect`` to signal end-of-stream.
    """

    _connected: bool
    _in_queue: asyncio.Queue[AudioChunk | None]
    _client_connected: asyncio.Event

    def _init_audio_queue(self, max_pending_chunks: int) -> None:
        self._max_pending_chunks = max_pending_chunks
        self._connected = False
        self._in_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=max_pending_chunks,
        )
        self._client_connected = asyncio.Event()

    def _reset_audio_queue(self) -> None:
        """Reinitialize the queue to clear any stale sentinels from a previous session."""
        self._in_queue = asyncio.Queue(maxsize=self._max_pending_chunks)

    def _enqueue_sentinel(self) -> None:
        """Put ``None`` on the queue to signal end-of-stream.

        The sentinel is critical for unblocking ``receive_audio()``, so if the
        queue is full we drain one item to make room rather than silently
        dropping the signal.
        """
        try:
            self._in_queue.put_nowait(None)
        except asyncio.QueueFull:
            # Drop one audio chunk to make room — shutdown signals must not
            # be silently lost.
            try:
                self._in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._in_queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.debug("Input queue full when enqueueing sentinel; ignoring")

    def _enqueue_chunk(self, chunk: AudioChunk, *, context: str) -> None:
        """Best-effort enqueue for inbound audio data.

        Parameters
        ----------
        chunk:
            The audio chunk to enqueue.
        context:
            Log-friendly transport/context name used when the queue is full.
        """
        try:
            self._in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("Inbound %s audio queue full — dropping frame", context)

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Yield audio chunks until a ``None`` sentinel is received."""
        while True:
            chunk = await self._in_queue.get()
            if chunk is None:
                break
            yield chunk

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def wait_for_client(self, timeout: float | None = None) -> None:
        """Block until a remote peer / client connects (or *timeout* expires)."""
        await asyncio.wait_for(self._client_connected.wait(), timeout=timeout)


# ── WebSocket server base ─────────────────────────────────────────


class _ServerTransportBase(_AudioQueueMixin):
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
        self._init_audio_queue(max_pending_chunks)

        self._server: Server | None = None
        self._ws: ServerConnection | None = None

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket server."""
        if self._connected:
            return

        self._reset_audio_queue()

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
        self._client_connected.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        self._enqueue_sentinel()
        self._connected = False

    @property
    def has_client(self) -> bool:
        return self._ws is not None
