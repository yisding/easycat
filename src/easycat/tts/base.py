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
        # Leftover odd byte carried across chunks so a 16-bit PCM sample is
        # never split at an arbitrary streaming-chunk boundary before resample.
        self._resample_carry = b""
        # Leftover sub-sample bytes carried across _make_audio_event calls so
        # every emitted AudioChunk is sample-aligned, even when no resample
        # happens (e.g. a WS frame whose length is not a multiple of the
        # sample width).
        self._sample_carry = b""

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
        self._resample_carry = b""
        self._sample_carry = b""

    def _end_synthesis(self) -> None:
        """Mark synthesis as complete."""
        self._active = False

    def _make_audio_event(self, data: bytes, fmt: AudioFormat | None = None) -> TTSEvent:
        """Create a TTSEvent with AUDIO type.

        Streaming sources (WebSocket / chunked HTTP) can split a single
        16-bit PCM sample across two frames, so an individual ``data``
        buffer may not be a whole number of samples. A 1-byte remainder
        is carried across calls so every emitted :class:`AudioChunk` is
        sample-aligned, regardless of whether ``fmt`` triggers a resample.

        If `fmt` differs from the target output format, the data is
        resampled and/or downmixed to match `self._output_format`.
        """
        source_format = fmt if fmt is not None else self._output_format
        sample_width = source_format.sample_width * source_format.channels
        if sample_width > 1:
            data = self._sample_carry + data
            remainder = len(data) % sample_width
            if remainder:
                self._sample_carry = data[-remainder:]
                data = data[:-remainder]
            else:
                self._sample_carry = b""
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
            # Prepend any leftover byte from the previous chunk and hold back a
            # new trailing odd byte so a 16-bit sample is never split across
            # streaming chunks (which would corrupt audio or crash resample).
            data = self._resample_carry + data
            if len(data) % 2:
                self._resample_carry = data[-1:]
                data = data[:-1]
            else:
                self._resample_carry = b""
            data = resample(data, source_format.sample_rate, self._output_format.sample_rate)

        return data

    @property
    def supports_ssml(self) -> bool:
        """Whether this provider accepts SSML input natively.

        The scheduler (:class:`~easycat.session._tts_scheduler.TTSScheduler`)
        reads this flag *before* calling :meth:`synthesize`: when it is
        ``False`` (the default for every built-in provider) any ``ssml``
        payload is downgraded to plain text via ``strip_ssml_tags`` up front.
        A provider that overrides this to ``True`` opts into receiving the
        raw SSML markup unchanged and is responsible for forwarding it to
        its backend.
        """
        return False

    def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text into streaming TTSEvent objects.

        Subclasses must override this method.  Unless a subclass advertises
        :attr:`supports_ssml`, the scheduler guarantees the payload is
        already plain text, so implementations only need ``payload.text``.
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
