"""Deepgram streaming STT provider — real-time WebSocket transcription."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import websockets

from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType, WordTimestamp
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase

logger = logging.getLogger(__name__)


@dataclass
class DeepgramSTTConfig:
    """Configuration for the Deepgram STT provider."""

    api_key: str
    model: str = "nova-2"
    language: str = "en"
    encoding: str = "linear16"
    sample_rate: int = 16000
    channels: int = 1
    punctuate: bool = True
    interim_results: bool = True
    smart_format: bool = False
    base_url: str = "wss://api.deepgram.com/v1/listen"
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for reconnect observability
    event_bus: Any = field(default=None, repr=False)

    @property
    def is_flux(self) -> bool:
        """Whether this config uses a Flux model with provider-side endpointing."""
        return self.model.lower().startswith("flux")


class DeepgramSTT(STTBase):
    """Real-time streaming STT using Deepgram WebSocket API.

    Opens a WebSocket on ``start_stream``, forwards audio chunks via
    ``send_audio``, and parses incoming transcript messages (partial + final)
    in a background receive loop.
    """

    def __init__(self, config: DeepgramSTTConfig) -> None:
        super().__init__(expected_sample_rate=config.sample_rate)
        self._config = config
        self._ws: ReconnectingWebSocket | None = None
        self._receive_task: asyncio.Task[None] | None = None

    async def _on_start(self) -> None:
        url = self._build_url()
        headers = {"Authorization": f"Token {self._config.api_key}"}

        self._ws = ReconnectingWebSocket(
            url=url,
            config=ReconnectConfig(extra_headers=headers),
            event_bus=self._config.event_bus,
            provider_name="deepgram_stt",
            connect_fn=self._config.ws_connect,
        )
        await self._ws.connect()
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            await self._ws.send(chunk.data)

    async def _on_end(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                logger.debug("Error sending CloseStream", exc_info=True)

            if self._receive_task is not None:
                try:
                    await asyncio.wait_for(self._receive_task, timeout=5.0)
                except TimeoutError:
                    self._receive_task.cancel()
                    logger.warning("Deepgram receive loop timed out on close")

            await self._ws.close()

        self._ws = None
        self._receive_task = None

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw_message in self._ws.recv_iter():
                if isinstance(raw_message, bytes):
                    continue
                msg = json.loads(raw_message)
                self._handle_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Deepgram WebSocket closed")
        except Exception:
            logger.exception("Error in Deepgram receive loop")
        finally:
            self._event_queue.put_nowait(None)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        if self._config.is_flux:
            self._handle_flux_message(msg_type, msg)
            return
        if msg_type != "Results":
            return

        channel = msg.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        best = alternatives[0]
        transcript = best.get("transcript", "")
        if not transcript:
            return

        confidence = best.get("confidence")
        is_final = msg.get("is_final", False)
        word_timestamps = _word_timestamps_from_words(best.get("words"))
        event_type = STTEventType.FINAL if is_final else STTEventType.PARTIAL
        self._emit_event(
            STTEvent(
                type=event_type,
                text=transcript,
                confidence=confidence,
                language=self._config.language,
                word_timestamps=word_timestamps,
            )
        )

    def _handle_flux_message(self, msg_type: str, msg: dict[str, Any]) -> None:
        if msg_type != "TurnInfo":
            return

        transcript = msg.get("transcript", "")
        if not transcript:
            return

        turn_event = msg.get("event", "")
        if turn_event == "EndOfTurn":
            event_type = STTEventType.FINAL
            confidence = msg.get("end_of_turn_confidence")
        else:
            event_type = STTEventType.PARTIAL
            confidence = None

        self._emit_event(
            STTEvent(
                type=event_type,
                text=transcript,
                confidence=confidence,
                language=self._config.language,
                word_timestamps=_word_timestamps_from_words(msg.get("words")),
            )
        )

    def _build_url(self) -> str:
        params = {
            "model": self._config.model,
            "encoding": self._config.encoding,
            "sample_rate": str(self._config.sample_rate),
        }
        if self._config.is_flux:
            base_url = _flux_base_url(self._config.base_url)
        else:
            base_url = self._config.base_url
            params.update(
                {
                    "language": self._config.language,
                    "channels": str(self._config.channels),
                    "punctuate": str(self._config.punctuate).lower(),
                    "interim_results": str(self._config.interim_results).lower(),
                    "smart_format": str(self._config.smart_format).lower(),
                }
            )
        return f"{base_url}?{urlencode(params)}"


def _flux_base_url(base_url: str) -> str:
    if base_url.endswith("/v1/listen"):
        return f"{base_url[: -len('/v1/listen')]}/v2/listen"
    return base_url


def _word_timestamps_from_words(words: Any) -> list[WordTimestamp] | None:
    if not isinstance(words, list):
        return None

    timestamps: list[WordTimestamp] = []
    for item in words:
        if not isinstance(item, dict):
            continue
        word = item.get("word")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(word, str) or start is None or end is None:
            continue
        timestamps.append(WordTimestamp(word=word, start=float(start), end=float(end)))

    return timestamps or None
