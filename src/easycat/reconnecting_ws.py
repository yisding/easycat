"""Reconnecting WebSocket wrapper with full reliability support.

Provides automatic reconnection for WebSocket connections used by
STT, TTS, and transport providers. This is the single source of
reconnect logic in EasyCat.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from easycat._numeric import is_finite_number

logger = logging.getLogger(__name__)

# Callback types for connection lifecycle hooks.
ReconnectCallback = Callable[[], Coroutine[Any, Any, None] | None]
DisconnectCallback = Callable[
    [websockets.exceptions.ConnectionClosed],
    Coroutine[Any, Any, None] | None,
]


@dataclass
class ReconnectConfig:
    """Configuration for reconnection behavior."""

    max_retries: int = 3  # 0 = no retries, -1 = unlimited
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter_factor: float = 0.5  # 0.0 = no jitter, 1.0 = full jitter
    extra_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ValueError("max_retries must be an integer")
        if self.max_retries < -1:
            raise ValueError("max_retries must be -1 for unlimited retries or >= 0")

        _validate_positive_number("base_delay", self.base_delay)
        _validate_positive_number("max_delay", self.max_delay)
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be greater than or equal to base_delay")

        _validate_positive_number("backoff_factor", self.backoff_factor)
        if self.backoff_factor < 1.0:
            raise ValueError("backoff_factor must be >= 1.0")

        if not isinstance(self.jitter_factor, int | float) or isinstance(self.jitter_factor, bool):
            raise ValueError("jitter_factor must be a number between 0.0 and 1.0")
        if not is_finite_number(self.jitter_factor):
            raise ValueError("jitter_factor must be a finite number between 0.0 and 1.0")
        if self.jitter_factor < 0.0 or self.jitter_factor > 1.0:
            raise ValueError("jitter_factor must be between 0.0 and 1.0")


def _validate_positive_number(name: str, value: object) -> None:
    if not is_finite_number(value):
        raise ValueError(f"{name} must be a finite number > 0")
    if value <= 0:
        raise ValueError(f"{name} must be a finite number > 0")


async def connect_until_stopped(
    ws: ReconnectingWebSocket,
    stop_event: asyncio.Event,
) -> bool:
    """Connect a reconnecting socket unless shutdown is requested first.

    This is the common wrapper for ``ReconnectConfig(max_retries=-1)`` clients:
    the initial ``connect()`` can retry forever while the server is down, so
    callers should race it against their shutdown event instead of making
    Ctrl-C wait for a server to appear.

    Returns:
        ``True`` when the socket connected. ``False`` when *stop_event* fired
        first; in that case the socket is closed before returning.
    """
    connect_task = asyncio.create_task(ws.connect())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {connect_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException as wait_error:
        connect_task.cancel()
        try:
            await ws.close()
        except BaseException as close_error:
            wait_error.add_note(
                f"WebSocket cleanup after interrupted connect failed: {close_error!r}"
            )
        await asyncio.gather(connect_task, return_exceptions=True)
        raise
    finally:
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)

    if connect_task not in done:
        connect_task.cancel()
        try:
            await ws.close()
        finally:
            await asyncio.gather(connect_task, return_exceptions=True)
        return False

    connect_task.result()
    return True


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
        on_disconnect: DisconnectCallback | None = None,
    ) -> None:
        self._url = url
        self._config = config or ReconnectConfig()
        self._event_bus = event_bus
        self._provider_name = provider_name
        self._connect_fn = connect_fn
        self._on_reconnect = on_reconnect
        self._on_give_up = on_give_up
        self._on_disconnect = on_disconnect
        self._ws: ClientConnection | None = None
        # Every connection remains cleanup-owned until its close succeeds,
        # whether it was committed, rolled back during installation, or
        # arrived late from a cancellation-resistant connector.
        self._pending_connection_closes: list[ClientConnection] = []
        self._connection_cleanup_lock = asyncio.Lock()
        self._connection_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        # Set when recv_iter ends because the reconnect budget was exhausted or a
        # reconnect ultimately failed (terminal mid-stream death), as opposed to a
        # deliberate close() or a clean end-of-stream. Lets STT/TTS receive loops
        # surface an Error instead of a silent empty transcript.
        self._died_abnormally = False
        self._last_connect_attempts = 0
        self._reconnect_attempts_exhausted: int | None = None
        self._reconnect_exhaustion_reason: str | None = None
        self._connect_lock = asyncio.Lock()
        self._close_event = asyncio.Event()
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

    @property
    def died_abnormally(self) -> bool:
        return self._died_abnormally

    @property
    def reconnect_attempts_exhausted(self) -> int | None:
        return self._reconnect_attempts_exhausted

    @property
    def reconnect_exhaustion_reason(self) -> str | None:
        return self._reconnect_exhaustion_reason

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

    async def _connect_with_retry(self, *, notify_reconnect: bool = False) -> None:
        """Connect with exponential backoff and jitter retry."""
        await self._retry_pending_connection_closes()
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
                candidate = await self._connect_attempt_or_close(
                    connect_fn(
                        self._url,
                        additional_headers=self._config.extra_headers,
                    )
                )
            except Exception as exc:
                if self._closed:
                    raise ConnectionError("WebSocket closed during reconnect") from exc
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
                await self._backoff_or_close(jittered_delay)
                delay = min(delay * self._config.backoff_factor, self._config.max_delay)
                continue

            # The TCP/WebSocket attempt succeeded. Installation includes
            # observer notification and is transactional; observer failures
            # are application errors, not failed network attempts to retry.
            await self._install_connection(candidate, attempt, notify_reconnect)
            return

        self._last_connect_attempts = attempt
        if notify_reconnect:
            self._mark_reconnect_exhausted(attempt, "failed reconnect attempts")
        await self._emit_reconnect_failure(str(last_error))
        if self._on_give_up:
            await self._invoke_callback(self._on_give_up, suppress_errors=True)
        raise ConnectionError(f"Failed to connect after {attempt} attempts") from last_error

    async def _install_connection(
        self,
        candidate: ClientConnection,
        attempt: int,
        notify_reconnect: bool,
    ) -> None:
        if self._closed:
            self._retain_connection_for_close(candidate)
            try:
                await self._close_retained_connection(candidate)
            except BaseException as cleanup_error:
                raise ConnectionError("WebSocket closed during reconnect") from cleanup_error
            raise ConnectionError("WebSocket closed during reconnect")
        previous_ws = self._ws
        was_connected = self._connected.is_set()
        previous_state = (
            self._ever_connected,
            self._died_abnormally,
            self._last_connect_attempts,
            self._reconnect_attempts_exhausted,
            self._reconnect_exhaustion_reason,
        )
        self._ws = candidate
        logger.debug("WebSocket connected to %s (attempt %d)", self._url, attempt + 1)
        self._connected.set()
        try:
            if (notify_reconnect or attempt > 0) and self._on_reconnect:
                await self._invoke_callback(self._on_reconnect)
            await self._emit_reconnect_success()
            if self._closed:
                raise ConnectionError("WebSocket closed during reconnect")
        except BaseException as install_error:
            if not self._closed and self._ws is candidate:
                self._ws = previous_ws
                if was_connected:
                    self._connected.set()
                else:
                    self._connected.clear()
                (
                    self._ever_connected,
                    self._died_abnormally,
                    self._last_connect_attempts,
                    self._reconnect_attempts_exhausted,
                    self._reconnect_exhaustion_reason,
                ) = previous_state
            else:
                self._connected.clear()
            self._retain_connection_for_close(candidate)
            try:
                await self._close_retained_connection(candidate)
            except BaseException as close_error:
                install_error.add_note(f"candidate rollback close failed: {close_error!r}")
            raise

        self._ever_connected = True
        self._died_abnormally = False
        self._last_connect_attempts = 0
        self._reconnect_attempts_exhausted = None
        self._reconnect_exhaustion_reason = None

    def _retain_connection_for_close(self, connection: ClientConnection) -> None:
        """Record cleanup ownership once, comparing connections by identity."""
        if not any(owned is connection for owned in self._pending_connection_closes):
            self._pending_connection_closes.append(connection)

    def _release_connection_after_close(self, connection: ClientConnection) -> None:
        for index, owned in enumerate(self._pending_connection_closes):
            if owned is connection:
                del self._pending_connection_closes[index]
                break

    async def _close_retained_connection(self, connection: ClientConnection) -> None:
        """Close one exact owned connection and release it only on success."""
        async with self._connection_cleanup_lock:
            if not any(owned is connection for owned in self._pending_connection_closes):
                return
            try:
                await connection.close()
            except BaseException:
                raise
            self._release_connection_after_close(connection)

    async def _retry_pending_connection_closes(self) -> None:
        """Finish all retained connection cleanup before reuse or final close."""
        while self._pending_connection_closes:
            connection = self._pending_connection_closes[0]
            caller = asyncio.current_task()
            cancellation_requests = caller.cancelling() if caller is not None else 0
            try:
                await self._close_retained_connection(connection)
            except asyncio.CancelledError as close_error:
                if caller is not None and caller.cancelling() > cancellation_requests:
                    raise
                raise RuntimeError(
                    "Previous WebSocket connection cleanup is incomplete; "
                    "retry close() or connect() after cleanup recovers"
                ) from close_error
            except BaseException as close_error:
                raise RuntimeError(
                    "Previous WebSocket connection cleanup is incomplete; "
                    "retry close() or connect() after cleanup recovers"
                ) from close_error

    async def _connect_attempt_or_close(
        self,
        operation: Awaitable[ClientConnection],
    ) -> ClientConnection:
        """Run one connector call while allowing close() to win immediately."""
        connect_task = asyncio.ensure_future(operation)
        close_task = asyncio.create_task(self._close_event.wait())
        try:
            done, _ = await asyncio.wait(
                {connect_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if connect_task in done:
                return connect_task.result()
            connect_task.cancel()
            raise ConnectionError("WebSocket closed during reconnect")
        except BaseException:
            if not connect_task.done():
                connect_task.cancel()
                connect_task.add_done_callback(self._close_late_connection)
            raise
        finally:
            close_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await close_task

    async def _backoff_or_close(self, delay: float) -> None:
        sleep_task = asyncio.create_task(asyncio.sleep(delay))
        close_task = asyncio.create_task(self._close_event.wait())
        try:
            done, _ = await asyncio.wait(
                {sleep_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if close_task in done:
                raise ConnectionError("WebSocket closed during reconnect")
        finally:
            for task in (sleep_task, close_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, close_task, return_exceptions=True)

    def _close_late_connection(self, task: asyncio.Future[ClientConnection]) -> None:
        """Close a connector result that arrived after its caller stopped waiting."""
        if task.cancelled():
            return
        try:
            connection = task.result()
        except Exception:
            return
        self._retain_connection_for_close(connection)
        close_task = asyncio.create_task(
            self._close_late_retained_connection(connection),
            name="websocket_late_connection_cleanup",
        )
        self._connection_cleanup_tasks.add(close_task)
        close_task.add_done_callback(self._connection_cleanup_tasks.discard)
        close_task.add_done_callback(ReconnectingWebSocket._consume_task_result)

    async def _close_late_retained_connection(self, connection: ClientConnection) -> None:
        """Best-effort immediate cleanup; failures remain retry-owned."""
        try:
            await self._close_retained_connection(connection)
        except BaseException:
            logger.debug("Error closing late WebSocket connection", exc_info=True)
            raise

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    def _mark_reconnect_exhausted(self, attempts: int, reason: str) -> None:
        self._died_abnormally = True
        self._reconnect_attempts_exhausted = attempts
        self._reconnect_exhaustion_reason = reason

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
            # A socket that has never connected fails fast. A socket whose
            # ``_ws`` has been nulled with no reconnect in flight is terminally
            # dead (e.g. recv_iter gave up after a failed reconnect): fast-fail
            # rather than burning the full wait timeout on a known-dead socket.
            if not self._ever_connected:
                raise RuntimeError("WebSocket is not connected")
            if self._ws is None and not self._connect_lock.locked():
                # A closed socket always reports "closed" regardless of where in
                # this method the caller happens to observe it (the exact point
                # is scheduling-dependent across Python versions).
                if self._closed:
                    raise RuntimeError("WebSocket has been closed")
                raise RuntimeError("WebSocket is not connected")
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=self._send_wait_timeout)
            except TimeoutError as exc:
                raise RuntimeError("WebSocket is not connected") from exc
            # close() (and other paths) may wake us by setting ``_connected``;
            # re-check the closed flag before snapshotting a now-closing socket.
            if self._closed:
                raise RuntimeError("WebSocket has been closed")
        ws = self._ws
        if ws is None:
            if self._closed:
                raise RuntimeError("WebSocket has been closed")
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

        # Bound successful reconnect cycles within a single receive stream.
        # ``_connect_with_retry`` caps failed connection *attempts* for one
        # reconnect, but a peer that accepts and immediately drops can otherwise
        # cause unbounded successful reconnect churn. Reuse ``max_retries`` as
        # the per-stream reconnect budget: 0 means fail fast after the first
        # drop, -1 keeps the explicit unlimited behaviour.
        remaining_reconnects = self._config.max_retries

        while True:
            try:
                async for message in self._ws:
                    yield message
                if self._closed or getattr(self._ws, "close_code", None) is None:
                    return
                raise self._normal_close_exception(self._ws)
            except websockets.exceptions.ConnectionClosed as exc:
                if not await self._recover_recv_iter_drop(exc, remaining_reconnects):
                    return
                if remaining_reconnects > 0:
                    remaining_reconnects -= 1

    async def _recover_recv_iter_drop(
        self,
        exc: websockets.exceptions.ConnectionClosed,
        remaining_reconnects: int,
    ) -> bool:
        """Recover ``recv_iter`` after a dropped socket, if policy allows it."""
        # The socket is gone; block concurrent sends until reconnect.
        self._connected.clear()
        if self._closed:
            return False

        if self._on_disconnect is not None:
            await self._invoke_disconnect_callback(self._on_disconnect, exc)
            if self._closed:
                return False

        close_code = self._connection_closed_code(exc)
        if self._on_reconnect is None:
            logger.warning(
                "WebSocket connection lost (code=%s). No on_reconnect callback "
                "configured; propagating ConnectionClosed for a clean restart.",
                close_code,
            )
            raise exc
        if remaining_reconnects == 0:
            logger.error(
                "WebSocket connection lost (code=%s). Reconnect budget exhausted; "
                "ending recv_iter",
                close_code,
            )
            self._mark_reconnect_exhausted(
                max(self._config.max_retries, 0),
                "successful reconnect cycle budget",
            )
            self._ws = None
            return False

        logger.warning(
            "WebSocket connection lost (code=%s). Attempting reconnect…",
            close_code,
        )
        try:
            async with self._connect_lock:
                await self._connect_with_retry(notify_reconnect=True)
        except Exception:
            if self._closed:
                return False
            # Reconnect is exhausted and the socket is terminally dead.
            # Null ``_ws`` (it still references the closed connection)
            # so a later send()/recv() fast-fails in ``_await_connected``
            # instead of waiting out the full reconnect timeout.
            logger.error("Reconnection failed; ending recv_iter")
            if not self._died_abnormally:
                self._mark_reconnect_exhausted(
                    self._last_connect_attempts,
                    "failed reconnect attempts",
                )
            self._ws = None
            return False
        return True

    @staticmethod
    def _connection_closed_code(exc: websockets.exceptions.ConnectionClosed) -> int | None:
        rcvd = getattr(exc, "rcvd", None)
        return rcvd.code if rcvd is not None else getattr(exc, "close_code", None)

    @staticmethod
    def _normal_close_exception(ws: ClientConnection) -> websockets.exceptions.ConnectionClosed:
        protocol_exc = getattr(getattr(ws, "protocol", None), "close_exc", None)
        if isinstance(protocol_exc, websockets.exceptions.ConnectionClosed):
            return protocol_exc
        code = getattr(ws, "close_code", None) or 1000
        reason = getattr(ws, "close_reason", None) or "connection closed"
        frame = websockets.frames.Close(code, reason)
        return websockets.exceptions.ConnectionClosedOK(frame, frame, True)

    async def close(self) -> None:
        """Close the WebSocket connection permanently.

        Sets ``_closed`` *before* acquiring the lock so that any in-progress
        ``_connect_with_retry`` loop sees the flag and exits promptly,
        releasing the lock without completing the full backoff sequence.
        """
        self._closed = True
        self._close_event.set()
        # Wake any sender blocked in ``_await_connected``; it will observe the
        # closed flag / cleared socket and raise instead of hanging.
        self._connected.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            self._retain_connection_for_close(ws)
        self._connected.clear()
        await self._retry_pending_connection_closes()

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

    async def _invoke_callback(
        self,
        callback: ReconnectCallback,
        *,
        suppress_errors: bool = False,
    ) -> None:
        """Invoke a sync or async callback."""
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error in reconnect callback %s", callback)
            if not suppress_errors:
                raise

    async def _invoke_disconnect_callback(
        self,
        callback: DisconnectCallback,
        exc: websockets.exceptions.ConnectionClosed,
    ) -> None:
        """Invoke a sync or async disconnect observer."""
        try:
            result = callback(exc)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error in disconnect callback %s", callback)
