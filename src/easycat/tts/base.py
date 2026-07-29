"""Base class for TTS providers with shared logic."""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncIterator

from easycat._audio_utils import PCM16StreamResampler, to_mono
from easycat.audio_format import PCM16_MONO_24K, AudioChunk, AudioFormat
from easycat.events import TTSEvent, TTSEventType
from easycat.tts.input import TTSInput, TTSInputPolicy

logger = logging.getLogger(__name__)


class TTSBase:
    """Concrete base class for TTS providers.

    Provides shared functionality: audio format normalization to PCM16,
    TTSEvent construction helpers, and cancellation state tracking.

    Subclasses implement `synthesize` to produce TTSEvent objects and
    override `stop`/`cancel` with provider-specific cleanup.
    """

    def __init__(self, output_format: AudioFormat = PCM16_MONO_24K) -> None:
        self._validate_pcm16_format("output_format", output_format)
        self._output_format = output_format
        self._cancelled = False
        self._active = False
        # Leftover sub-frame bytes carried across _make_audio_event calls so
        # every emitted AudioChunk is frame-aligned, even when a streaming
        # transport splits an interleaved multichannel frame. The format is
        # retained with the carry so bytes from one source format can never be
        # prepended to a different format after an upstream transition.
        self._sample_carry = b""
        self._sample_carry_format: AudioFormat | None = None
        self._resampler = PCM16StreamResampler(self._output_format.sample_rate)

    @staticmethod
    def _validate_pcm16_format(name: str, fmt: AudioFormat) -> None:
        if fmt.encoding != "pcm" or fmt.sample_width != 2:
            raise ValueError(
                f"{name} must be PCM16 audio "
                f"(got encoding={fmt.encoding!r}, sample_width={fmt.sample_width!r})"
            )
        if (
            isinstance(fmt.channels, bool)
            or not isinstance(fmt.channels, int)
            or fmt.channels <= 0
        ):
            raise ValueError(f"{name}.channels must be a positive integer (got {fmt.channels!r})")

    def _upmix_to_output_channels(self, data: bytes) -> bytes:
        channels = self._output_format.channels
        if channels == 1 or not data:
            return data
        sample_count = len(data) // 2
        samples = struct.unpack(f"<{sample_count}h", data)
        interleaved = tuple(sample for sample in samples for _ in range(channels))
        return struct.pack(f"<{len(interleaved)}h", *interleaved)

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
        self._sample_carry = b""
        self._sample_carry_format = None
        self._resampler.reset()

    def _end_synthesis(self) -> None:
        """Mark synthesis as complete and discard unfinished conversion state.

        Any non-empty ``_sample_carry`` here is a sub-frame remainder (less
        than one complete sample across all source channels) that no following
        frame ever completed. It is intentionally dropped rather than
        zero-padded and emitted: a fabricated partial frame would inject a
        click far more audible than the sub-frame silence lost, and emitting
        from here would force a return-value contract onto every subclass's
        synthesis loop. Successful
        synthesis paths call :meth:`_finish_audio_event` before this teardown;
        error, cancellation, and early-generator-close paths intentionally
        discard any delayed resampler output here.
        """
        self._sample_carry = b""
        self._sample_carry_format = None
        self._resampler.reset()
        self._active = False

    def _reset_audio_alignment(self) -> None:
        """Discard any held sub-sample remainder so the next chunk re-aligns.

        WebSocket providers replay the request on a mid-stream reconnect,
        restarting the utterance from the top on a fresh stream. The first
        chunk of that restarted stream is sample-aligned in its own right, so
        a stale ``_sample_carry`` byte left over from before the drop would
        prepend a spurious half-sample and shift every subsequent sample by
        one byte for the entire replayed utterance (turning the accepted
        audible repetition into full-duration static). Providers call this at
        the top of their ``_replay_request`` hook, mirroring the carry reset
        ``_start_synthesis``/``_end_synthesis`` perform around a synthesis run.
        """
        self._sample_carry = b""
        self._sample_carry_format = None
        self._resampler.reset()

    def _make_audio_event(self, data: bytes, fmt: AudioFormat | None = None) -> TTSEvent | None:
        """Create a non-empty TTSEvent with AUDIO type.

        Streaming sources (WebSocket / chunked HTTP) can split a single
        interleaved PCM frame across two messages, so an individual ``data``
        buffer may not be a whole number of frames. A sub-frame remainder is
        carried across calls so every emitted :class:`AudioChunk` is aligned,
        but is discarded if the source format changes before it is completed.

        If `fmt` differs from the target output format, the data is
        resampled and/or downmixed to match `self._output_format`.
        """
        source_format = fmt if fmt is not None else self._output_format
        self._validate_pcm16_format("source_format", source_format)
        if self._sample_carry_format != source_format:
            self._sample_carry = b""
            self._sample_carry_format = None

        frame_size = source_format.frame_size
        if frame_size > 1:
            data = self._sample_carry + data
            remainder = len(data) % frame_size
            if remainder:
                self._sample_carry = data[-remainder:]
                self._sample_carry_format = source_format
                data = data[:-remainder]
            else:
                self._sample_carry = b""
                self._sample_carry_format = None
        data = self._normalize_audio(data, source_format)
        # Quality streaming resamplers can retain an initial filter window.
        # Do not let that implementation detail masquerade as first audio to
        # timeout, observability, or playback accounting downstream.
        if not data:
            return None
        chunk = AudioChunk(data=data, format=self._output_format)
        return TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    def _finish_audio_event(self, *, emit: bool = True) -> TTSEvent | None:
        """Flush delayed output after success, or discard it after cancellation."""
        self._sample_carry = b""
        self._sample_carry_format = None
        if not emit or self._cancelled:
            self._resampler.reset()
            return None
        data = self._upmix_to_output_channels(self._resampler.finish())
        if not data:
            return None
        chunk = AudioChunk(data=data, format=self._output_format)
        return TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    def _make_markers_event(self, markers: list[dict]) -> TTSEvent:
        """Create a TTSEvent with MARKERS type.

        ``markers`` carries the provider's *native* alignment payload as-is
        (Cartesia word timestamps vs. ElevenLabs char alignment): this is a
        best-effort, debug-only event with no normalized cross-provider shape,
        so the journal records it opaquely and no pipeline code interprets it.
        See :class:`~easycat.events.TTSMarkers` for the documented contract.
        """
        return TTSEvent(type=TTSEventType.MARKERS, markers=markers)

    def _normalize_audio(self, data: bytes, source_format: AudioFormat) -> bytes:
        """Convert audio data to match the target output format.

        Preserves exact-format PCM16 data unchanged. Other channel layouts are
        downmixed before sample-rate conversion, then upmixed to the requested
        output layout. The streaming resampler therefore receives mono PCM16
        only and never interprets interleaved channels as a mono stream.
        Assumes PCM16 encoding throughout.
        """
        self._validate_pcm16_format("source_format", source_format)
        if source_format == self._output_format:
            # A previous non-exact segment may still have delayed resampler
            # output. Emit that tail before the first direct-format samples so
            # a source-format transition cannot reorder audio.
            return self._upmix_to_output_channels(self._resampler.finish()) + data

        if source_format.channels > 1:
            data = to_mono(data, source_format.channels)

        # ``data`` is mono and sample-aligned: _make_audio_event held back any
        # incomplete source frame before calling here. Route same-rate chunks
        # through the stream converter too so a mid-stream source-rate change
        # flushes the prior segment before pass-through.
        data = self._resampler.process(data, source_format.sample_rate)

        return self._upmix_to_output_channels(data)

    @property
    def input_policy(self) -> TTSInputPolicy:
        """Typed input contract for payloads delivered to ``synthesize``."""
        return TTSInputPolicy.plain_text()

    def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text into streaming TTSEvent objects.

        Subclasses must override this method. Unless a subclass advertises an
        :attr:`input_policy` that accepts SSML, the scheduler guarantees the
        payload is already plain text, so implementations only need
        ``payload.text``.
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
