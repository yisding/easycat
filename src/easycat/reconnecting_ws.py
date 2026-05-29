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
        # Set while a live socket is available, cleared during a reconnect
        # window. ``send()``/``recv()`` await this (with a timeout) so a
        # concurrent write blocks briefly across a recv_iter-driven reconnect
        # instead of racing against a half-replaced socket.
        self._connected = asyncio.Event()
        # True once an initial connection has succeeded. Before that,
        # send()/recv() fail fast rather than waiting on a reconnect that
        # isn't happening.
        self._ever_connected = False
        # How long send()/recv() wait for an in-progress reconnect before
        # giving up. Bounded so a write (or a best-effort cancel frame) does
        # not stall the pipeline for a full backoff budget; if the reconnect
        # is slower than this the write fails and defers to turn-level retry.
        self._send_wait_timeout = min(self._config.max_delay, max(self._config.base_delay, 5.0))

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.close_code is None

    async def connect(self) -> None:
        """Establish the WebSocket connection."""
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("WebSocket has been closed")
            if self.is_connected:
                return
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
                self._connected.set()
                self._ever_connected = True
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

    async def _await_connected(self) -> ClientConnection:
        """Wait for a live socket, snapshot it, and return it.

        Blocks briefly while a ``recv_iter``-driven reconnect swaps in a new
        connection so a concurrent ``send()``/``recv()`` does not race against
        a half-replaced or half-open socket. Snapshotting ``self._ws`` into a
        local guards against it being reassigned out from under us between the
        ``None`` check and the actual I/O call.
        """
        if self._closed:
            raise RuntimeError("WebSocket has been closed")
        if not self._connected.is_set():
            # Only wait if a reconnect could plausibly restore the socket.
            # A socket that has never connected fails fast.
            if not self._ever_connected:
                raise RuntimeError("WebSocket is not connected")
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=self._send_wait_timeout)
            except TimeoutError as exc:
                raise RuntimeError("WebSocket is not connected") from exc
        ws = self._ws
        if ws is None:
            raise RuntimeError("WebSocket is not connected")
        return ws

    async def send(self, message: str | bytes) -> None:
        """Send a message over the WebSocket.

        Best-effort across a reconnect: if a ``recv_iter``-driven reconnect is
        in flight, the send blocks (up to ``max_delay``) for the new socket
        rather than failing against the closing one.
        """
        ws = await self._await_connected()
        await ws.send(message)

    async def recv(self) -> str | bytes:
        """Receive a message from the WebSocket."""
        ws = await self._await_connected()
        return await ws.recv()

    async def recv_iter(self) -> AsyncIterator[str | bytes]:
        """Iterate over incoming messages, reconnecting on transient drops.

        Behaviour on ``ConnectionClosed`` depends on whether an
        ``on_reconnect`` callback was configured:

        - **With** an ``on_reconnect`` hook, the connection is re-established
          using the same retry/backoff policy as the initial ``connect()``.
          The hook re-primes provider session state, then iteration resumes.
          If reconnection ultimately fails, the iterator ends cleanly.
        - **Without** an ``on_reconnect`` hook the drop is propagated: the
          ``ConnectionClosed`` exception is re-raised into the consumer.
          Stateful providers that send one-shot init frames cannot safely
          resume a half-open stream, so they surface the error for a clean
          restart instead of silently reconnecting into a broken session.

        If the socket was explicitly closed via ``close()``, the iterator
        ends cleanly in both cases.
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
                # The socket is gone; block concurrent sends until reconnect.
                self._connected.clear()
                if self._closed:
                    return
                rcvd = getattr(exc, "rcvd", None)
                close_code = rcvd.code if rcvd is not None else getattr(exc, "close_code", None)
                if self._on_reconnect is None:
                    logger.warning(
                        "WebSocket connection lost (code=%s). No on_reconnect callback "
                        "configured; propagating ConnectionClosed for a clean restart.",
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
                    return

    async def close(self) -> None:
        """Close the WebSocket connection permanently.

        Sets ``_closed`` *before* acquiring the lock so that any in-progress
        ``_connect_with_retry`` loop sees the flag and exits promptly,
        releasing the lock without completing the full backoff sequence.
        """
        self._closed = True
        # Wake any sender blocked in ``_await_connected``; it will observe the
        # closed flag / cleared socket and raise instead of hanging.
        self._connected.set()
        async with self._connect_lock:
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    logger.debug("Error closing WebSocket", exc_info=True)
                finally:
                    self._ws = None
        self._connected.clear()

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
