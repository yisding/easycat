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
from typing import Any, Literal, TypeAlias, cast

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
EchoCancellationFallbackPolicy: TypeAlias = Literal["passthrough", "error"]
_VALID_AEC_FALLBACK_POLICIES: tuple[EchoCancellationFallbackPolicy, ...] = (
    "passthrough",
    "error",
)


def _validate_aec_fallback_policy(policy: str) -> EchoCancellationFallbackPolicy:
    if policy not in _VALID_AEC_FALLBACK_POLICIES:
        allowed = ", ".join(_VALID_AEC_FALLBACK_POLICIES)
        raise ValueError(
            f"Unknown echo cancellation fallback_policy '{policy}'. Expected one of: {allowed}."
        )
    return cast(EchoCancellationFallbackPolicy, policy)


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
        self._rtc = require_module("livekit.rtc", extra="aec", purpose="Echo cancellation")
        self._apm: Any = self._rtc.AudioProcessingModule(echo_cancellation=True)
        logger.info("LiveKit AEC initialized")

    def close(self) -> None:
        """Release AudioProcessingModule resources."""
        self._apm = None

    def __del__(self) -> None:
        self.close()

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process a near-end (microphone) audio chunk through AEC.

        LiveKit's APM modifies the ``AudioFrame`` in place, so we wrap
        each 10 ms slice, invoke ``process_stream``, then reassemble
        from the frame's (now-processed) data buffer.
        """
        fmt = chunk.format
        frame_samples = _frame_samples_for_rate(fmt.sample_rate)
        frame_bytes = frame_samples * fmt.frame_size
        frames = _split_frames(chunk.data, frame_bytes)

        processed_parts: list[bytes] = []
        for frame_bytes_slice in frames:
            af = self._rtc.AudioFrame(
                data=frame_bytes_slice,
                sample_rate=fmt.sample_rate,
                num_channels=fmt.channels,
                samples_per_channel=frame_samples,
            )
            self._apm.process_stream(af)
            processed_parts.append(bytes(af.data))

        # Trim to original length (last frame may have been zero-padded).
        joined = b"".join(processed_parts)[: len(chunk.data)]
        return AudioChunk(data=joined, format=fmt, timestamp=chunk.timestamp)

    def feed_reference(self, chunk: AudioChunk) -> None:
        """Feed a far-end (speaker) audio chunk as the AEC reference signal."""
        fmt = chunk.format
        frame_samples = _frame_samples_for_rate(fmt.sample_rate)
        frame_bytes = frame_samples * fmt.frame_size
        frames = _split_frames(chunk.data, frame_bytes)

        for frame_bytes_slice in frames:
            af = self._rtc.AudioFrame(
                data=frame_bytes_slice,
                sample_rate=fmt.sample_rate,
                num_channels=fmt.channels,
                samples_per_channel=frame_samples,
            )
            self._apm.process_reverse_stream(af)

    def version_info(self) -> dict[str, str]:
        sdk_ver = "unknown"
        try:
            from importlib.metadata import version

            sdk_ver = version("livekit")
        except Exception:
            pass
        return {
            "provider": "livekit",
            "model": "webrtc-aec3",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


# ── Passthrough (no-op) ──────────────────────────────────────────


class PassthroughAEC:
    """No-op echo canceller that passes audio through unchanged."""

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "passthrough",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }


# ── Config & factory ─────────────────────────────────────────────


@dataclass
class EchoCancellationConfig:
    """Configuration for echo cancellation."""

    enabled: bool = False
    fallback_policy: EchoCancellationFallbackPolicy = "passthrough"

    def __post_init__(self) -> None:
        self.fallback_policy = _validate_aec_fallback_policy(self.fallback_policy)


def create_echo_canceller(config: EchoCancellationConfig | None = None) -> Any:
    """Create an echo canceller based on configuration.

    Returns LiveKitAEC when enabled and the livekit package is available.
    Missing LiveKit falls back to PassthroughAEC when fallback_policy is
    "passthrough", or raises RuntimeError when fallback_policy is "error".
    """
    cfg = config or EchoCancellationConfig()
    cfg.fallback_policy = _validate_aec_fallback_policy(cfg.fallback_policy)

    if not cfg.enabled:
        return PassthroughAEC()

    try:
        return LiveKitAEC()
    except (ImportError, RuntimeError) as exc:
        if cfg.fallback_policy == "error":
            raise RuntimeError(
                f"Echo cancellation requested but LiveKit AEC is unavailable: {exc}"
            ) from exc
        logger.warning("LiveKit AEC not available, falling back to passthrough: %s", exc)
        return PassthroughAEC()
