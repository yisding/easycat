"""Cartesia streaming STT (Ink-Whisper) WebSocket provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.websocket_base import WebSocketSTTBase


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


class CartesiaSTT(WebSocketSTTBase):
    """Real-time streaming STT using Cartesia's Ink-Whisper WebSocket API.

    Opens a WebSocket on :meth:`start_stream`, forwards audio as binary
    frames, and parses ``transcript`` messages (partial + final) in a
    background receive loop. A ``finalize`` control message flushes the
    buffered audio mid-stream (used by
    :meth:`~easycat.stt.base.STTBase.commit_segment`); a ``done``
    control message closes the session cleanly.
    """

    def __init__(self, config: CartesiaSTTConfig) -> None:
        super().__init__(
            provider_name="cartesia_stt",
            provider_error_name="cartesia",
            expected_sample_rate=config.sample_rate,
            close_timeout=5.0,
        )
        self._config = config

    async def _on_start(self) -> None:
        url = self._build_url()
        headers = {
            "X-API-Key": self._config.api_key,
            "Cartesia-Version": self._config.cartesia_version,
        }
        await self._connect_websocket(
            url=url,
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
        )

    async def _on_audio(self, chunk: AudioChunk) -> None:
        await self._send_ws(chunk.data)

    async def _on_commit_segment(self) -> bool:
        return await self._send_json_control({"type": "finalize"}, label="Cartesia finalize")

    async def _on_end(self) -> None:
        if self._ws is not None:
            await self._send_json_control({"type": "done"}, label="Cartesia done")

        await self._close_active_websocket()

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "transcript":
            self._handle_transcript(msg)
        elif msg_type == "error":
            self._emit_provider_error_from_message(msg, default_message="Cartesia STT error")
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

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "cartesia",
            "model": self._config.model,
            "api_version": self._config.cartesia_version,
            "sdk_version": get_package_version("websockets"),
        }
