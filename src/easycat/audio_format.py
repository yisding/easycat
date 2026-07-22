"""Audio format types and constants for EasyCat's internal audio contract."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from easycat._turn_context import TurnContext


@dataclass(frozen=True)
class AudioFormat:
    """Describes a raw audio encoding."""

    sample_rate: int
    channels: int
    sample_width: int  # bytes per sample (2 = 16-bit)
    encoding: str = "pcm"

    @property
    def frame_size(self) -> int:
        """Bytes per frame (one sample across all channels)."""
        return self.channels * self.sample_width

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.frame_size


# Standard format constants
PCM16_MONO_8K = AudioFormat(sample_rate=8000, channels=1, sample_width=2)
PCM16_MONO_16K = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
PCM16_MONO_24K = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
PCM16_MONO_48K = AudioFormat(sample_rate=48000, channels=1, sample_width=2)


@dataclass
class AudioChunk:
    """A chunk of raw audio with format metadata.

    The ``_easycat_*`` fields are internal outbound-routing metadata stamped by
    :class:`~easycat.session._audio_router.AudioRouter` as a chunk leaves the
    pipeline. They let buffered/reporting transports (which emit a bare
    ``TransportAudioDelivered`` callback later) re-attribute a delivered chunk
    to the originating session and turn, and let the outbound drain loop tell
    replay chunks apart from ordinary synthesis audio. They default to unset so
    inbound and freshly synthesized chunks carry no ownership claim.
    """

    data: bytes
    format: AudioFormat
    timestamp: float = field(default_factory=time.monotonic)
    # Outbound-routing metadata (see class docstring); stamped by AudioRouter.
    # Kept out of ``repr``/``__eq__`` so two chunks with identical audio compare
    # equal regardless of routing metadata, matching the prior side-channel
    # (setattr) behavior these fields replaced.
    _easycat_replay_chunk: bool = field(default=False, repr=False, compare=False)
    _easycat_session_id: str | None = field(default=None, repr=False, compare=False)
    _easycat_turn_id: str | None = field(default=None, repr=False, compare=False)
    _easycat_turn_ref: TurnContext | None = field(default=None, repr=False, compare=False)

    @property
    def num_samples(self) -> int:
        """Number of samples in this chunk (per channel)."""
        return len(self.data) // self.format.frame_size

    @property
    def duration_ms(self) -> float:
        """Duration of this chunk in milliseconds."""
        return (self.num_samples / self.format.sample_rate) * 1000
