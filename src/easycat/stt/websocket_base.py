"""Shared WebSocket lifecycle helpers for streaming STT providers."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any

import websockets

from easycat._audio_utils import to_mono_chunk, validate_pcm16_format
from easycat._provider_helpers import ProviderErrorEmitter
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.errors import EASYCAT_E304
from easycat.events import ErrorStage
from easycat.reconnecting_ws import ReconnectCallback, ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import _STT_RECEIVE_FINISH_POLICY, STTBase

logger = logging.getLogger(__name__)

_RECEIVE_TASK = "stt_receive_loop"


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
        dynamic_event_queue: bool = False,
    ) -> None:
        super().__init__(
            expected_sample_rate=expected_sample_rate,
            allow_end_during_audio_send=True,
        )
        self._provider_name = provider_name
        self._provider_error_name = provider_error_name
        self._close_timeout = close_timeout
        self._dynamic_event_queue = dynamic_event_queue
        self._ws: ReconnectingWebSocket | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._provider_event_bus: Any | None = None
        self._source_frame_carry = b""
        self._source_frame_carry_format: AudioFormat | None = None
        self._source_frame_generation = -1
        # Persistent providers set this while deliberately discarding a socket
        # whose drained frames must still reach the current turn's queue: the
        # receive loop then skips its terminal sentinel so events emitted after
        # the drain (e.g. a promoted interim transcript) are not stranded
        # behind it in a queue the consumer has already stopped reading.
        self._suppress_terminal_sentinel = False
        self._init_emit_tasks()

    def _validate_audio(self, chunk: AudioChunk) -> None:
        super()._validate_audio(chunk)
        if self._uses_streaming_audio_path():
            validate_pcm16_format("Streaming STT input", chunk.format)

    def _prepare_audio(self, chunk: AudioChunk) -> AudioChunk:
        """Frame-align and downmix before provider-specific resampling."""
        if not self._uses_streaming_audio_path():
            return chunk
        if (
            self._source_frame_generation != self._stream_generation
            or self._source_frame_carry_format != chunk.format
        ):
            self._source_frame_carry = b""
        self._source_frame_generation = self._stream_generation
        self._source_frame_carry_format = chunk.format

        source_data = self._source_frame_carry + chunk.data
        remainder = len(source_data) % chunk.format.frame_size
        if remainder:
            self._source_frame_carry = source_data[-remainder:]
            source_data = source_data[:-remainder]
        else:
            self._source_frame_carry = b""
        return to_mono_chunk(replace(chunk, data=source_data))

    def _uses_streaming_audio_path(self) -> bool:
        """Whether this stream sends raw PCM through the WebSocket path."""
        return True

    def _resolve_event_bus(self) -> Any | None:
        # Providers still source the bus from their own static config object
        # (e.g. Deepgram passes ``self._config.event_bus`` into
        # ``_connect_websocket``), same as the TTS providers. Only the
        # resolution timing differs: this base class doesn't know each
        # subclass's config type, so it caches the bus on the instance when
        # ``_connect_websocket`` runs (``self._provider_event_bus``) instead
        # of reading a config attribute directly here.
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
            on_disconnect=self._on_websocket_disconnect,
        )
        self._ws = ws
        self._provider_event_bus = event_bus
        await ws.connect()
        scope = self._ensure_runtime_scope()
        self._receive_task = scope.create_task(
            _RECEIVE_TASK,
            self._receive_loop(),
            task_name=f"{self._provider_error_name}:receive",
            policy=_STT_RECEIVE_FINISH_POLICY,
        )
        return ws

    async def _send_ws(self, message: str | bytes) -> None:
        if self._ws is not None:
            await self._ws.send(message)

    async def _on_websocket_disconnect(
        self,
        exc: websockets.exceptions.ConnectionClosed,
    ) -> None:
        """Surface a live provider drop before reconnect policy handles it."""
        self._emit_provider_error(
            EASYCAT_E304(
                provider=self._provider_error_name,
                detail=str(exc) or "connection closed",
            )
        )
        # Preserve lifecycle order: observers must see the drop before any
        # reconnect attempt/success/failure events.
        await self._drain_emit_tasks()

    async def _send_text_control(self, message: str, *, label: str) -> bool:
        """Send one control frame as a raw text message.

        Not every provider wraps its client commands in a JSON envelope:
        Cartesia's STT socket defines ``finalize`` and ``close`` as bare text
        messages.  Delivery is reported rather than raised — a control frame
        lost to an already-closing socket is not a turn failure.
        """
        if self._ws is None:
            return False
        try:
            await self._ws.send(message)
        except Exception:
            logger.debug("Error sending %s", label, exc_info=True)
            return False
        return True

    async def _send_json_control(self, payload: dict[str, Any], *, label: str) -> bool:
        return await self._send_text_control(json.dumps(payload), label=label)

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
        await self._drain_and_close(ws, receive_task, close_before_drain=close_before_drain)
        # Do not forget a socket whose close failed: failed-start cleanup keeps
        # a retry ledger, and it needs these exact references to finish the
        # ownership obligation on the next start_stream()/close().
        if self._ws is ws:
            self._ws = None
        if self._receive_task is receive_task:
            self._receive_task = None
        if receive_task is not None and receive_task.done() and self._runtime_scope is not None:
            self._runtime_scope.discard(receive_task)
        self._provider_event_bus = None

    async def _on_start_failed(self) -> None:
        """Close a socket published before its initial connect completed."""
        await self._close_active_websocket(close_before_drain=True)

    async def _on_end_cleanup(self) -> None:
        """Retry only socket cleanup after provider end/finalization failed."""
        await self._close_active_websocket(close_before_drain=True)

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
        # Capture the socket this loop consumes: ``self._ws`` can be rebound
        # (restart/reconnect) or nulled while a late-finishing loop unwinds,
        # and the abnormal-death check below must inspect THIS socket, not
        # whatever currently occupies the attribute.
        ws = self._ws
        queue = self._event_queue
        try:
            async for raw_message in ws.recv_iter():
                await self._handle_incoming_message(raw_message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("%s WebSocket closed", self._provider_log_label)
        except Exception as exc:
            logger.exception("Error in %s receive loop", self._provider_log_label)
            # recv_iter() itself can fail with exceptions other than
            # ConnectionClosed. Such a terminal failure cannot be recovered
            # per frame, but it still must not look like a clean EOF.
            self._emit_provider_error(exc, phase="receive_loop")
        finally:
            self._on_receive_loop_end()
            if ws.died_abnormally:
                from easycat.errors import EASYCAT_E305

                self._emit_provider_error(
                    EASYCAT_E305(
                        provider=self._provider_error_name,
                        attempts=getattr(ws, "reconnect_attempts_exhausted", None) or 0,
                        reason=getattr(ws, "reconnect_exhaustion_reason", None)
                        or "reconnect policy",
                    )
                )
            # Persistent STT providers keep this receive loop alive while
            # ``start_stream`` replaces the logical per-turn event queue. They
            # opt into terminating the current queue if the shared socket dies;
            # one-shot providers retain the socket-bound queue so a late old
            # receive loop cannot terminate a newly opened stream.
            terminal_queue = self._event_queue if self._dynamic_event_queue else queue
            if not self._suppress_terminal_sentinel:
                terminal_queue.put_nowait(None)

    async def _handle_incoming_message(self, raw_message: Any) -> None:
        """Parse and dispatch one frame without letting it kill reception."""
        if not isinstance(raw_message, str | bytes):
            logger.debug(
                "Ignoring unsupported %s WebSocket message type %s",
                self._provider_log_label,
                type(raw_message).__name__,
            )
            return
        frame_type = "binary" if isinstance(raw_message, bytes) else "json"
        try:
            if isinstance(raw_message, bytes):
                await self._handle_ws_bytes_message(raw_message)
                return
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                return
            if not isinstance(msg, dict):
                return
            self._handle_json_message(msg)
        except Exception as exc:
            # One malformed provider frame must not tear down the receive task
            # and silently truncate every later transcript.
            logger.exception(
                "Error handling %s %s message",
                frame_type,
                self._provider_log_label,
            )
            self._emit_provider_error(
                exc,
                phase="receive_frame",
                frame_type=frame_type,
            )

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
