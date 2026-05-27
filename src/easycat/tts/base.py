"""Base class for TTS providers with shared logic."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from easycat._audio_utils import resample, to_mono
from easycat.audio_format import PCM16_MONO_24K, AudioChunk, AudioFormat
from easycat.events import TTSEvent, TTSEventType
from easycat.tts.input import TTSInput

logger = logging.getLogger(__name__)


class TTSBase:
    """Concrete base class for TTS providers.

    Provides shared functionality: audio format normalization to PCM16,
    TTSEvent construction helpers, and cancellation state tracking.

    Subclasses implement `synthesize` to produce TTSEvent objects and
    override `stop`/`cancel` with provider-specific cleanup.
    """

    def __init__(self, output_format: AudioFormat = PCM16_MONO_24K) -> None:
        self._output_format = output_format
        self._cancelled = False
        self._active = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def is_active(self) -> bool:
        return self._active

    def _start_synthesis(self) -> None:
        """Mark synthesis as active and reset cancellation state."""
        self._cancelled = False
        self._active = True

    def _end_synthesis(self) -> None:
        """Mark synthesis as complete."""
        self._active = False

    def _make_audio_event(self, data: bytes, fmt: AudioFormat | None = None) -> TTSEvent:
        """Create a TTSEvent with AUDIO type.

        If `fmt` differs from the target output format, the data is
        resampled and/or downmixed to match `self._output_format`.
        """
        if fmt is not None:
            data = self._normalize_audio(data, fmt)
        chunk = AudioChunk(data=data, format=self._output_format)
        return TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    def _make_markers_event(self, markers: list[dict]) -> TTSEvent:
        """Create a TTSEvent with MARKERS type."""
        return TTSEvent(type=TTSEventType.MARKERS, markers=markers)

    def _normalize_audio(self, data: bytes, source_format: AudioFormat) -> bytes:
        """Convert audio data to match the target output format.

        Handles mono downmix and sample rate conversion.
        Assumes PCM16 encoding throughout.
        """
        if source_format.channels > 1:
            data = to_mono(data, source_format.channels)

        if source_format.sample_rate != self._output_format.sample_rate:
            data = resample(data, source_format.sample_rate, self._output_format.sample_rate)

        return data

    @property
    def supports_ssml(self) -> bool:
        """Whether this provider accepts SSML input natively."""
        return False

    def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text into streaming TTSEvent objects.

        Subclasses must override this method.
        """
        raise NotImplementedError

    async def stop(self) -> None:
        """Gracefully stop the current synthesis."""
        self._active = False

    async def cancel(self) -> None:
        """Immediately cancel synthesis and discard pending output."""
        self._cancelled = True
        self._active = False

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
