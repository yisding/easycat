"""Deepgram streaming STT provider — real-time WebSocket transcription."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.websocket_base import WebSocketSTTBase


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


class DeepgramSTT(WebSocketSTTBase):
    """Real-time streaming STT using Deepgram WebSocket API.

    Opens a WebSocket on ``start_stream``, forwards audio chunks via
    ``send_audio``, and parses incoming transcript messages (partial + final)
    in a background receive loop.
    """

    def __init__(self, config: DeepgramSTTConfig) -> None:
        super().__init__(
            provider_name="deepgram_stt",
            provider_error_name="deepgram",
            expected_sample_rate=config.sample_rate,
            close_timeout=5.0,
        )
        self._config = config

    async def _on_start(self) -> None:
        url = self._build_url()
        headers = {"Authorization": f"Token {self._config.api_key}"}
        await self._connect_websocket(
            url=url,
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
        )

    async def _on_audio(self, chunk: AudioChunk) -> None:
        await self._send_ws(chunk.data)

    async def _on_end(self) -> None:
        if self._ws is not None:
            await self._send_json_control({"type": "CloseStream"}, label="CloseStream")

        await self._close_active_websocket(close_before_drain=False)

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
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
        word_timestamps = word_timestamps_from_words(best.get("words"))
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

    def _handle_message(self, msg: dict[str, Any]) -> None:
        self._handle_json_message(msg)

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
                word_timestamps=word_timestamps_from_words(msg.get("words")),
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

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "deepgram",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("websockets"),
        }


def _flux_base_url(base_url: str) -> str:
    if base_url.endswith("/v1/listen"):
        return f"{base_url[: -len('/v1/listen')]}/v2/listen"
    return base_url
