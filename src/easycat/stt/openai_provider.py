"""OpenAI STT provider — turn-based transcription via Audio API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType
from easycat.stt.base import STTBase, pcm_to_wav

logger = logging.getLogger(__name__)


@dataclass
class OpenAISTTConfig:
    """Configuration for the OpenAI STT provider."""

    api_key: str
    model: str = "gpt-4o-transcribe"
    language: str | None = None
    prompt: str | None = None
    base_url: str = "https://api.openai.com/v1"
    max_retries: int = 3
    timeout: float = 30.0
    # Optional HTTP client override for testing
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)


class OpenAISTT(STTBase):
    """Turn-based STT using OpenAI Audio API transcriptions endpoint.

    Buffers all audio received via ``send_audio``, then submits the complete
    buffer as a WAV file to the transcription API when ``end_stream`` is called.
    """

    def __init__(self, config: OpenAISTTConfig) -> None:
        super().__init__()
        self._config = config
        self._buffer = bytearray()
        self._audio_format: AudioFormat | None = None

    async def _on_start(self) -> None:
        self._buffer.clear()
        self._audio_format = None

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._audio_format is None:
            self._audio_format = chunk.format
        self._buffer.extend(chunk.data)

    async def _on_end(self) -> None:
        if not self._buffer or self._audio_format is None:
            return

        wav_data = pcm_to_wav(bytes(self._buffer), self._audio_format)
        text = await self._transcribe(wav_data)
        if text:
            self._emit_event(STTEvent(type=STTEventType.FINAL, text=text))

    async def _transcribe(self, wav_data: bytes) -> str:
        url = f"{self._config.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}

        data: dict[str, str] = {"model": self._config.model}
        if self._config.language:
            data["language"] = self._config.language
        if self._config.prompt:
            data["prompt"] = self._config.prompt

        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries):
            try:
                client = self._config.http_client or httpx.AsyncClient(
                    timeout=self._config.timeout
                )
                owns_client = self._config.http_client is None
                try:
                    response = await client.post(
                        url,
                        headers=headers,
                        files={"file": ("audio.wav", wav_data, "audio/wav")},
                        data=data,
                    )
                    response.raise_for_status()
                    return response.json()["text"]
                finally:
                    if owns_client:
                        await client.aclose()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code == 429 and attempt < self._config.max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < self._config.max_retries - 1:
                    logger.warning(
                        "OpenAI STT request failed (attempt %d/%d): %s",
                        attempt + 1,
                        self._config.max_retries,
                        exc,
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                raise

        raise RuntimeError("All retries exhausted") from last_exc
