"""Reconnecting WebSocket wrapper with full reliability support.

Provides automatic reconnection for WebSocket connections used by
STT, TTS, and transport providers. This is the single source of
reconnect logic in EasyCat.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

# Callback types for reconnection hooks
ReconnectCallback = Callable[[], Coroutine[Any, Any, None]] | Callable[[], None]


@dataclass
class ReconnectConfig:
    """Configuration for reconnection behavior."""

    max_retries: int = 3  # 0 = no retries, -1 = unlimited
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter_factor: float = 0.5  # 0.0 = no jitter, 1.0 = full jitter
    extra_headers: dict[str, str] = field(default_factory=dict)


class ReconnectingWebSocket:
    """WebSocket client with automatic reconnection.

    Wraps websockets library to provide:
    - Automatic reconnection with exponential backoff and jitter
    - EventBus integration for reconnect.attempt/success/failure events
    - Callbacks for provider-specific recovery logic
    - Send/receive methods that handle disconnection transparently
    - Clean shutdown via close()
    """

    def __init__(
        self,
        url: str,
        config: ReconnectConfig | None = None,
        event_bus: Any | None = None,
        provider_name: str = "websocket",
        connect_fn: Callable[..., Coroutine[Any, Any, ClientConnection]] | None = None,
        on_reconnect: ReconnectCallback | None = None,
        on_give_up: ReconnectCallback | None = None,
    ) -> None:
        self._url = url
        self._config = config or ReconnectConfig()
        self._event_bus = event_bus
        self._provider_name = provider_name
        self._connect_fn = connect_fn
        self._on_reconnect = on_reconnect
        self._on_give_up = on_give_up
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

    def _compute_delay(self, base_delay: float) -> float:
        """Compute delay with jitter applied."""
        jitter = self._config.jitter_factor
        if jitter <= 0:
            return base_delay
        # Apply jitter: delay * (1 - jitter + random * 2 * jitter)
        return base_delay * (1.0 - jitter + random.random() * 2.0 * jitter)

    def _max_attempts(self) -> int | None:
        """Return the max number of connect attempts, or None for unlimited."""
        if self._config.max_retries < 0:
            return None  # unlimited
        return self._config.max_retries + 1

    async def _connect_with_retry(self) -> None:
        """Connect with exponential backoff and jitter retry."""
        delay = self._config.base_delay
        last_error: Exception | None = None
        max_attempts = self._max_attempts()
        attempt = 0

        while True:
            if self._closed:
                raise ConnectionError("WebSocket closed during reconnect")
            try:
                await self._emit_reconnect_attempt(attempt + 1)
                connect_fn = self._connect_fn or websockets.connect
                self._ws = await connect_fn(
                    self._url,
                    additional_headers=self._config.extra_headers,
                )
                logger.debug("WebSocket connected to %s (attempt %d)", self._url, attempt + 1)
                await self._emit_reconnect_success()
                if attempt > 0 and self._on_reconnect:
                    await self._invoke_callback(self._on_reconnect)
                return
            except Exception as exc:
                last_error = exc
                attempt += 1
                if max_attempts is not None and attempt >= max_attempts:
                    break
                jittered_delay = self._compute_delay(delay)
                logger.warning(
                    "WebSocket connection attempt %d failed: %s. Retrying in %.1fs",
                    attempt,
                    exc,
                    jittered_delay,
                )
                await asyncio.sleep(jittered_delay)
                delay = min(delay * self._config.backoff_factor, self._config.max_delay)

        await self._emit_reconnect_failure(str(last_error))
        if self._on_give_up:
            await self._invoke_callback(self._on_give_up)
        raise ConnectionError(f"Failed to connect after {attempt} attempts") from last_error

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
        """Iterate over incoming messages, reconnecting on transient drops.

        On ``ConnectionClosed``, attempts to re-establish the connection
        using the same retry/backoff policy as the initial ``connect()``.
        If reconnection fails (or the socket was explicitly closed via
        ``close()``), the iterator ends.
        """
        if self._ws is None:
            if self._closed:
                return
            raise RuntimeError("WebSocket is not connected")

        while True:
            try:
                async for message in self._ws:
                    yield message
                # Clean end-of-stream (server closed normally) — done.
                return
            except websockets.exceptions.ConnectionClosed as exc:
                if self._closed:
                    return
                rcvd = getattr(exc, "rcvd", None)
                close_code = rcvd.code if rcvd is not None else getattr(exc, "close_code", None)
                if self._on_reconnect is None:
                    logger.warning(
                        "WebSocket connection lost (code=%s). No on_reconnect callback "
                        "configured; ending recv_iter to allow clean restart.",
                        close_code,
                    )
                    raise
                logger.warning(
                    "WebSocket connection lost (code=%s). Attempting reconnect…",
                    close_code,
                )
                try:
                    async with self._connect_lock:
                        await self._connect_with_retry()
                except ConnectionError:
                    logger.error("Reconnection failed; ending recv_iter")
                    raise

    async def close(self) -> None:
        """Close the WebSocket connection permanently.

        Sets ``_closed`` *before* acquiring the lock so that any in-progress
        ``_connect_with_retry`` loop sees the flag and exits promptly,
        releasing the lock without completing the full backoff sequence.
        """
        self._closed = True
        async with self._connect_lock:
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    logger.debug("Error closing WebSocket", exc_info=True)
                finally:
                    self._ws = None

    # ── Event emission helpers ────────────────────────────────────

    async def _emit_reconnect_attempt(self, attempt: int) -> None:
        if self._event_bus is not None:
            from easycat.events import ReconnectAttempt

            await self._event_bus.emit(
                ReconnectAttempt(provider=self._provider_name, attempt=attempt)
            )

    async def _emit_reconnect_success(self) -> None:
        if self._event_bus is not None:
            from easycat.events import ReconnectSuccess

            await self._event_bus.emit(ReconnectSuccess(provider=self._provider_name))

    async def _emit_reconnect_failure(self, error: str) -> None:
        if self._event_bus is not None:
            from easycat.events import ReconnectFailure

            await self._event_bus.emit(ReconnectFailure(provider=self._provider_name, error=error))

    async def _invoke_callback(self, callback: ReconnectCallback) -> None:
        """Invoke a sync or async callback."""
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error in reconnect callback %s", callback)
