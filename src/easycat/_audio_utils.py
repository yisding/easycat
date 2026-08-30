"""Audio processing utilities: resampling, mono downmix, chunk sizing, and format conversion."""

from __future__ import annotations

import io
import logging
import math
import struct
import sys
from collections.abc import Iterator
from dataclasses import replace
from functools import lru_cache
from typing import Any

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


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def validate_pcm16_format(name: str, fmt: AudioFormat) -> None:
    """Require PCM16 audio with a positive integer channel count."""
    if fmt.encoding != "pcm" or fmt.sample_width != 2:
        raise ValueError(
            f"{name} must be PCM16 audio "
            f"(got encoding={fmt.encoding!r}, sample_width={fmt.sample_width!r})"
        )
    if isinstance(fmt.channels, bool) or not isinstance(fmt.channels, int) or fmt.channels <= 0:
        raise ValueError(f"{name}.channels must be a positive integer (got {fmt.channels!r})")


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
    from_rate = _require_positive_int("from_rate", from_rate)
    to_rate = _require_positive_int("to_rate", to_rate)
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

    backend = resample_backend()
    if backend == "soxr":
        result = _resample_soxr(data, from_rate, to_rate)
        if result is not None:
            return result
    elif backend == "scipy":
        result = _resample_scipy(data, from_rate, to_rate)
        if result is not None:
            return result

    return _resample_linear(data, from_rate, to_rate)


def resample_backend() -> str:
    """Return the cached resampling backend used by :func:`resample`.

    This diagnostic keeps operator tooling from reaching into the module's
    private cache.
    """
    global _resolved_backend
    if _resolved_backend is None:
        _resolved_backend = _resolve_resample_backend()
    return _resolved_backend


def _resolve_resample_backend() -> str:
    """Probe for an optional high-quality resampling backend exactly once.

    Returns ``"soxr"``, ``"scipy"``, or ``"linear"``. A missing import is
    expected and silent; a backend that imports but fails at runtime is logged
    once so silent quality regressions are observable.
    """
    try:
        import numpy  # type: ignore[import-untyped]
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
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
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
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
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
    )


def _resample_linear(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Dependency-free filtered linear-interpolation resampler.

    Tolerates an odd trailing byte (a 16-bit sample split across a chunk
    boundary) by dropping it rather than raising ``struct.error``. Before
    downsampling, a fixed-tap low-pass prevents out-of-band content from
    folding into the speech band. This remains a lower-quality fallback than
    SoXR, but it never degenerates into unfiltered sample dropping.
    """
    # Decode PCM16 LE samples, dropping any odd trailing byte that would
    # otherwise split a 16-bit sample and crash struct.unpack.
    num_samples = len(data) // 2
    data = data[: num_samples * 2]
    samples: tuple[float, ...] | list[float] = struct.unpack(f"<{num_samples}h", data)
    if to_rate < from_rate:
        samples = _low_pass_for_downsampling(samples, from_rate, to_rate)

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
        out_samples.append(max(-32768, min(32767, round(value))))

    return struct.pack(f"<{len(out_samples)}h", *out_samples)


_FALLBACK_FILTER_TAPS = 63


@lru_cache(maxsize=32)
def _downsample_filter(from_rate: int, to_rate: int) -> tuple[float, ...]:
    """Build a normalized Hamming-windowed sinc filter for one rate pair."""
    # Leave a 10% transition band below the destination Nyquist frequency.
    cutoff = 0.45 * to_rate / from_rate
    midpoint = (_FALLBACK_FILTER_TAPS - 1) / 2
    taps: list[float] = []
    for index in range(_FALLBACK_FILTER_TAPS):
        offset = index - midpoint
        if offset == 0:
            sinc = 2 * cutoff
        else:
            sinc = math.sin(2 * math.pi * cutoff * offset) / (math.pi * offset)
        window = 0.54 - 0.46 * math.cos(2 * math.pi * index / (_FALLBACK_FILTER_TAPS - 1))
        taps.append(sinc * window)
    scale = sum(taps)
    return tuple(tap / scale for tap in taps)


def _low_pass_for_downsampling(
    samples: tuple[float, ...] | list[float],
    from_rate: int,
    to_rate: int,
) -> list[float]:
    """Apply an edge-extended anti-alias filter without numeric packages.

    Centering the symmetric FIR and extending each boundary with its nearest
    sample avoids injecting a fresh zero-padding transient whenever a caller
    supplies streamed audio in separate frames.
    """
    taps = _downsample_filter(from_rate, to_rate)
    midpoint = len(taps) // 2
    filtered: list[float] = []
    for sample_index in range(len(samples)):
        value = 0.0
        for tap_index, tap in enumerate(taps):
            source_index = sample_index + midpoint - tap_index
            source_index = max(0, min(len(samples) - 1, source_index))
            value += tap * samples[source_index]
        filtered.append(value)
    return filtered


class _StreamingLinearState:
    """Filtered linear resampling state for one fixed rate pair."""

    def __init__(self, from_rate: int, to_rate: int) -> None:
        self._from_rate = from_rate
        self._to_rate = to_rate
        self._taps = _downsample_filter(from_rate, to_rate) if to_rate < from_rate else ()
        self._raw_history: list[float] = []
        self._buffer: list[float] = []
        self._buffer_start = 0
        self._received = 0
        self._next_position = 0
        self._output_count = 0
        # Never import NumPy on the first live audio frame: a cold import can
        # add hundreds of milliseconds to time-to-first-audio. Reuse it when
        # an installed VAD/smart-turn/provider already loaded it; otherwise
        # stay on the dependency-free path.
        self._np: Any | None = sys.modules.get("numpy")

    def process(self, samples: tuple[int, ...], *, final: bool) -> bytes:
        filtered = self._filter(samples)
        self._buffer.extend(filtered)
        self._received += len(filtered)

        output: list[int] = []
        target_output_count = int(self._received * self._to_rate / self._from_rate)
        while self._output_count < target_output_count:
            index, fraction_numerator = divmod(self._next_position, self._to_rate)
            if index >= self._received:
                break
            if fraction_numerator and index + 1 >= self._received and not final:
                break

            first = self._sample(index)
            if fraction_numerator and index + 1 < self._received:
                second = self._sample(index + 1)
                fraction = fraction_numerator / self._to_rate
                value = first * (1 - fraction) + second * fraction
            else:
                value = first
            output.append(max(-32768, min(32767, round(value))))
            self._next_position += self._from_rate
            self._output_count += 1

        next_index = self._next_position // self._to_rate
        discard = min(len(self._buffer), max(0, next_index - self._buffer_start))
        if discard:
            del self._buffer[:discard]
            self._buffer_start += discard
        return struct.pack(f"<{len(output)}h", *output)

    @property
    def pending_output_bytes(self) -> int:
        """PCM16 bytes retained until the current segment is finished."""
        final_output_count = int(self._received * self._to_rate / self._from_rate)
        return max(0, final_output_count - self._output_count) * 2

    def _filter(self, samples: tuple[int, ...]) -> list[float]:
        if not self._taps:
            return [float(sample) for sample in samples]
        combined = [*self._raw_history, *samples]
        if not combined:
            return []
        history_length = len(self._raw_history)
        if self._np is not None:
            convolved = self._np.convolve(
                self._np.asarray(combined, dtype=self._np.float64),
                self._np.asarray(self._taps, dtype=self._np.float64),
                mode="full",
            )
            filtered = convolved[history_length : history_length + len(samples)].tolist()
        else:
            filtered = []
            for sample_index in range(history_length, len(combined)):
                value = 0.0
                for tap_index in range(min(len(self._taps), sample_index + 1)):
                    value += self._taps[tap_index] * combined[sample_index - tap_index]
                filtered.append(value)
        history_size = len(self._taps) - 1
        self._raw_history = combined[-history_size:] if history_size else []
        return filtered

    def _sample(self, global_index: int) -> float:
        return self._buffer[global_index - self._buffer_start]


class _StreamingSoxrState:
    """Stateful SoXR backend for one fixed rate pair."""

    def __init__(self, from_rate: int, to_rate: int) -> None:
        import numpy as np  # type: ignore[import-untyped]
        import soxr  # type: ignore[import-not-found]

        self._np = np
        self._from_rate = from_rate
        self._to_rate = to_rate
        self._received = 0
        self._output_count = 0
        self._stream = soxr.ResampleStream(
            from_rate,
            to_rate,
            1,
            dtype="float32",
            # Downsampling must remain band-limited; LQ materially reduces the
            # live filter window while still suppressing aliases. Upsampling
            # cannot fold energy into the destination band, so QQ's stateful
            # cubic interpolation avoids adding an 80 ms startup buffer to
            # common telephony 8 -> 16 kHz ingress.
            quality="QQ" if to_rate > from_rate else "LQ",
        )

    def process(self, samples: tuple[int, ...], *, final: bool) -> bytes:
        self._received += len(samples)
        values = self._np.asarray(samples, dtype=self._np.float32) / 32768.0
        output = self._stream.resample_chunk(values, last=final)
        self._output_count += len(output)
        pcm = self._np.clip(self._np.rint(output * 32768.0), -32768, 32767).astype(self._np.int16)
        return pcm.tobytes()

    @property
    def pending_output_bytes(self) -> int:
        # SoXR rounds the final sample count to the nearest sample, with .5
        # rounded up. Track it explicitly: ``delay()`` is fractional for
        # non-integral rate ratios, and ``ceil(delay)`` can over-report a
        # short stream by one sample.
        expected = (2 * self._received * self._to_rate + self._from_rate) // (2 * self._from_rate)
        return max(0, expected - self._output_count) * 2


class _StreamingScipyState:
    """Stateful polyphase FIR backend for one fixed rate pair."""

    def __init__(self, from_rate: int, to_rate: int) -> None:
        import numpy as np  # type: ignore[import-untyped]
        from scipy.signal import firwin, lfilter  # type: ignore[import-not-found]

        divisor = math.gcd(from_rate, to_rate)
        self._up = to_rate // divisor
        self._down = from_rate // divisor
        self._from_rate = from_rate
        self._to_rate = to_rate
        self._np = np
        self._lfilter = lfilter
        half_length = 10 * max(self._up, self._down)
        self._half_length = half_length
        self._taps = firwin(2 * half_length + 1, 1.0 / max(self._up, self._down)) * self._up
        self._zi = np.zeros(len(self._taps) - 1, dtype=np.float64)
        self._expanded_count = 0
        self._next_output_index = half_length
        self._received = 0
        self._output_count = 0

    def process(self, samples: tuple[int, ...], *, final: bool) -> bytes:
        values = self._np.asarray(samples, dtype=self._np.float64) / 32768.0
        self._received += len(samples)
        expanded = self._np.zeros(len(values) * self._up, dtype=self._np.float64)
        expanded[:: self._up] = values
        if final:
            expanded = self._np.concatenate(
                (
                    expanded,
                    self._np.zeros(
                        self._half_length + self._down,
                        dtype=self._np.float64,
                    ),
                )
            )
        filtered, self._zi = self._lfilter(
            self._taps,
            [1.0],
            expanded,
            zi=self._zi,
        )

        start = self._expanded_count
        stop = start + len(filtered)
        self._expanded_count = stop
        target_count = int(self._received * self._to_rate / self._from_rate)
        output: list[float] = []
        while self._next_output_index < stop and (not final or self._output_count < target_count):
            if self._next_output_index >= start:
                output.append(float(filtered[self._next_output_index - start]))
                self._output_count += 1
            self._next_output_index += self._down

        pcm = self._np.clip(
            self._np.rint(self._np.asarray(output) * 32768.0),
            -32768,
            32767,
        ).astype(self._np.int16)
        return pcm.tobytes()

    @property
    def pending_output_bytes(self) -> int:
        target_count = int(self._received * self._to_rate / self._from_rate)
        return max(0, target_count - self._output_count) * 2


def _streaming_state(from_rate: int, to_rate: int) -> Any:
    """Construct the stateful implementation for the selected quality backend."""
    backend = resample_backend()
    if backend == "soxr":
        try:
            return _StreamingSoxrState(from_rate, to_rate)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            _log_runtime_failure_once("soxr")
    elif backend == "scipy":
        try:
            return _StreamingScipyState(from_rate, to_rate)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            _log_runtime_failure_once("scipy")
    return _StreamingLinearState(from_rate, to_rate)


class PCM16StreamResampler:
    """Low-latency stateful PCM16-mono resampling to one target rate.

    Filter history, interpolation phase, and a split trailing byte survive
    calls. A source-rate change cleanly finishes the old segment before
    starting a fresh one. Call :meth:`finish` at a real stream boundary or
    :meth:`reset` when buffered output must be discarded (for example,
    barge-in cancellation).
    """

    def __init__(self, target_rate: int) -> None:
        self._target_rate = _require_positive_int("target_rate", target_rate)
        self._source_rate: int | None = None
        self._state: Any | None = None
        self._byte_carry = b""

    @property
    def target_rate(self) -> int:
        return self._target_rate

    @property
    def source_rate(self) -> int | None:
        return self._source_rate

    @property
    def pending_output_bytes(self) -> int:
        """PCM16 bytes that :meth:`finish` would currently emit."""
        if self._state is None:
            return 0
        return self._state.pending_output_bytes

    def process(self, data: bytes, source_rate: int) -> bytes:
        """Convert the next contiguous PCM16-mono chunk."""
        source_rate = _require_positive_int("source_rate", source_rate)
        prefix = b""
        if self._source_rate is not None and source_rate != self._source_rate:
            carry = self._byte_carry
            prefix = self.finish()
            if carry:
                self._byte_carry = carry
            self._source_rate = source_rate
            if source_rate != self._target_rate:
                self._state = _streaming_state(source_rate, self._target_rate)

        aligned = self._byte_carry + data
        if len(aligned) % 2:
            self._byte_carry = aligned[-1:]
            aligned = aligned[:-1]
        else:
            self._byte_carry = b""
        if not aligned:
            return prefix
        if source_rate == self._target_rate:
            return prefix + aligned

        sample_count = len(aligned) // 2
        samples = struct.unpack(f"<{sample_count}h", aligned)
        assert self._state is not None
        return prefix + self._state.process(samples, final=False)

    def finish(self) -> bytes:
        """Flush interpolation output and reset for the next stream."""
        output = b""
        if self._state is not None:
            output = self._state.process((), final=True)
        self.reset()
        return output

    def reset(self) -> None:
        """Discard pending state without emitting it."""
        self._source_rate = None
        self._state = None
        self._byte_carry = b""


def resample_chunk(chunk: AudioChunk, to_rate: int) -> AudioChunk:
    """Resample PCM16 channels independently and restore their interleaving.

    A trailing incomplete frame is discarded, matching :func:`resample`'s
    treatment of an incomplete PCM16 sample without letting bytes from one
    channel shift into another.
    """
    if chunk.format.sample_rate == to_rate:
        return chunk

    fmt = chunk.format
    validate_pcm16_format("chunk.format", fmt)
    channels = fmt.channels
    frame_size = channels * fmt.sample_width
    frame_count = len(chunk.data) // frame_size
    aligned_data = chunk.data[: frame_count * frame_size]

    if channels == 1:
        new_data = resample(aligned_data, fmt.sample_rate, to_rate)
    else:
        total_samples = frame_count * channels
        samples = struct.unpack(f"<{total_samples}h", aligned_data)
        resampled_channels = [
            resample(
                struct.pack(f"<{frame_count}h", *samples[channel_index::channels]),
                fmt.sample_rate,
                to_rate,
            )
            for channel_index in range(channels)
        ]

        output_lengths = {len(channel_data) for channel_data in resampled_channels}
        if len(output_lengths) != 1:
            raise RuntimeError("resampling produced different output lengths across channels")
        channel_bytes = output_lengths.pop() if output_lengths else 0
        output_frames = channel_bytes // fmt.sample_width
        per_channel = [
            struct.unpack(f"<{output_frames}h", channel[: output_frames * fmt.sample_width])
            for channel in resampled_channels
        ]
        new_data = struct.pack(
            f"<{output_frames * channels}h",
            *(sample for frame in zip(*per_channel, strict=True) for sample in frame),
        )

    new_format = AudioFormat(
        sample_rate=to_rate,
        channels=fmt.channels,
        sample_width=fmt.sample_width,
        encoding=fmt.encoding,
    )
    return replace(chunk, data=new_data, format=new_format)


def to_mono(data: bytes, channels: int) -> bytes:
    """Downmix multi-channel PCM16 audio to mono by averaging channels."""
    channels = _require_positive_int("channels", channels)
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
        avg = round(sum(frame_samples) / channels)
        mono_samples.append(max(-32768, min(32767, avg)))

    return struct.pack(f"<{len(mono_samples)}h", *mono_samples)


class AudioFrameAligner:
    """Carry incomplete source frames across streaming audio chunks.

    A transport can split interleaved PCM frames at arbitrary byte offsets.
    Downmixing either half independently loses or cross-pairs channel samples,
    so consumers that normalize multi-channel streaming audio must align the
    source geometry first. A format change deliberately drops an incomplete
    old frame rather than combining it with different audio geometry.
    """

    def __init__(self) -> None:
        self._carry = b""
        self._carry_format: AudioFormat | None = None

    def align(self, chunk: AudioChunk) -> AudioChunk:
        """Return ``chunk`` with only complete source frames.

        Any trailing partial frame is retained for the next same-format
        chunk. Call :meth:`reset` at a stream boundary when it must be
        discarded instead.
        """
        if self._carry_format != chunk.format:
            self.reset()

        data = self._carry + chunk.data
        remainder = len(data) % chunk.format.frame_size
        if remainder:
            self._carry = data[-remainder:]
            self._carry_format = chunk.format
            data = data[:-remainder]
        else:
            self.reset()
        return replace(chunk, data=data)

    def reset(self) -> None:
        """Discard an incomplete frame at an explicit stream boundary."""
        self._carry = b""
        self._carry_format = None


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
    return replace(chunk, data=new_data, format=new_format)


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
