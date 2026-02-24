"""Echo cancellation using LiveKit's AudioProcessingModule (WebRTC AEC3).

Provides an optional AEC pipeline stage that sits between noise reduction and
VAD.  The near-end (microphone) signal is cleaned via ``process``, and the
far-end (speaker) signal is fed as a reference via ``feed_reference``.

LiveKit APM requires 10 ms int16 PCM frames — the same encoding as EasyCat's
``AudioChunk``, just needs frame splitting.

Requires the ``livekit`` package (``uv add easycat[aec]``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from easycat.audio_format import AudioChunk
from easycat.extras import require_module

logger = logging.getLogger(__name__)

# 10 ms frame sizes at common sample rates (samples per frame).
_FRAME_SAMPLES_BY_RATE: dict[int, int] = {
    8000: 80,
    16000: 160,
    24000: 240,
    48000: 480,
}


def _frame_samples_for_rate(sample_rate: int) -> int:
    """Return the number of samples in a 10 ms frame at the given rate."""
    if sample_rate in _FRAME_SAMPLES_BY_RATE:
        return _FRAME_SAMPLES_BY_RATE[sample_rate]
    return sample_rate // 100


def _split_frames(data: bytes, frame_bytes: int) -> list[bytes]:
    """Split raw PCM data into fixed-size frames, zero-padding the last."""
    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        frame = data[offset : offset + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + b"\x00" * (frame_bytes - len(frame))
        frames.append(frame)
        offset += frame_bytes
    return frames


# ── LiveKit AEC ───────────────────────────────────────────────────


class LiveKitAEC:
    """Echo canceller using LiveKit's AudioProcessingModule (WebRTC AEC3).

    Requires the ``livekit`` package.
    """

    def __init__(self) -> None:
        rtc = require_module("livekit.rtc", extra="aec", purpose="Echo cancellation")
        self._apm: Any = rtc.AudioProcessingModule(echo_cancellation=True)
        logger.info("LiveKit AEC initialized")

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process a near-end (microphone) audio chunk through AEC."""
        frame_samples = _frame_samples_for_rate(chunk.format.sample_rate)
        frame_bytes = frame_samples * chunk.format.frame_size
        frames = _split_frames(chunk.data, frame_bytes)

        processed_parts: list[bytes] = []
        for frame in frames:
            result = self._apm.process_stream(frame)
            processed_parts.append(result)

        # Trim to original length (last frame may have been zero-padded).
        joined = b"".join(processed_parts)[: len(chunk.data)]
        return AudioChunk(data=joined, format=chunk.format, timestamp=chunk.timestamp)

    def feed_reference(self, chunk: AudioChunk) -> None:
        """Feed a far-end (speaker) audio chunk as the AEC reference signal."""
        frame_samples = _frame_samples_for_rate(chunk.format.sample_rate)
        frame_bytes = frame_samples * chunk.format.frame_size
        frames = _split_frames(chunk.data, frame_bytes)

        for frame in frames:
            self._apm.process_reverse_stream(frame)


# ── Passthrough (no-op) ──────────────────────────────────────────


class PassthroughAEC:
    """No-op echo canceller that passes audio through unchanged."""

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        pass


# ── Config & factory ─────────────────────────────────────────────


@dataclass
class EchoCancellationConfig:
    """Configuration for echo cancellation."""

    enabled: bool = False


def create_echo_canceller(config: EchoCancellationConfig | None = None) -> Any:
    """Create an echo canceller based on configuration.

    Returns a LiveKitAEC if enabled and the livekit package is available,
    otherwise returns a PassthroughAEC.
    """
    cfg = config or EchoCancellationConfig()

    if not cfg.enabled:
        return PassthroughAEC()

    try:
        return LiveKitAEC()
    except (ImportError, RuntimeError) as exc:
        logger.warning("LiveKit AEC not available, falling back to passthrough: %s", exc)
        return PassthroughAEC()
