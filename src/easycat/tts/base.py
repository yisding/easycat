"""Base class for TTS providers with shared logic."""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncIterator

from easycat._audio_utils import PCM16StreamResampler, to_mono, validate_pcm16_format
from easycat.audio_format import PCM16_MONO_24K, AudioChunk, AudioFormat
from easycat.events import TTSEvent, TTSEventType
from easycat.tts.input import TTSInput, TTSInputPolicy

logger = logging.getLogger(__name__)


class _AudioConversionState:
    """Frame-alignment and resampling state for one streaming audio source."""

    def __init__(self, output_sample_rate: int) -> None:
        self.sample_carry = b""
        self.sample_carry_format: AudioFormat | None = None
        self.resampler = PCM16StreamResampler(output_sample_rate)

    def reset(self) -> None:
        self.sample_carry = b""
        self.sample_carry_format = None
        self.resampler.reset()


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
        self._default_audio_state = self._new_audio_conversion_state()

    def _new_audio_conversion_state(self) -> _AudioConversionState:
        """Create isolated alignment state for one provider stream/context."""
        return _AudioConversionState(self._output_format.sample_rate)

    @property
    def _sample_carry(self) -> bytes:
        return self._default_audio_state.sample_carry

    @_sample_carry.setter
    def _sample_carry(self, value: bytes) -> None:
        self._default_audio_state.sample_carry = value

    @property
    def _sample_carry_format(self) -> AudioFormat | None:
        return self._default_audio_state.sample_carry_format

    @_sample_carry_format.setter
    def _sample_carry_format(self, value: AudioFormat | None) -> None:
        self._default_audio_state.sample_carry_format = value

    @property
    def _resampler(self) -> PCM16StreamResampler:
        return self._default_audio_state.resampler

    @_resampler.setter
    def _resampler(self, value: PCM16StreamResampler) -> None:
        self._default_audio_state.resampler = value

    def _audio_state(self, state: _AudioConversionState | None) -> _AudioConversionState:
        return self._default_audio_state if state is None else state

    def _reset_audio_state(self, state: _AudioConversionState | None = None) -> None:
        self._audio_state(state).reset()

    @staticmethod
    def _validate_pcm16_format(name: str, fmt: AudioFormat) -> None:
        validate_pcm16_format(name, fmt)

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
        self._reset_audio_state()

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
        self._reset_audio_state()
        self._active = False

    def _reset_audio_alignment(self, *, state: _AudioConversionState | None = None) -> None:
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
        self._reset_audio_state(state)

    def _make_audio_event(
        self,
        data: bytes,
        fmt: AudioFormat | None = None,
        *,
        state: _AudioConversionState | None = None,
    ) -> TTSEvent | None:
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
        audio_state = self._audio_state(state)
        if audio_state.sample_carry_format != source_format:
            audio_state.sample_carry = b""
            audio_state.sample_carry_format = None

        frame_size = source_format.frame_size
        if frame_size > 1:
            data = audio_state.sample_carry + data
            remainder = len(data) % frame_size
            if remainder:
                audio_state.sample_carry = data[-remainder:]
                audio_state.sample_carry_format = source_format
                data = data[:-remainder]
            else:
                audio_state.sample_carry = b""
                audio_state.sample_carry_format = None
        data = self._normalize_audio(data, source_format, state=audio_state)
        # Quality streaming resamplers can retain an initial filter window.
        # Do not let that implementation detail masquerade as first audio to
        # timeout, observability, or playback accounting downstream.
        if not data:
            return None
        chunk = AudioChunk(data=data, format=self._output_format)
        return TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    def _finish_audio_event(
        self,
        *,
        emit: bool = True,
        state: _AudioConversionState | None = None,
    ) -> TTSEvent | None:
        """Flush delayed output after success, or discard it after cancellation."""
        audio_state = self._audio_state(state)
        audio_state.sample_carry = b""
        audio_state.sample_carry_format = None
        if not emit or self._cancelled:
            audio_state.resampler.reset()
            return None
        data = self._upmix_to_output_channels(audio_state.resampler.finish())
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

    def _normalize_audio(
        self,
        data: bytes,
        source_format: AudioFormat,
        *,
        state: _AudioConversionState | None = None,
    ) -> bytes:
        """Convert audio data to match the target output format.

        Preserves exact-format PCM16 data unchanged. Other channel layouts are
        downmixed before sample-rate conversion, then upmixed to the requested
        output layout. The streaming resampler therefore receives mono PCM16
        only and never interprets interleaved channels as a mono stream.
        Assumes PCM16 encoding throughout.
        """
        self._validate_pcm16_format("source_format", source_format)
        audio_state = self._audio_state(state)
        if source_format == self._output_format:
            # A previous non-exact segment may still have delayed resampler
            # output. Emit that tail before the first direct-format samples so
            # a source-format transition cannot reorder audio.
            return self._upmix_to_output_channels(audio_state.resampler.finish()) + data

        if source_format.channels > 1:
            data = to_mono(data, source_format.channels)

        # ``data`` is mono and sample-aligned: _make_audio_event held back any
        # incomplete source frame before calling here. Route same-rate chunks
        # through the stream converter too so a mid-stream source-rate change
        # flushes the prior segment before pass-through.
        data = audio_state.resampler.process(data, source_format.sample_rate)

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
