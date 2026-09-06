"""OpenAI STT provider — streaming transcription via Audio API."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx

from easycat._provider_helpers import ProviderErrorEmitter, get_package_version
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import ErrorStage, STTEvent, STTEventType
from easycat.stt.base import (
    DEFAULT_MAX_AUDIO_BUFFER_BYTES,
    DEFAULT_MAX_AUDIO_CHUNK_BYTES,
    DEFAULT_MAX_AUDIO_DURATION_MS,
    STTBase,
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

    api_key: str = field(default="", repr=False)
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
    # Optional EventBus for provider-error observability.
    event_bus: object | None = field(default=None, repr=False)

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


class OpenAISTT(ProviderErrorEmitter, STTBase):
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

    _error_stage = ErrorStage.STT
    _provider_error_name = "openai"

    def __init__(self, config: OpenAISTTConfig) -> None:
        super().__init__()
        self._config = config
        self._buffer = bytearray()
        self._audio_format: AudioFormat | None = None
        self._init_emit_tasks()

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
        wav_data = self._drain_buffer_to_wav()
        if wav_data is None:
            return
        await self._transcribe_streaming(wav_data)

    async def _on_end(self) -> None:
        await self._flush_buffer()

    async def _transcribe_streaming(self, wav_data: bytes) -> str:
        """Submit *wav_data* with retries; returns the final transcript text.

        ``max_retries`` is the total attempt count; the shared helper clamps
        it to at least one so a misconfigured ``max_retries=0`` still sends a
        single request rather than raising a causeless "no attempts" error.
        Each attempt buffers its events internally and only emits on success,
        preserving the emit-on-success-only semantics across retries.
        """
        # Client construction stays *inside* the reporting ``try``: a failed
        # ``AsyncClient(...)`` (bad transport/proxy configuration) is a provider
        # failure and must still reach ``_emit_provider_error``.
        owned_client: httpx.AsyncClient | None = None
        try:
            if self._config.http_client is not None:
                client = self._config.http_client
            else:
                # One client for all attempts: a per-attempt client would pay a
                # fresh DNS+TCP+TLS handshake on every retry, exactly when the
                # provider is already slow or rate-limited (matches the
                # ElevenLabs provider).
                client = owned_client = httpx.AsyncClient(timeout=self._config.timeout)
            return await self._run_with_bounded_retry(
                lambda: self._attempt_streaming_transcription(wav_data, client),
                max_retries=self._config.max_retries,
                provider_label="OpenAI STT",
            )
        except Exception as exc:
            context: dict[str, object] = {}
            if isinstance(exc, httpx.HTTPStatusError):
                context["http_status"] = exc.response.status_code
            self._emit_provider_error(exc, **context)
            raise
        finally:
            if owned_client is not None:
                await owned_client.aclose()

    def _request_form_data(self) -> dict[str, str]:
        """Multipart form fields for the streaming transcription request."""
        data: dict[str, str] = {"model": self._config.model}
        if self._config.language:
            data["language"] = self._config.language
        if self._config.prompt:
            data["prompt"] = self._config.prompt
        data["stream"] = "true"
        return data

    async def _attempt_streaming_transcription(
        self,
        wav_data: bytes,
        client: httpx.AsyncClient,
    ) -> str:
        """Run one streaming transcription attempt and emit its events.

        Events are buffered in the parser for the whole attempt so a
        mid-stream retry does not replay duplicate PARTIAL/FINAL events
        onto the queue — they are only flushed once the attempt completes
        successfully.  ``client`` is owned by the caller and shared across
        retries so a retry reuses the established connection.
        """
        url = f"{self._config.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        async with client.stream(
            "POST",
            url,
            headers=headers,
            files={"file": ("audio.wav", wav_data, "audio/wav")},
            data=self._request_form_data(),
        ) as response:
            response.raise_for_status()
            parser = _TranscriptStreamParser(self._config)
            stream_timeout = self._config.stream_timeout or self._config.timeout
            try:
                async with asyncio.timeout(stream_timeout):
                    async for chunk in response.aiter_bytes():
                        if parser.feed(chunk):
                            break
                    parser.finish()
            except TimeoutError as exc:
                raise OpenAISTTStreamLimitError(
                    f"OpenAI STT streaming response exceeded {stream_timeout:.1f}s"
                ) from exc
            for event in parser.pending_events:
                self._emit_event(event)
            return parser.full_text

    @staticmethod
    def _extract_stream_text(payload: str) -> tuple[str | None, bool, bool]:
        """Parse one stream payload into ``(text, is_delta, is_final)``."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None, False, False

        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            return None, False, False

        choice_result = OpenAISTT._extract_choice_text(data)
        if choice_result is not None:
            return choice_result
        return OpenAISTT._extract_flat_text(data)

    @staticmethod
    def _extract_choice_text(data: dict) -> tuple[str | None, bool, bool] | None:
        """Handle the chat-completions-shaped ``choices[0]`` payload variant."""
        if not isinstance(data.get("choices"), list):
            return None
        choice = data["choices"][0] if data["choices"] else {}
        if not isinstance(choice, dict):
            return None
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if isinstance(delta.get("text"), str):
                return delta["text"], True, False
            if isinstance(delta.get("content"), str):
                return delta["content"], True, False
        if isinstance(choice.get("text"), str):
            return choice["text"], False, choice.get("finish_reason") is not None
        return None

    @staticmethod
    def _extract_flat_text(data: dict) -> tuple[str | None, bool, bool]:
        """Handle the flat ``delta`` / ``text`` / ``transcript`` payload variants."""
        if isinstance(data.get("delta"), str):
            return data["delta"], True, False
        for key in ("text", "transcript"):
            if isinstance(data.get(key), str):
                is_final = bool(data.get("is_final") or data.get("final"))
                return data[key], False, is_final
        return None, False, False

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "openai",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("httpx"),
        }


class _TranscriptStreamParser:
    """Incremental SSE-line parser for one streaming transcription attempt.

    Tracks the byte caps as raw network chunks arrive so an unbounded
    no-newline body is aborted *before* httpx fully materializes it,
    rather than after a whole decoded line is buffered by
    ``aiter_lines()``.  Emitted events are buffered on ``pending_events``
    so the caller can flush them only after the attempt succeeds.
    """

    def __init__(self, config: OpenAISTTConfig) -> None:
        self._config = config
        self._buffer = bytearray()
        self._total_bytes = 0
        self._stream_events = 0
        self._partial_events = 0
        self._done = False
        self._emitted_final = False
        self.full_text = ""
        self.pending_events: list[STTEvent] = []

    def feed(self, chunk: bytes) -> bool:
        """Consume one raw network chunk; return ``True`` when the stream is done."""
        if not chunk:
            return False
        self._total_bytes += len(chunk)
        if self._total_bytes > self._config.max_stream_total_bytes:
            raise OpenAISTTStreamLimitError(
                "OpenAI STT streaming response exceeded "
                f"{self._config.max_stream_total_bytes} total bytes"
            )
        self._buffer.extend(chunk)
        while (newline := self._buffer.find(b"\n")) != -1:
            raw_line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if len(raw_line) > self._config.max_stream_line_bytes:
                raise self._line_too_large()
            if self._process_line(raw_line):
                self._done = True
                return True
        # A pending fragment without a newline still counts against the
        # per-line cap so a single gigantic no-newline line is rejected
        # before it grows without bound.
        if len(self._buffer) > self._config.max_stream_line_bytes:
            raise self._line_too_large()
        return False

    def finish(self) -> None:
        """Flush a trailing newline-less final line and ensure a FINAL event."""
        if not self._done and self._buffer:
            if len(self._buffer) > self._config.max_stream_line_bytes:
                raise self._line_too_large()
            self._process_line(bytes(self._buffer))
        if self.full_text and not self._emitted_final:
            self.pending_events.append(STTEvent(type=STTEventType.FINAL, text=self.full_text))
            self._emitted_final = True

    # ── Internals ─────────────────────────────────────────────────

    def _line_too_large(self) -> OpenAISTTStreamLimitError:
        return OpenAISTTStreamLimitError(
            "OpenAI STT streaming response line exceeded "
            f"{self._config.max_stream_line_bytes} bytes"
        )

    def _process_line(self, raw_line: bytes) -> bool:
        """Apply per-event caps; return ``True`` when the stream is done."""
        line = raw_line.decode("utf-8", "replace").strip()
        if not line:
            return False
        self._stream_events += 1
        if self._stream_events > self._config.max_stream_events:
            raise OpenAISTTStreamLimitError(
                f"OpenAI STT streaming response exceeded {self._config.max_stream_events} events"
            )
        payload = line
        if payload.startswith("data:"):
            payload = payload[5:].strip()
        if payload == "[DONE]":
            return True
        text, is_delta, is_final = OpenAISTT._extract_stream_text(payload)
        if not text:
            return False
        next_text = self.full_text + text if is_delta else text
        if len(next_text) > self._config.max_transcript_chars:
            raise OpenAISTTStreamLimitError(
                f"OpenAI STT transcript exceeded {self._config.max_transcript_chars} characters"
            )
        self.full_text = next_text
        self._partial_events += 1
        if self._partial_events > self._config.max_partial_events:
            raise OpenAISTTStreamLimitError(
                "OpenAI STT streaming response exceeded "
                f"{self._config.max_partial_events} partial events"
            )
        self.pending_events.append(STTEvent(type=STTEventType.PARTIAL, text=self.full_text))
        if is_final:
            self.pending_events.append(STTEvent(type=STTEventType.FINAL, text=self.full_text))
            self._emitted_final = True
            return True
        return False
