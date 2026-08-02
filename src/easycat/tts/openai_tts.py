"""OpenAI TTS provider implementation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx

from easycat._provider_helpers import ProviderErrorEmitter, get_package_version
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import ErrorStage, TTSEvent
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput, coerce_tts_input

logger = logging.getLogger(__name__)


# OpenAI's pcm response format returns raw PCM16 at 24kHz mono
_OPENAI_PCM_FORMAT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)

# Release one transport-friendly 20 ms frame as soon as it is available, then
# return to 100 ms chunks for steady-state playback.  Passing ``chunk_size=4800``
# to httpx buffered a full 100 ms of 24 kHz PCM before the first yield, adding
# avoidable framework latency after the provider had already started streaming.
_FIRST_AUDIO_CHUNK_BYTES = 960
_STEADY_AUDIO_CHUNK_BYTES = 4800


async def _iter_low_latency_pcm_chunks(
    source: AsyncIterable[bytes],
    *,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[bytes]:
    """Rechunk raw response bytes with a small first frame and stable tail.

    httpx's default ``aiter_bytes()`` preserves the response's natural stream
    cadence.  This helper coalesces that cadence into a 20 ms first PCM frame,
    followed by the existing 100 ms frame size, and flushes the final remainder
    without dropping or duplicating bytes.
    """
    pending = bytearray()
    target = _FIRST_AUDIO_CHUNK_BYTES
    stopped = should_stop or (lambda: False)

    iterator = source.__aiter__()
    while not stopped():
        try:
            chunk = await anext(iterator)
        except StopAsyncIteration:
            break
        if not chunk:
            continue
        pending.extend(chunk)
        while len(pending) >= target and not stopped():
            output = bytes(pending[:target])
            del pending[:target]
            target = _STEADY_AUDIO_CHUNK_BYTES
            yield output

    if pending and not stopped():
        yield bytes(pending)


@dataclass
class OpenAITTSConfig:
    """Configuration for the OpenAI TTS provider."""

    api_key: str = field(default="", repr=False)
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    speed: float = 1.0
    base_url: str = "https://api.openai.com/v1"
    output_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    # Declaring this field lets ``create_tts_provider_from_config`` auto-wire
    # the session event bus (it detects the field structurally), so OpenAI TTS
    # emits journal-visible provider Errors on failure like the WS providers.
    event_bus: object | None = None


class OpenAITTS(ProviderErrorEmitter, TTSBase):
    """TTS provider using OpenAI's Audio API.

    Uses the `audio/speech` endpoint with `response_format=pcm` to get
    raw PCM16 audio at 24kHz, avoiding any need for MP3/Opus decoding.
    Streaming is done via httpx's async streaming response.
    """

    _error_stage = ErrorStage.TTS
    _provider_error_name = "openai"

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
        self._init_emit_tasks()

    async def warmup(self) -> None:
        """Prime the HTTP connection pool before the first speech request.

        Issues a cheap ``GET /models`` against the same origin the billed
        ``/audio/speech`` POST reuses, warming DNS/TLS/keep-alive without
        making a billed synthesis call.  All failures are swallowed: warmup
        is best-effort and ``WarmupRunner`` re-raises, so an auth/timeout
        error here must not abort ``Session.start()``.
        """
        try:
            response = await self._client.get("/models")
            await response.aclose()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("OpenAI TTS warmup skipped: %s", exc)

    @staticmethod
    async def _raise_for_streaming_status(response: httpx.Response) -> None:
        if not response.is_success:
            # A streamed non-2xx response has an unread body; read it now,
            # while the stream is still open, so the except handler can
            # touch exc.response.text without raising ResponseNotRead.
            await response.aread()
        response.raise_for_status()

    async def _iter_response_audio(self, response: httpx.Response) -> AsyncIterator[TTSEvent]:
        async for chunk in _iter_low_latency_pcm_chunks(
            response.aiter_bytes(), should_stop=lambda: self._cancelled
        ):
            if self._cancelled:
                break
            if not chunk:
                continue
            event = self._make_audio_event(chunk, _OPENAI_PCM_FORMAT)
            if event is not None:
                yield event

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text using OpenAI Audio API with streaming response.

        Requests PCM16 format directly to avoid decoding overhead.
        Yields TTSEvent objects with AUDIO type containing PCM16 chunks.

        The default input policy makes the scheduler deliver plain text here.
        """
        self._start_synthesis()

        text = coerce_tts_input(payload).text

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
                await self._raise_for_streaming_status(response)

                # Read at the response's native cadence so httpx does not hold
                # the first audio until a full 100 ms / 4800-byte block has
                # accumulated.  The local rechunker releases 20 ms first and
                # preserves the former 100 ms steady-state frame size.
                async for event in self._iter_response_audio(response):
                    yield event
            tail = self._finish_audio_event()
            if tail is not None:
                yield tail

        except httpx.HTTPStatusError as exc:
            logger.error(
                "OpenAI TTS API error: %s %s", exc.response.status_code, exc.response.text
            )
            self._emit_provider_error(
                exc, http_status=exc.response.status_code, body=exc.response.text[:400]
            )
            raise
        except httpx.StreamError as exc:
            if not self._cancelled:
                raise
            logger.debug("OpenAI TTS stream closed after cancel: %s", exc)
        except httpx.HTTPError as exc:
            if not self._cancelled:
                logger.error("OpenAI TTS HTTP error: %s", exc)
                self._emit_provider_error(exc)
                raise
            # A connection error that races a barge-in cancel is expected; log
            # at debug so the masked failure is still recoverable from a bundle.
            logger.debug("OpenAI TTS HTTP error after cancel: %s", exc)
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
        # Await any in-flight fire-and-forget Error-emit tasks so teardown does
        # not leave them dangling into interpreter shutdown.
        await self._drain_emit_tasks()

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "openai",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("httpx"),
        }
