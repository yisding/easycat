"""Deepgram TTS (Aura) provider implementation."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput, coerce_tts_input, strip_ssml_tags

logger = logging.getLogger(__name__)


@dataclass
class DeepgramTTSConfig:
    """Configuration for the Deepgram TTS provider."""

    api_key: str = ""
    model: str = "aura-asteria-en"
    encoding: str = "linear16"
    sample_rate: int = 24000
    base_url: str = "wss://api.deepgram.com/v1/speak"
    output_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    event_bus: object | None = None


class DeepgramTTS(TTSBase):
    """TTS provider using Deepgram's Aura WebSocket API.

    Opens a WebSocket connection to Deepgram, sends text, and receives
    audio chunks as binary messages. Uses ReconnectingWebSocket for
    connection lifecycle management.

    Requests linear16 (PCM16) encoding directly from Deepgram to avoid
    needing audio decoding dependencies.
    """

    def __init__(self, config: DeepgramTTSConfig) -> None:
        super().__init__(output_format=config.output_format)
        self._config = config
        self._ws: ReconnectingWebSocket | None = None
        # Build the source format based on what Deepgram returns
        self._source_format = AudioFormat(
            sample_rate=config.sample_rate,
            channels=1,
            sample_width=2,
        )

    def _build_url(self) -> str:
        """Build the Deepgram TTS WebSocket URL with query parameters."""
        return (
            f"{self._config.base_url}"
            f"?model={self._config.model}"
            f"&encoding={self._config.encoding}"
            f"&sample_rate={self._config.sample_rate}"
        )

    def _create_ws(self) -> ReconnectingWebSocket:
        """Create a new ReconnectingWebSocket with auth headers."""
        return ReconnectingWebSocket(
            url=self._build_url(),
            config=ReconnectConfig(
                extra_headers={"Authorization": f"Token {self._config.api_key}"},
            ),
            event_bus=self._config.event_bus,
            provider_name="deepgram_tts",
        )

    @property
    def supports_ssml(self) -> bool:
        return False

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text using Deepgram's WebSocket TTS API.

        Opens a WebSocket, sends the text, and yields audio chunks as
        they arrive. Sends a flush message after the text to signal
        end of input.
        """
        self._start_synthesis()
        self._ws = self._create_ws()
        payload = coerce_tts_input(payload)
        text = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)

        try:
            await self._ws.connect()

            # Send the text payload
            await self._ws.send(json.dumps({"type": "Speak", "text": text}))

            # Send flush to signal end of text input
            await self._ws.send(json.dumps({"type": "Flush"}))

            # Receive audio chunks
            async for message in self._ws.recv_iter():
                if self._cancelled:
                    break

                if isinstance(message, bytes) and message:
                    yield self._make_audio_event(message, self._source_format)
                elif isinstance(message, str):
                    # Handle control messages from Deepgram
                    try:
                        ctrl = json.loads(message)
                        if ctrl.get("type") == "Flushed":
                            break
                    except json.JSONDecodeError:
                        pass

        except Exception as exc:
            if not self._cancelled:
                logger.error("Deepgram TTS error: %s", exc)
                raise
        finally:
            await self._close_ws()
            self._end_synthesis()

    async def _close_ws(self) -> None:
        """Close the current WebSocket connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing Deepgram WebSocket", exc_info=True)
            finally:
                self._ws = None

    async def stop(self) -> None:
        """Gracefully stop synthesis."""
        await super().stop()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "Flush"}))
            except Exception:
                pass

    async def cancel(self) -> None:
        """Immediately cancel synthesis and close the WebSocket."""
        await super().cancel()
        await self._close_ws()
