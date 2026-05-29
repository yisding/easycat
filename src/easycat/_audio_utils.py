"""Audio processing utilities: resampling, mono downmix, chunk sizing, and format conversion."""

from __future__ import annotations

import io
import logging
import struct
from collections.abc import Iterator

from easycat.audio_format import AudioChunk, AudioFormat

logger = logging.getLogger(__name__)

# Cache of which optional resampling backend resolved, to avoid re-importing
# on every chunk. Values: "soxr", "scipy", "linear", or None when not yet
# probed. This reflects which backend is *available*, not whether it last
# succeeded: a transient runtime failure falls back to linear for that one
# chunk only and the high-quality backend is retried on the next chunk.
_resolved_backend: str | None = None

# Track whether a real runtime failure for each backend has already been
# logged, so we warn once (with a traceback) rather than on every chunk while
# still retrying the high-quality backend. A transient native-lib hiccup must
# not permanently degrade quality for the lifetime of the process.
_logged_runtime_failure: set[str] = set()


def pcm_to_wav(pcm_data: bytes, fmt: AudioFormat) -> bytes:
    """Convert raw PCM16 data to WAV file bytes."""
    buf = io.BytesIO()
    data_size = len(pcm_data)
    bits_per_sample = fmt.sample_width * 8

    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))
    buf.write(struct.pack("<H", 1))  # PCM format
    buf.write(struct.pack("<H", fmt.channels))
    buf.write(struct.pack("<I", fmt.sample_rate))
    buf.write(struct.pack("<I", fmt.bytes_per_second))
    buf.write(struct.pack("<H", fmt.frame_size))
    buf.write(struct.pack("<H", bits_per_sample))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm_data)

    return buf.getvalue()


def resample(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM16 *mono* audio between sample rates.

    The byte buffer is treated as a single interleaved-free int16 stream,
    so multi-channel input must be downmixed (see :func:`to_mono` /
    :func:`to_mono_chunk`) before calling this; interleaved stereo would be
    resampled as garbage. Prefers high-quality backends (soxr, scipy) when
    available and falls back to linear interpolation if not.
    """
    if from_rate == to_rate:
        return data
    if not data:
        return data

    # Drop any odd trailing byte: a 16-bit sample split across a chunk boundary
    # can't be reconstructed within a single call and would otherwise crash both
    # np.frombuffer and struct.unpack. Callers that stream arbitrary byte-length
    # chunks (TTSBase) buffer the leftover byte so no audio is actually lost.
    if len(data) % 2:
        data = data[:-1]
        if not data:
            return b""

    global _resolved_backend

    # Resolve the best available backend once and cache the result. A runtime
    # failure does not change this cache, so the high-quality backend is
    # retried on every chunk and only the (logged-once) failures fall back to
    # linear for the affected chunk.
    if _resolved_backend is None:
        _resolved_backend = _resolve_resample_backend()

    if _resolved_backend == "soxr":
        result = _resample_soxr(data, from_rate, to_rate)
        if result is not None:
            return result
    elif _resolved_backend == "scipy":
        result = _resample_scipy(data, from_rate, to_rate)
        if result is not None:
            return result

    return _resample_linear(data, from_rate, to_rate)


def _resolve_resample_backend() -> str:
    """Probe for an optional high-quality resampling backend exactly once.

    Returns ``"soxr"``, ``"scipy"``, or ``"linear"``. A missing import is
    expected and silent; a backend that imports but fails at runtime is logged
    once so silent quality regressions are observable.
    """
    try:
        import numpy  # type: ignore[import-untyped]  # noqa: F401
        import soxr  # type: ignore[import-not-found]  # noqa: F401

        return "soxr"
    except ImportError:
        pass

    try:
        import numpy  # type: ignore[import-untyped]  # noqa: F401
        from scipy.signal import resample_poly  # type: ignore[import-not-found]  # noqa: F401

        return "scipy"
    except ImportError:
        pass

    return "linear"


def _resample_soxr(data: bytes, from_rate: int, to_rate: int) -> bytes | None:
    """Resample via soxr; return ``None`` on a real failure.

    A failure falls back to linear for this chunk only; soxr is retried on the
    next chunk. The failure is logged once (with a traceback) to surface a
    quality regression without spamming the log on every chunk.
    """
    try:
        return _resample_soxr_impl(data, from_rate, to_rate)
    except Exception:
        _log_runtime_failure_once("soxr")
        return None


def _resample_soxr_impl(data: bytes, from_rate: int, to_rate: int) -> bytes:
    import numpy as np  # type: ignore[import-untyped]
    import soxr  # type: ignore[import-not-found]

    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    resampled = soxr.resample(samples, from_rate, to_rate)
    out = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
    return out.tobytes()


def _resample_scipy(data: bytes, from_rate: int, to_rate: int) -> bytes | None:
    """Resample via scipy; return ``None`` on a real failure.

    A failure falls back to linear for this chunk only; scipy is retried on the
    next chunk. The failure is logged once (with a traceback) to surface a
    quality regression without spamming the log on every chunk.
    """
    try:
        return _resample_scipy_impl(data, from_rate, to_rate)
    except Exception:
        _log_runtime_failure_once("scipy")
        return None


def _resample_scipy_impl(data: bytes, from_rate: int, to_rate: int) -> bytes:
    import math

    import numpy as np  # type: ignore[import-untyped]
    from scipy.signal import resample_poly  # type: ignore[import-not-found]

    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    g = math.gcd(from_rate, to_rate)
    up = to_rate // g
    down = from_rate // g
    resampled = resample_poly(samples, up, down)
    out = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
    return out.tobytes()


def _log_runtime_failure_once(backend: str) -> None:
    """Warn (with traceback) the first time ``backend`` fails at runtime.

    Subsequent failures for the same backend are silent to avoid per-chunk log
    spam, but the backend itself is still retried on later chunks.
    """
    if backend in _logged_runtime_failure:
        return
    _logged_runtime_failure.add(backend)
    logger.warning(
        "%s resampling failed; falling back to linear interpolation (lower "
        "quality) for this chunk. The high-quality backend will be retried on "
        "subsequent chunks; suppressing further %s failure logs.",
        backend,
        backend,
        exc_info=True,
    )


def _resample_linear(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Pure-Python linear-interpolation resampler (no optional deps).

    Tolerates an odd trailing byte (a 16-bit sample split across a chunk
    boundary) by dropping it rather than raising ``struct.error``.
    """
    # Decode PCM16 LE samples, dropping any odd trailing byte that would
    # otherwise split a 16-bit sample and crash struct.unpack.
    num_samples = len(data) // 2
    data = data[: num_samples * 2]
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
    for name, value in (
        ("frame_duration_ms", frame_duration_ms),
        ("sample_rate", sample_rate),
        ("sample_width", sample_width),
        ("channels", channels),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    frame_samples = (sample_rate * frame_duration_ms) // 1000
    frame_bytes = frame_samples * sample_width * channels
    if frame_bytes <= 0:
        raise ValueError("frame_duration_ms is too small for sample_rate")

    offset = 0
    while offset < len(audio):
        end = offset + frame_bytes
        frame = audio[offset:end]
        yield frame
        offset = end
