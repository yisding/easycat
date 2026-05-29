"""STT provider base class with shared logic."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from easycat._audio_utils import pcm_to_wav  # noqa: F401 — re-exported for backward compat
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent

logger = logging.getLogger(__name__)


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
