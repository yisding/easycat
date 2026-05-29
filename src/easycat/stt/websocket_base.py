"""Shared WebSocket lifecycle helpers for streaming STT providers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from easycat.reconnecting_ws import ReconnectCallback, ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase

logger = logging.getLogger(__name__)


async def _noop_reconnect() -> None:
    """Present-but-empty reconnect hook.

    Passed to :class:`ReconnectingWebSocket` for providers whose entire
    session config travels in the connection URL (query params), so no
    re-configuration is needed after a transparent reconnect.  Its mere
    presence flips ``recv_iter`` from "re-raise on drop" to "reconnect and
    keep yielding"; without it those providers would silently die on any
    transient disconnect.
    """


class WebSocketSTTBase(STTBase):
    """Base class for STT providers backed by a streaming WebSocket."""

    def __init__(
        self,
        *,
        provider_name: str,
        provider_error_name: str,
        expected_sample_rate: int | None = None,
        close_timeout: float = 2.0,
    ) -> None:
        super().__init__(expected_sample_rate=expected_sample_rate)
        self._provider_name = provider_name
        self._provider_error_name = provider_error_name
        self._close_timeout = close_timeout
        self._ws: ReconnectingWebSocket | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._provider_event_bus: Any | None = None
        # Strong references to fire-and-forget Error-emit tasks so the event
        # loop does not garbage-collect them before ``bus.emit`` completes.
        self._emit_tasks: set[asyncio.Task[Any]] = set()

    async def _connect_websocket(
        self,
        *,
        url: str,
        headers: dict[str, str],
        event_bus: Any | None = None,
        connect_fn: Any | None = None,
        on_reconnect: ReconnectCallback | None = None,
    ) -> ReconnectingWebSocket:
        # Query-param-configured providers (e.g. Deepgram, Cartesia) carry
        # their entire session config in the URL, so they need no re-config
        # callback — but ``recv_iter`` only reconnects when *some* hook is
        # present.  Default to a no-op so transient drops reconnect instead
        # of ending the receive loop and silently killing the stream.
        ws = ReconnectingWebSocket(
            url=url,
            config=ReconnectConfig(extra_headers=headers),
            event_bus=event_bus,
            provider_name=self._provider_name,
            connect_fn=connect_fn,
            on_reconnect=on_reconnect or _noop_reconnect,
        )
        self._ws = ws
        self._provider_event_bus = event_bus
        await ws.connect()
        self._receive_task = asyncio.create_task(self._receive_loop())
        return ws

    async def _send_ws(self, message: str | bytes) -> None:
        if self._ws is not None:
            await self._ws.send(message)

    async def _send_json_control(self, payload: dict[str, Any], *, label: str) -> bool:
        if self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            logger.debug("Error sending %s", label, exc_info=True)
            return False
        return True

    async def _close_active_websocket(self) -> None:
        """Drain the receive loop, then close the underlying WebSocket."""
        ws = self._ws
        receive_task = self._receive_task
        if ws is None:
            return
        try:
            await self._drain_and_close(ws, receive_task)
        finally:
            if self._ws is ws:
                self._ws = None
            if self._receive_task is receive_task:
                self._receive_task = None
            self._provider_event_bus = None

    async def _drain_and_close(
        self,
        ws: ReconnectingWebSocket,
        receive_task: asyncio.Task[None] | None,
    ) -> None:
        try:
            if receive_task is not None:
                try:
                    await asyncio.wait_for(receive_task, timeout=self._close_timeout)
                except TimeoutError:
                    receive_task.cancel()
                    try:
                        await receive_task
                    except asyncio.CancelledError:
                        pass
                    logger.warning("%s receive loop timed out on close", self._provider_log_label)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        "%s close ignored receive-loop error",
                        self._provider_log_label,
                        exc_info=True,
                    )
        finally:
            await ws.close()

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        queue = self._event_queue
        try:
            async for raw_message in self._ws.recv_iter():
                if isinstance(raw_message, bytes):
                    await self._handle_ws_bytes_message(raw_message)
                    continue
                try:
                    msg = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                self._handle_json_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("%s WebSocket closed", self._provider_log_label)
        except Exception:
            logger.exception("Error in %s receive loop", self._provider_log_label)
        finally:
            queue.put_nowait(None)

    async def _handle_ws_bytes_message(self, message: bytes) -> None:
        """Handle binary messages from the provider. Default policy ignores them."""

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        """Handle one decoded JSON message from the provider."""
        raise NotImplementedError

    def _emit_provider_error_from_message(
        self,
        msg: dict[str, Any],
        *,
        default_message: str | None = None,
    ) -> None:
        message = msg.get("message") or msg.get("title") or default_message or "unknown error"
        exc = RuntimeError(f"{self._provider_log_label} STT error: {message}")
        self._emit_provider_error(
            exc,
            code=msg.get("code"),
            status_code=msg.get("status_code"),
        )

    def _emit_provider_error(self, exc: BaseException, **context: Any) -> None:
        bus = self._provider_event_bus
        if bus is None:
            return
        from easycat.events import Error, ErrorStage

        for key, value in context.items():
            if value is None:
                continue
            try:
                exc.add_note(f"{key}={value}")  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - pre-3.11
                pass
        try:
            task = asyncio.create_task(
                bus.emit(
                    Error(
                        exception=exc,
                        stage=ErrorStage.STT,
                        provider=self._provider_error_name,
                    )
                )
            )
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error - no running loop", exc_info=True)
            return
        # Keep a strong reference until the emit completes; the event loop
        # only holds a weak one, so an untracked task can be GC'd mid-flight.
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    @property
    def _provider_log_label(self) -> str:
        return self._provider_error_name.replace("-", " ").title()
