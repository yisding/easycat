"""Reconnecting WebSocket wrapper.

Minimal implementation providing automatic reconnection for WebSocket
connections used by TTS (and later STT) providers. This will eventually
be superseded by WS8's full reliability implementation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)


@dataclass
class ReconnectConfig:
    """Configuration for reconnection behavior."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    extra_headers: dict[str, str] = field(default_factory=dict)


class ReconnectingWebSocket:
    """WebSocket client with automatic reconnection.

    Wraps websockets library to provide:
    - Automatic reconnection with exponential backoff
    - Send/receive methods that handle disconnection transparently
    - Clean shutdown via close()
    """

    def __init__(self, url: str, config: ReconnectConfig | None = None) -> None:
        self._url = url
        self._config = config or ReconnectConfig()
        self._ws: ClientConnection | None = None
        self._closed = False
        self._connect_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.close_code is None

    async def connect(self) -> None:
        """Establish the WebSocket connection."""
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("WebSocket has been closed")
            await self._connect_with_retry()

    async def _connect_with_retry(self) -> None:
        """Connect with exponential backoff retry."""
        delay = self._config.base_delay
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries + 1):
            try:
                self._ws = await websockets.connect(
                    self._url,
                    additional_headers=self._config.extra_headers,
                )
                logger.debug("WebSocket connected to %s (attempt %d)", self._url, attempt + 1)
                return
            except Exception as exc:
                last_error = exc
                if attempt < self._config.max_retries:
                    logger.warning(
                        "WebSocket connection attempt %d failed: %s. Retrying in %.1fs",
                        attempt + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * self._config.backoff_factor, self._config.max_delay)

        raise ConnectionError(
            f"Failed to connect after {self._config.max_retries + 1} attempts"
        ) from last_error

    async def send(self, message: str | bytes) -> None:
        """Send a message over the WebSocket."""
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        await self._ws.send(message)

    async def recv(self) -> str | bytes:
        """Receive a message from the WebSocket."""
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        return await self._ws.recv()

    async def recv_iter(self) -> AsyncIterator[str | bytes]:
        """Iterate over incoming messages until the connection closes."""
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        try:
            async for message in self._ws:
                yield message
        except websockets.exceptions.ConnectionClosed:
            logger.debug("WebSocket connection closed during recv_iter")

    async def close(self) -> None:
        """Close the WebSocket connection permanently."""
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing WebSocket", exc_info=True)
            finally:
                self._ws = None
