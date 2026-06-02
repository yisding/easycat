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
from easycat.stt.base import (
    DEFAULT_MAX_AUDIO_BUFFER_BYTES,
    DEFAULT_MAX_AUDIO_CHUNK_BYTES,
    DEFAULT_MAX_AUDIO_DURATION_MS,
    STTBase,
    pcm_to_wav,
)

logger = logging.getLogger(__name__)


class OpenAISTTStreamLimitError(RuntimeError):
    """Raised when an OpenAI STT streaming response exceeds configured limits."""


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
    max_audio_chunk_bytes: int | None = DEFAULT_MAX_AUDIO_CHUNK_BYTES
    max_audio_buffer_bytes: int | None = DEFAULT_MAX_AUDIO_BUFFER_BYTES
    max_audio_duration_ms: float | None = DEFAULT_MAX_AUDIO_DURATION_MS
    stream_timeout: float | None = None
    max_stream_events: int = 1_000
    max_stream_line_bytes: int = 65_536
    max_stream_total_bytes: int = 8_388_608
    max_transcript_chars: int = 131_072
    max_partial_events: int = 1_000
    # Optional HTTP client override for testing
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(
                "OpenAISTTConfig.max_retries must be >= 0 "
                f"(got {self.max_retries}); it is the total attempt count, "
                "where 0 is clamped to a single attempt"
            )
        STTBase._validate_positive_limit(
            "OpenAISTTConfig.max_audio_chunk_bytes", self.max_audio_chunk_bytes
        )
        STTBase._validate_positive_limit(
            "OpenAISTTConfig.max_audio_buffer_bytes", self.max_audio_buffer_bytes
        )
        STTBase._validate_positive_limit(
            "OpenAISTTConfig.max_audio_duration_ms", self.max_audio_duration_ms
        )
        if self.timeout <= 0:
            raise ValueError("OpenAISTTConfig.timeout must be positive")
        if self.stream_timeout is not None and self.stream_timeout <= 0:
            raise ValueError("OpenAISTTConfig.stream_timeout must be positive when set")
        if self.max_stream_events <= 0:
            raise ValueError("OpenAISTTConfig.max_stream_events must be positive")
        if self.max_stream_line_bytes <= 0:
            raise ValueError("OpenAISTTConfig.max_stream_line_bytes must be positive")
        if self.max_stream_total_bytes <= 0:
            raise ValueError("OpenAISTTConfig.max_stream_total_bytes must be positive")
        if self.max_transcript_chars <= 0:
            raise ValueError("OpenAISTTConfig.max_transcript_chars must be positive")
        if self.max_partial_events <= 0:
            raise ValueError("OpenAISTTConfig.max_partial_events must be positive")


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
        await self._buffer_batch_audio_or_finalize(
            self._buffer,
            chunk,
            max_chunk_bytes=self._config.max_audio_chunk_bytes,
            max_buffer_bytes=self._config.max_audio_buffer_bytes,
            max_duration_ms=self._config.max_audio_duration_ms,
            provider_label="OpenAI STT",
            finalize=self._flush_buffer,
        )

    async def _flush_buffer(self) -> None:
        """Transcribe and emit whatever is buffered, then reset for a fresh stream.

        Used both when the stream ends normally and when a cumulative buffer
        cap forces an early finalize mid-stream (long-talking caller). The
        latched format is preserved so the next utterance keeps the same
        first-seen format contract.
        """
        if not self._buffer or self._audio_format is None:
            return

        wav_data = pcm_to_wav(bytes(self._buffer), self._audio_format)
        # Clear in place (not a rebind) so the buffer reference held by the
        # in-progress ``_buffer_batch_audio_or_finalize`` call stays the same
        # object, letting the chunk that tripped the cap restart a fresh stream.
        self._buffer.clear()
        await self._transcribe_streaming(wav_data)

    async def _on_end(self) -> None:
        await self._flush_buffer()

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
                        stream_timeout = self._config.stream_timeout or self._config.timeout
                        # Track the byte caps incrementally as raw network
                        # chunks arrive so an unbounded no-newline body is
                        # aborted *before* httpx fully materializes it, rather
                        # than after a whole decoded line is buffered by
                        # ``aiter_lines()``.
                        buffer = bytearray()
                        total_bytes = 0
                        stream_events = 0
                        partial_events = 0
                        done = False

                        def _line_too_large() -> OpenAISTTStreamLimitError:
                            return OpenAISTTStreamLimitError(
                                "OpenAI STT streaming response line exceeded "
                                f"{self._config.max_stream_line_bytes} bytes"
                            )

                        def _process_line(raw_line: bytes) -> bool:
                            """Apply per-event caps; return True when the stream is done."""
                            nonlocal full_text, emitted_final, stream_events, partial_events
                            line = raw_line.decode("utf-8", "replace").strip()
                            if not line:
                                return False
                            stream_events += 1
                            if stream_events > self._config.max_stream_events:
                                raise OpenAISTTStreamLimitError(
                                    "OpenAI STT streaming response exceeded "
                                    f"{self._config.max_stream_events} events"
                                )
                            payload = line
                            if payload.startswith("data:"):
                                payload = payload[5:].strip()
                            if payload == "[DONE]":
                                return True
                            text, is_delta, is_final = self._extract_stream_text(payload)
                            if not text:
                                return False
                            next_text = full_text + text if is_delta else text
                            if len(next_text) > self._config.max_transcript_chars:
                                raise OpenAISTTStreamLimitError(
                                    "OpenAI STT transcript exceeded "
                                    f"{self._config.max_transcript_chars} characters"
                                )
                            full_text = next_text
                            partial_events += 1
                            if partial_events > self._config.max_partial_events:
                                raise OpenAISTTStreamLimitError(
                                    "OpenAI STT streaming response exceeded "
                                    f"{self._config.max_partial_events} partial events"
                                )
                            pending_events.append(
                                STTEvent(type=STTEventType.PARTIAL, text=full_text)
                            )
                            if is_final:
                                pending_events.append(
                                    STTEvent(type=STTEventType.FINAL, text=full_text)
                                )
                                emitted_final = True
                                return True
                            return False

                        try:
                            async with asyncio.timeout(stream_timeout):
                                async for chunk in response.aiter_bytes():
                                    if not chunk:
                                        continue
                                    total_bytes += len(chunk)
                                    if total_bytes > self._config.max_stream_total_bytes:
                                        raise OpenAISTTStreamLimitError(
                                            "OpenAI STT streaming response exceeded "
                                            f"{self._config.max_stream_total_bytes} total bytes"
                                        )
                                    buffer.extend(chunk)
                                    while (newline := buffer.find(b"\n")) != -1:
                                        raw_line = bytes(buffer[:newline])
                                        del buffer[: newline + 1]
                                        if len(raw_line) > self._config.max_stream_line_bytes:
                                            raise _line_too_large()
                                        if _process_line(raw_line):
                                            done = True
                                            break
                                    if done:
                                        break
                                    # A pending fragment without a newline still
                                    # counts against the per-line cap so a single
                                    # gigantic no-newline line is rejected before
                                    # it grows without bound.
                                    if len(buffer) > self._config.max_stream_line_bytes:
                                        raise _line_too_large()
                                if not done and buffer:
                                    # Flush a trailing newline-less final line.
                                    if len(buffer) > self._config.max_stream_line_bytes:
                                        raise _line_too_large()
                                    _process_line(bytes(buffer))
                        except TimeoutError as exc:
                            raise OpenAISTTStreamLimitError(
                                f"OpenAI STT streaming response exceeded {stream_timeout:.1f}s"
                            ) from exc
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
