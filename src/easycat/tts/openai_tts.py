"""OpenAI TTS provider implementation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput, coerce_tts_input, strip_ssml_tags

logger = logging.getLogger(__name__)

# OpenAI's pcm response format returns raw PCM16 at 24kHz mono
_OPENAI_PCM_FORMAT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)


@dataclass
class OpenAITTSConfig:
    """Configuration for the OpenAI TTS provider."""

    api_key: str = ""
    model: str = "tts-1"
    voice: str = "alloy"
    speed: float = 1.0
    base_url: str = "https://api.openai.com/v1"
    output_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)


class OpenAITTS(TTSBase):
    """TTS provider using OpenAI's Audio API.

    Uses the `audio/speech` endpoint with `response_format=pcm` to get
    raw PCM16 audio at 24kHz, avoiding any need for MP3/Opus decoding.
    Streaming is done via httpx's async streaming response.
    """

    def __init__(self, config: OpenAITTSConfig) -> None:
        super().__init__(output_format=config.output_format)
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._response: httpx.Response | None = None

    @property
    def supports_ssml(self) -> bool:
        return False

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text using OpenAI Audio API with streaming response.

        Requests PCM16 format directly to avoid decoding overhead.
        Yields TTSEvent objects with AUDIO type containing PCM16 chunks.
        """
        self._start_synthesis()

        payload = coerce_tts_input(payload)
        text = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)

        try:
            request_body = {
                "model": self._config.model,
                "input": text,
                "voice": self._config.voice,
                "speed": self._config.speed,
                "response_format": "pcm",
            }

            async with self._client.stream(
                "POST",
                "/audio/speech",
                json=request_body,
            ) as response:
                self._response = response
                response.raise_for_status()

                async for chunk in response.aiter_bytes(chunk_size=4800):
                    if self._cancelled:
                        break
                    if chunk:
                        yield self._make_audio_event(chunk, _OPENAI_PCM_FORMAT)

        except httpx.HTTPStatusError as exc:
            logger.error(
                "OpenAI TTS API error: %s %s", exc.response.status_code, exc.response.text
            )
            raise
        except httpx.HTTPError as exc:
            if not self._cancelled:
                logger.error("OpenAI TTS HTTP error: %s", exc)
                raise
        finally:
            self._response = None
            self._end_synthesis()

    async def stop(self) -> None:
        """Gracefully stop synthesis."""
        await super().stop()
        if self._response is not None:
            await self._response.aclose()

    async def cancel(self) -> None:
        """Immediately cancel synthesis and close the HTTP stream."""
        await super().cancel()
        resp = self._response
        self._response = None
        if resp is not None:
            await resp.aclose()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
