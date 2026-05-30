"""Shared WebSocket lifecycle helpers for streaming STT providers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from easycat._provider_helpers import ProviderErrorEmitter
from easycat.events import ErrorStage
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


class WebSocketSTTBase(ProviderErrorEmitter, STTBase):
    """Base class for STT providers backed by a streaming WebSocket."""

    _error_stage = ErrorStage.STT

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
        self._init_emit_tasks()

    def _resolve_event_bus(self) -> Any | None:
        # STT carries the bus per connection (set in ``_connect_websocket``),
        # not on a static config object like the TTS providers do.
        return self._provider_event_bus

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

    async def _close_active_websocket(self, *, close_before_drain: bool = False) -> None:
        """Drain the receive loop, then close the underlying WebSocket.

        Some providers (e.g. ElevenLabs/OpenAI realtime STT) keep the
        socket open after delivering the final transcript, so draining
        first would block in ``recv_iter`` until the close timeout fires.
        Pass ``close_before_drain=True`` to close the socket up front —
        waking the receive loop so it returns promptly — then drain it.
        ``ReconnectingWebSocket.close()`` is idempotent, so the later
        close in ``_drain_and_close`` is a harmless no-op.
        """
        ws = self._ws
        receive_task = self._receive_task
        if ws is None:
            return
        try:
            await self._drain_and_close(ws, receive_task, close_before_drain=close_before_drain)
        finally:
            if self._ws is ws:
                self._ws = None
            if self._receive_task is receive_task:
                self._receive_task = None
            self._provider_event_bus = None
            await self._drain_emit_tasks()

    async def _drain_and_close(
        self,
        ws: ReconnectingWebSocket,
        receive_task: asyncio.Task[None] | None,
        *,
        close_before_drain: bool = False,
    ) -> None:
        if close_before_drain:
            # Close first to wake a receive loop that would otherwise block
            # waiting for the provider to close a socket it keeps open.
            await ws.close()
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
            self._on_receive_loop_end()
            queue.put_nowait(None)

    async def _handle_ws_bytes_message(self, message: bytes) -> None:
        """Handle binary messages from the provider. Default policy ignores them."""

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        """Handle one decoded JSON message from the provider."""
        raise NotImplementedError

    def _on_receive_loop_end(self) -> None:
        """Hook run once the receive loop exits, before the sentinel is queued.

        The default does nothing.  Providers that gate ``_on_start`` on a
        post-connect "ready" future (e.g. the OpenAI Realtime
        ``session.update`` handshake) override this to fail-fast that
        future when the socket drops before the session is acknowledged —
        otherwise the start waiter would block for its full timeout
        instead of surfacing the close immediately.
        """

    def _emit_provider_error_from_message(
        self,
        msg: dict[str, Any],
        *,
        default_message: str | None = None,
        override_message: str | None = None,
    ) -> None:
        # ``override_message`` lets a provider surface a field it considers
        # more descriptive (e.g. Deepgram's ``description``) ahead of the
        # generic ``message``/``title`` fallbacks.
        message = (
            override_message
            or msg.get("message")
            or msg.get("title")
            or default_message
            or "unknown error"
        )
        exc = RuntimeError(f"{self._provider_log_label} STT error: {message}")
        self._emit_provider_error(
            exc,
            code=msg.get("code"),
            status_code=msg.get("status_code"),
        )

    @property
    def _provider_log_label(self) -> str:
        return self._provider_error_name.replace("-", " ").title()
