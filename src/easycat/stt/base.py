"""STT provider base class with shared logic."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from easycat._audio_utils import pcm_to_wav  # noqa: F401 — re-exported for backward compat
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEvent

logger = logging.getLogger(__name__)


DEFAULT_MAX_AUDIO_CHUNK_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_AUDIO_BUFFER_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_AUDIO_DURATION_MS = 5 * 60 * 1000.0


class AudioBufferLimitExceeded(Exception):
    """Raised when a batch STT buffer reaches its cumulative cap.

    This is distinct from the per-chunk ``ValueError`` checks (malformed or
    impossibly large single frames). It signals that an *otherwise valid*
    stream has simply accumulated more buffered audio (total bytes or
    duration) than the configured cap allows — e.g. a long-talking caller on
    a held-open turn. Providers catch it to gracefully finalize the current
    utterance and start a fresh buffer, so the per-chunk pipeline error policy
    in ``session/_audio_router.py`` never sees it and the live call is not
    torn down.
    """


class STTBase:
    """Concrete base class for STT providers.

    Handles event queue management, audio format validation, and stream
    lifecycle. Subclasses override ``_on_start``, ``_on_audio``, and
    ``_on_end`` to add provider-specific behaviour.
    """

    def __init__(self, *, expected_sample_rate: int | None = None) -> None:
        # ``expected_sample_rate`` controls the strict-rate contract enforced
        # by ``_validate_audio``. When set, ``send_audio`` rejects any chunk
        # whose rate differs. When ``None`` (the convention used by all
        # EasyCat-bundled streaming providers), the provider is responsible
        # for resampling mismatched input to its own target rate in
        # ``_on_audio`` so callers can swap providers without crashing.
        self._event_queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._running = False
        self._expected_sample_rate = expected_sample_rate

    async def start_stream(self) -> None:
        """Begin a new STT stream session."""
        self._event_queue = asyncio.Queue()
        self._running = True
        try:
            await self._on_start()
        except Exception:
            self._running = False
            raise

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the active STT stream."""
        if not self._running:
            raise RuntimeError("Stream not started; call start_stream() first")
        self._validate_audio(chunk)
        await self._on_audio(chunk)

    async def commit_segment(self) -> bool:
        """Finalize the current segment without closing the stream.

        Returns ``True`` when the provider accepted the segment commit request.
        The default implementation returns ``False`` for providers that only
        support whole-stream finalization.
        """
        if not self._running:
            return False
        return await self._on_commit_segment()

    async def end_stream(self) -> None:
        """Signal that no more audio will be sent for the current stream."""
        if not self._running:
            return
        self._running = False
        try:
            await self._on_end()
        finally:
            await self._event_queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        """Return an async iterator of provider-scoped STT events."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    # -- Protected helpers for subclasses ----------------------------------

    def _emit_event(self, event: STTEvent) -> None:
        """Enqueue an STTEvent for consumers of ``events()``."""
        self._event_queue.put_nowait(event)

    @staticmethod
    def _latch_uniform_format(
        current: AudioFormat | None, chunk: AudioChunk, *, provider_label: str
    ) -> AudioFormat:
        """Latch the first-seen format and reject a mid-stream change.

        Batch STT providers wrap the whole buffered utterance in a single WAV
        header built from the first-seen format, so a mid-stream rate/channel
        change would be silently mislabeled (garbled / wrong-pitch transcript).
        The first chunk latches the format; a later mismatch raises
        ``ValueError`` rather than corrupting the transcript. Bundled
        transports resample inbound audio to a fixed pipeline rate before STT,
        so this only guards custom transports.
        """
        if current is None:
            return chunk.format
        if chunk.format != current:
            raise ValueError(
                f"{provider_label} received a mid-stream audio format change "
                f"({current} -> {chunk.format}); the batch path requires a "
                "uniform format for the whole utterance"
            )
        return current

    @staticmethod
    def _validate_positive_limit(name: str, value: int | float | None) -> None:
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be > 0 when set (got {value!r})")

    @staticmethod
    def _extend_limited_audio_buffer(
        buffer: bytearray,
        chunk: AudioChunk,
        *,
        max_chunk_bytes: int | None,
        max_buffer_bytes: int | None,
        max_duration_ms: float | None,
        provider_label: str,
    ) -> None:
        """Append ``chunk`` to ``buffer`` after enforcing batch-audio caps.

        A single chunk that is itself larger than ``max_chunk_bytes`` raises
        ``ValueError`` — that is a malformed/oversized frame, not a normal
        accumulation, so it should surface as a hard validation error. The
        *cumulative* caps (total buffered bytes, total buffered duration)
        raise :class:`AudioBufferLimitExceeded` instead, because a valid
        long-talking caller can reach them legitimately; providers catch that
        to finalize the utterance gracefully rather than tearing down the call.
        """
        chunk_bytes = len(chunk.data)
        if max_chunk_bytes is not None and chunk_bytes > max_chunk_bytes:
            raise ValueError(
                f"{provider_label} audio chunk exceeds the configured limit "
                f"({chunk_bytes} > {max_chunk_bytes} bytes)"
            )

        buffered_bytes = len(buffer) + chunk_bytes
        if max_buffer_bytes is not None and buffered_bytes > max_buffer_bytes:
            raise AudioBufferLimitExceeded(
                f"{provider_label} buffered audio exceeds the configured limit "
                f"({buffered_bytes} > {max_buffer_bytes} bytes)"
            )

        if max_duration_ms is not None:
            bytes_per_second = chunk.format.bytes_per_second
            if bytes_per_second <= 0:
                raise ValueError(
                    f"{provider_label} cannot enforce a duration cap on audio with "
                    f"non-positive byte rate ({bytes_per_second} bytes/s); check the "
                    "chunk's sample_rate/channels/sample_width"
                )
            buffered_duration_ms = (buffered_bytes / bytes_per_second) * 1000
            if buffered_duration_ms > max_duration_ms:
                raise AudioBufferLimitExceeded(
                    f"{provider_label} buffered audio duration exceeds the configured limit "
                    f"({buffered_duration_ms:.0f} > {max_duration_ms:.0f} ms)"
                )

        buffer.extend(chunk.data)

    async def _buffer_batch_audio_or_finalize(
        self,
        buffer: bytearray,
        chunk: AudioChunk,
        *,
        max_chunk_bytes: int | None,
        max_buffer_bytes: int | None,
        max_duration_ms: float | None,
        provider_label: str,
        finalize: Callable[[], Awaitable[None]],
    ) -> None:
        """Buffer ``chunk`` for a batch utterance, finalizing on a cumulative cap.

        Wraps :meth:`_extend_limited_audio_buffer`. On a cumulative-cap hit
        (:class:`AudioBufferLimitExceeded`) the already-buffered audio is
        flushed through ``finalize`` (which transcribes + emits + clears the
        buffer), and the new ``chunk`` then starts a fresh buffer. This keeps
        a long-talking caller's live call running: the current utterance is
        finalized early instead of an error tearing down the pipeline.

        A genuinely oversized *single* chunk still raises ``ValueError`` (it is
        re-raised, not swallowed) because retrying it is futile; and if the new
        chunk alone would re-trip a cumulative cap on the now-empty buffer it is
        dropped (with a warning) rather than looping forever.
        """
        try:
            self._extend_limited_audio_buffer(
                buffer,
                chunk,
                max_chunk_bytes=max_chunk_bytes,
                max_buffer_bytes=max_buffer_bytes,
                max_duration_ms=max_duration_ms,
                provider_label=provider_label,
            )
            return
        except AudioBufferLimitExceeded as exc:
            logger.info(
                "%s reached its batch buffer cap (%s); finalizing the current "
                "utterance and starting a fresh stream",
                provider_label,
                exc,
            )

        # Flush whatever is buffered so the caller's speech so far is not lost.
        await finalize()

        # Start a fresh utterance with the chunk that tripped the cap. If that
        # chunk on its own still exceeds a cumulative cap, drop it instead of
        # looping (the buffer is already empty, so finalizing again is a no-op).
        try:
            self._extend_limited_audio_buffer(
                buffer,
                chunk,
                max_chunk_bytes=max_chunk_bytes,
                max_buffer_bytes=max_buffer_bytes,
                max_duration_ms=max_duration_ms,
                provider_label=provider_label,
            )
        except AudioBufferLimitExceeded as exc:
            logger.warning(
                "%s dropping a single chunk that exceeds the batch buffer cap "
                "on an empty buffer (%s)",
                provider_label,
                exc,
            )

    def _validate_audio(self, chunk: AudioChunk) -> None:
        if chunk.format.encoding != "pcm":
            raise ValueError(f"Expected PCM encoding, got '{chunk.format.encoding}'")
        if (
            self._expected_sample_rate is not None
            and chunk.format.sample_rate != self._expected_sample_rate
        ):
            raise ValueError(
                f"Expected sample rate {self._expected_sample_rate}, "
                f"got {chunk.format.sample_rate}"
            )

    # -- Hooks for subclasses to override ----------------------------------

    async def _on_start(self) -> None:
        """Called when a new stream starts. Override in subclass."""

    async def _on_audio(self, chunk: AudioChunk) -> None:
        """Called for each audio chunk. Override in subclass."""

    async def _on_commit_segment(self) -> bool:
        """Finalize the current segment without closing the stream."""
        return False

    async def _on_end(self) -> None:
        """Called when the stream ends. Override in subclass."""

    # -- Provider metadata ----------------------------------------------------

    def version_info(self) -> dict[str, str]:
        """Return stable-shape dict identifying this provider.

        Keys: ``provider``, ``model``, ``api_version``, ``sdk_version``.
        Unknown fields are ``"unknown"`` rather than omitted.
        """
        return {
            "provider": "unknown",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }
