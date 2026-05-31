"""OpenAI STT provider — streaming transcription via Audio API."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx

from easycat._provider_helpers import get_package_version
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType
from easycat.stt.base import STTBase, pcm_to_wav

logger = logging.getLogger(__name__)


@dataclass
class OpenAISTTConfig:
    """Configuration for the OpenAI STT provider.

    .. note::

       ``api_key`` defaults to ``""`` to support the inject-the-key-later
       workflow (e.g. constructing the config first and assigning the key
       before use).  A missing key is therefore *not* validated at
       construction time — it surfaces on the first live transcription
       request rather than eagerly.  The :func:`easycat.stt.factory` path
       still fail-fasts on an empty key.

    ``max_retries`` is the *total* number of transcription attempts; the
    request path always runs at least once, so ``max_retries=0`` (or any
    value below 1) is clamped to a single attempt rather than sending zero
    requests.
    """

    api_key: str = ""
    model: str = "gpt-4o-transcribe"
    language: str | None = None
    prompt: str | None = None
    base_url: str = "https://api.openai.com/v1"
    max_retries: int = 3
    timeout: float = 30.0
    # Optional HTTP client override for testing
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(
                "OpenAISTTConfig.max_retries must be >= 0 "
                f"(got {self.max_retries}); it is the total attempt count, "
                "where 0 is clamped to a single attempt"
            )


class OpenAISTT(STTBase):
    """Turn-based STT using OpenAI Audio API streaming transcriptions.

    Buffers all audio received via ``send_audio``, then submits the complete
    buffer as a WAV file to the transcription API when ``end_stream`` is called.
    The transcription response is streamed and emitted as partial events, with
    a final transcript emitted at the end of the stream.

    The buffered PCM is wrapped into one WAV header built from the first
    chunk's :class:`AudioFormat`, so every chunk in a single utterance must
    share that format. ``_on_audio`` raises ``ValueError`` on a mid-stream
    format change rather than silently mislabeling the WAV. Bundled transports
    resample inbound audio to a fixed pipeline rate before STT, so this only
    guards against custom transports that emit varying formats.
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
        self._audio_format = self._latch_uniform_format(
            self._audio_format, chunk, provider_label="OpenAI STT"
        )
        self._buffer.extend(chunk.data)

    async def _on_end(self) -> None:
        if not self._buffer or self._audio_format is None:
            return

        wav_data = pcm_to_wav(bytes(self._buffer), self._audio_format)
        await self._transcribe_streaming(wav_data)

    async def _transcribe_streaming(self, wav_data: bytes) -> str:
        url = f"{self._config.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}

        data: dict[str, str] = {"model": self._config.model}
        if self._config.language:
            data["language"] = self._config.language
        if self._config.prompt:
            data["prompt"] = self._config.prompt
        data["stream"] = "true"

        # ``max_retries`` is the total attempt count; clamp to at least one
        # so a misconfigured ``max_retries=0`` still sends a single request
        # rather than raising a causeless "no attempts" error.
        total_attempts = max(1, self._config.max_retries)
        last_exc: Exception | None = None
        for attempt in range(total_attempts):
            full_text = ""
            emitted_final = False
            # Buffer events for this attempt so a mid-stream retry does not
            # replay duplicate PARTIAL/FINAL events onto the queue. Events are
            # only flushed once the attempt completes successfully.
            pending_events: list[STTEvent] = []
            try:
                client = self._config.http_client or httpx.AsyncClient(
                    timeout=self._config.timeout
                )
                owns_client = self._config.http_client is None
                try:
                    async with client.stream(
                        "POST",
                        url,
                        headers=headers,
                        files={"file": ("audio.wav", wav_data, "audio/wav")},
                        data=data,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            payload = line.strip()
                            if payload.startswith("data:"):
                                payload = payload[5:].strip()
                            if payload == "[DONE]":
                                break
                            text, is_delta, is_final = self._extract_stream_text(payload)
                            if not text:
                                continue
                            if is_delta:
                                full_text += text
                            else:
                                full_text = text
                            pending_events.append(
                                STTEvent(type=STTEventType.PARTIAL, text=full_text)
                            )
                            if is_final:
                                pending_events.append(
                                    STTEvent(type=STTEventType.FINAL, text=full_text)
                                )
                                emitted_final = True
                                break
                        if full_text and not emitted_final:
                            pending_events.append(
                                STTEvent(type=STTEventType.FINAL, text=full_text)
                            )
                            emitted_final = True
                        for event in pending_events:
                            self._emit_event(event)
                        return full_text
                finally:
                    if owns_client:
                        await client.aclose()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code == 429 and attempt < total_attempts - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < total_attempts - 1:
                    logger.warning(
                        "OpenAI STT request failed (attempt %d/%d): %s",
                        attempt + 1,
                        total_attempts,
                        exc,
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                raise

        # The loop always runs at least once (total_attempts >= 1), so reaching
        # here means every attempt failed without re-raising; last_exc is set.
        raise RuntimeError(
            f"OpenAI STT: all {total_attempts} transcription attempt(s) failed"
        ) from last_exc

    @staticmethod
    def _extract_stream_text(payload: str) -> tuple[str | None, bool, bool]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None, False, False

        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]

        if isinstance(data, dict) and isinstance(data.get("choices"), list):
            choice = data["choices"][0] if data["choices"] else {}
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if isinstance(delta.get("text"), str):
                        return delta["text"], True, False
                    if isinstance(delta.get("content"), str):
                        return delta["content"], True, False
                if isinstance(choice.get("text"), str):
                    return choice["text"], False, choice.get("finish_reason") is not None

        if isinstance(data, dict):
            if isinstance(data.get("delta"), str):
                return data["delta"], True, False
            if isinstance(data.get("text"), str):
                is_final = bool(data.get("is_final") or data.get("final"))
                return data["text"], False, is_final
            if isinstance(data.get("transcript"), str):
                is_final = bool(data.get("is_final") or data.get("final"))
                return data["transcript"], False, is_final

        return None, False, False

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "openai",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("httpx"),
        }
