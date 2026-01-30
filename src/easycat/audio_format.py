"""Audio format types and constants for EasyCat's internal audio contract."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


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


@dataclass
class AudioChunk:
    """A chunk of raw audio with format metadata."""

    data: bytes
    format: AudioFormat
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def num_samples(self) -> int:
        """Number of samples in this chunk (per channel)."""
        return len(self.data) // self.format.frame_size

    @property
    def duration_ms(self) -> float:
        """Duration of this chunk in milliseconds."""
        return (self.num_samples / self.format.sample_rate) * 1000
