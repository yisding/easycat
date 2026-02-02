"""Audio processing utilities: resampling, mono downmix, and chunk sizing."""

from __future__ import annotations

import struct
from collections.abc import Iterator

from easycat.audio_format import AudioChunk, AudioFormat


def resample(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM16 mono audio between sample rates.

    Prefers high-quality backends (soxr, scipy) when available and
    falls back to linear interpolation if not.
    """
    if from_rate == to_rate:
        return data
    if not data:
        return data

    # Try soxr (highest quality, fast) if installed
    try:
        import numpy as np  # type: ignore[import-untyped]
        import soxr  # type: ignore[import-not-found]

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        resampled = soxr.resample(samples, from_rate, to_rate)
        out = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
        return out.tobytes()
    except Exception:
        pass

    # Try scipy.signal.resample_poly as a quality fallback
    try:
        import math

        import numpy as np  # type: ignore[import-untyped]
        from scipy.signal import resample_poly  # type: ignore[import-not-found]

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        g = math.gcd(from_rate, to_rate)
        up = to_rate // g
        down = from_rate // g
        resampled = resample_poly(samples, up, down)
        out = np.clip(resampled, -32768, 32767).astype(np.int16)
        return out.tobytes()
    except Exception:
        pass

    # Decode PCM16 LE samples
    num_samples = len(data) // 2
    samples = struct.unpack(f"<{num_samples}h", data)

    ratio = from_rate / to_rate
    out_len = int(num_samples / ratio)

    out_samples: list[int] = []
    for i in range(out_len):
        src_pos = i * ratio
        idx = int(src_pos)
        frac = src_pos - idx

        if idx + 1 < num_samples:
            value = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            value = samples[idx] if idx < num_samples else 0
        # Clamp to int16 range
        out_samples.append(max(-32768, min(32767, int(round(value)))))

    return struct.pack(f"<{len(out_samples)}h", *out_samples)


def resample_chunk(chunk: AudioChunk, to_rate: int) -> AudioChunk:
    """Resample an AudioChunk to a different sample rate."""
    if chunk.format.sample_rate == to_rate:
        return chunk
    new_data = resample(chunk.data, chunk.format.sample_rate, to_rate)
    new_format = AudioFormat(
        sample_rate=to_rate,
        channels=chunk.format.channels,
        sample_width=chunk.format.sample_width,
        encoding=chunk.format.encoding,
    )
    return AudioChunk(data=new_data, format=new_format, timestamp=chunk.timestamp)


def to_mono(data: bytes, channels: int) -> bytes:
    """Downmix multi-channel PCM16 audio to mono by averaging channels."""
    if channels == 1:
        return data

    samples_per_frame = channels
    bytes_per_sample = 2
    frame_size = samples_per_frame * bytes_per_sample
    num_frames = len(data) // frame_size

    mono_samples: list[int] = []
    for i in range(num_frames):
        offset = i * frame_size
        frame_samples = struct.unpack(f"<{channels}h", data[offset : offset + frame_size])
        avg = sum(frame_samples) // channels
        mono_samples.append(max(-32768, min(32767, avg)))

    return struct.pack(f"<{len(mono_samples)}h", *mono_samples)


def to_mono_chunk(chunk: AudioChunk) -> AudioChunk:
    """Downmix an AudioChunk to mono."""
    if chunk.format.channels == 1:
        return chunk
    new_data = to_mono(chunk.data, chunk.format.channels)
    new_format = AudioFormat(
        sample_rate=chunk.format.sample_rate,
        channels=1,
        sample_width=chunk.format.sample_width,
        encoding=chunk.format.encoding,
    )
    return AudioChunk(data=new_data, format=new_format, timestamp=chunk.timestamp)


def chunk_frames(
    audio: bytes,
    frame_duration_ms: int,
    sample_rate: int,
    sample_width: int = 2,
    channels: int = 1,
) -> Iterator[bytes]:
    """Split raw audio bytes into fixed-duration frames.

    Yields frames of exactly `frame_duration_ms` milliseconds. A final
    partial frame (shorter than the requested duration) is yielded if
    there are leftover bytes.
    """
    frame_samples = (sample_rate * frame_duration_ms) // 1000
    frame_bytes = frame_samples * sample_width * channels

    offset = 0
    while offset < len(audio):
        end = offset + frame_bytes
        frame = audio[offset:end]
        yield frame
        offset = end
