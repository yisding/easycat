"""Cartesia streaming STT (Ink-Whisper) WebSocket provider."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import websockets

from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase

logger = logging.getLogger(__name__)


@dataclass
class CartesiaSTTConfig:
    """Configuration for the Cartesia STT (Ink-Whisper) provider."""

    api_key: str = ""
    model: str = "ink-whisper"
    language: str = "en"
    encoding: str = "pcm_s16le"
    sample_rate: int = 16000
    # VAD threshold (0.0–1.0). Kept at 0.0 so EasyCat's own turn
    # manager owns endpointing decisions; Cartesia won't close the turn
    # on volume alone at this setting.
    min_volume: float = 0.0
    # How long of a silence gap Cartesia waits before emitting a final
    # transcript. 5s is intentionally generous so the turn manager's
    # own silence detection fires first in most cases.
    max_silence_duration_secs: float = 5.0
    cartesia_version: str = "2026-03-01"
    base_url: str = "wss://api.cartesia.ai/stt/websocket"
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for provider-error observability
    event_bus: Any = field(default=None, repr=False)


class CartesiaSTT(STTBase):
    """Real-time streaming STT using Cartesia's Ink-Whisper WebSocket API.

    Opens a WebSocket on :meth:`start_stream`, forwards audio as binary
    frames, and parses ``transcript`` messages (partial + final) in a
    background receive loop. A ``finalize`` control message flushes the
    buffered audio mid-stream (used by
    :meth:`~easycat.stt.base.STTBase.commit_segment`); a ``done``
    control message closes the session cleanly.
    """

    def __init__(self, config: CartesiaSTTConfig) -> None:
        super().__init__(expected_sample_rate=config.sample_rate)
        self._config = config
        self._ws: ReconnectingWebSocket | None = None
        self._receive_task: asyncio.Task[None] | None = None

    async def _on_start(self) -> None:
        url = self._build_url()
        headers = {
            "X-API-Key": self._config.api_key,
            "Cartesia-Version": self._config.cartesia_version,
        }
        self._ws = ReconnectingWebSocket(
            url=url,
            config=ReconnectConfig(extra_headers=headers),
            event_bus=self._config.event_bus,
            provider_name="cartesia_stt",
            connect_fn=self._config.ws_connect,
        )
        await self._ws.connect()
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            await self._ws.send(chunk.data)

    async def _on_commit_segment(self) -> bool:
        if self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps({"type": "finalize"}))
        except Exception:
            logger.debug("Error sending Cartesia finalize", exc_info=True)
            return False
        return True

    async def _on_end(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "done"}))
            except Exception:
                logger.debug("Error sending Cartesia done", exc_info=True)

            if self._receive_task is not None:
                try:
                    await asyncio.wait_for(self._receive_task, timeout=5.0)
                except TimeoutError:
                    self._receive_task.cancel()
                    logger.warning("Cartesia receive loop timed out on close")

            await self._ws.close()

        self._ws = None
        self._receive_task = None

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw_message in self._ws.recv_iter():
                if isinstance(raw_message, bytes):
                    continue
                try:
                    msg = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                self._handle_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cartesia WebSocket closed")
        except Exception:
            logger.exception("Error in Cartesia receive loop")
        finally:
            self._event_queue.put_nowait(None)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "transcript":
            self._handle_transcript(msg)
        elif msg_type == "error":
            self._emit_provider_error_from_msg(msg)
        # ``flush_done`` and ``done`` are acks — nothing to do.

    def _handle_transcript(self, msg: dict[str, Any]) -> None:
        text = msg.get("text", "")
        if not text:
            return

        is_final = bool(msg.get("is_final"))
        event_type = STTEventType.FINAL if is_final else STTEventType.PARTIAL
        word_timestamps = word_timestamps_from_words(msg.get("words"))
        self._emit_event(
            STTEvent(
                type=event_type,
                text=text,
                confidence=msg.get("confidence"),
                language=msg.get("language") or self._config.language,
                word_timestamps=word_timestamps,
            )
        )

    def _build_url(self) -> str:
        params = {
            "model": self._config.model,
            "language": self._config.language,
            "encoding": self._config.encoding,
            "sample_rate": str(self._config.sample_rate),
            "min_volume": str(self._config.min_volume),
            "max_silence_duration_secs": str(self._config.max_silence_duration_secs),
        }
        return f"{self._config.base_url}?{urlencode(params)}"

    def _emit_provider_error_from_msg(self, msg: dict[str, Any]) -> None:
        message = msg.get("message") or msg.get("title") or "Cartesia STT error"
        exc = RuntimeError(f"Cartesia STT error: {message}")
        self._emit_provider_error(
            exc,
            code=msg.get("code"),
            status_code=msg.get("status_code"),
        )

    def _emit_provider_error(self, exc: BaseException, **context: Any) -> None:
        """Post a journal-visible ``Error`` event, with provider context."""
        bus = self._config.event_bus
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
            asyncio.create_task(
                bus.emit(Error(exception=exc, stage=ErrorStage.STT, provider="cartesia"))
            )
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error — no running loop", exc_info=True)

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "cartesia",
            "model": self._config.model,
            "api_version": self._config.cartesia_version,
            "sdk_version": get_package_version("websockets"),
        }
