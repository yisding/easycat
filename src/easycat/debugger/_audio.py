"""PCM/WAV audio coercion helpers for the debugger.

Pure, aiohttp-free audio plumbing split out of
:mod:`easycat.debugger.server` (QS3): numpy/audioop PCM downmix + resample
fallbacks, the bounded frame-format reconciliation used to stitch a turn's
audio, the RIFF/WAVE header builder, the in-memory concat helper, and the
JSON-safe :class:`ReplayFrame` projection.

DEPENDENCY NOTE: ``audioop`` was removed from the stdlib in Python 3.13 and
``numpy`` is an optional extra; both are resolved lazily with fallbacks so a
bare (``aiohttp``-only) debugger install still imports this module.
"""

from __future__ import annotations

import struct
from typing import Any

from easycat.debug._pcm import is_supported_width as _is_supported_width

# ``audioop`` was removed from the stdlib in Python 3.13.  Fall back to numpy
# (optional extra) for mic-track resample/downmix; skip mismatched frames only
# when neither helper is available.
try:
    import audioop as _audioop
except ImportError:  # pragma: no cover - exercised on 3.13+
    _audioop = None  # type: ignore[assignment]

try:
    import numpy as _np
except (ImportError, RecursionError):  # pragma: no cover
    _np = None  # type: ignore[assignment]

# Accepted PCM geometry for audio concat/waveform routes.  Debug bundles are
# treated as untrusted input, so journal-provided format metadata must be kept
# within normal voice-audio bounds before it is used in WAV headers or passed to
# audioop's resampler.
_AUDIO_DEFAULT_FMT = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
_AUDIO_MIN_SAMPLE_RATE = 8000
_AUDIO_MAX_SAMPLE_RATE = 192000
_AUDIO_VALID_CHANNELS = frozenset({1, 2})
_AUDIO_MAX_RESAMPLE_RATIO = 4
_AUDIO_MAX_CONVERTED_FRAME_BYTES = 5 * 1024 * 1024


def _np_pcm_dtype(width: int) -> Any:
    """Return the signed little-endian numpy dtype for raw PCM samples."""
    _dtypes = {1: "int8", 2: "<i2", 4: "<i4"}
    spec = _dtypes.get(width)
    if spec is None:
        raise ValueError(f"unsupported sample width {width}")
    return _np.dtype(spec)  # type: ignore[union-attr]


def _np_tomono(data: bytes, width: int) -> bytes:
    """Average stereo channels into mono using numpy (int8/16/32 PCM)."""
    dt = _np_pcm_dtype(width)
    arr = _np.frombuffer(data, dtype=dt)  # type: ignore[union-attr]
    stereo = arr.reshape(-1, 2).astype(_np.int64)  # type: ignore[union-attr]
    mono = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(dt)
    return mono.tobytes()


def _np_ratecv(data: bytes, width: int, nchannels: int, inrate: int, outrate: int) -> bytes:
    """Linearly interpolate PCM from *inrate* to *outrate* using numpy."""
    dt = _np_pcm_dtype(width)
    arr = _np.frombuffer(data, dtype=dt).astype(_np.float64)  # type: ignore[union-attr]
    n_frames = len(arr) // nchannels
    if n_frames == 0:
        return b""
    n_out = max(1, round(n_frames * outrate / inrate))
    out_bytes = n_out * nchannels * width
    if out_bytes > _AUDIO_MAX_CONVERTED_FRAME_BYTES:
        raise ValueError("resampled audio frame exceeds debugger size limit")
    x_in = _np.arange(n_frames)  # type: ignore[union-attr]
    x_out = _np.linspace(0, n_frames - 1, n_out)  # type: ignore[union-attr]
    if nchannels == 1:
        out = _np.interp(x_out, x_in, arr)  # type: ignore[union-attr]
    else:
        frames = arr.reshape(n_frames, nchannels)
        out = _np.column_stack(  # type: ignore[union-attr]
            [_np.interp(x_out, x_in, frames[:, ch]) for ch in range(nchannels)]  # type: ignore[union-attr]
        ).ravel()
    info = _np.iinfo(dt)  # type: ignore[union-attr]
    return _np.clip(out, info.min, info.max).astype(dt).tobytes()  # type: ignore[union-attr]


def _project_converted_pcm_bytes(
    data: bytes,
    *,
    width: int,
    channels: int,
    target_channels: int,
    rate: int,
    target_rate: int,
) -> int:
    frames = len(data) // (width * channels)
    if frames <= 0:
        return 0
    output_frames = frames
    if rate > 0 and rate != target_rate:
        output_frames = max(1, round(frames * target_rate / rate))
    return output_frames * target_channels * width


def _serialize_frame(frame: Any) -> dict[str, Any]:
    """Project a :class:`ReplayFrame` into JSON-safe shape for the wire.

    The raw frame carries ``input_blob`` / ``output_blob`` as ``bytes``,
    which can't go through ``json.dumps``.  We strip the bytes and expose
    the SHA-256 refs instead — the UI fetches blobs on demand from
    ``/api/artifact/{ref}``.  Sizes are surfaced separately so the UI can
    show a badge without paying the round-trip.
    """
    return {
        "sequence": frame.sequence,
        "stage": frame.stage,
        "kind": frame.kind,
        "name": frame.name,
        "turn_id": frame.turn_id,
        "data": frame.data,
        "input_ref": frame.input_ref,
        "output_ref": frame.output_ref,
        "input_blob_size": len(frame.input_blob) if frame.input_blob else 0,
        "output_blob_size": len(frame.output_blob) if frame.output_blob else 0,
        "error": frame.error,
        "side_effecting": frame.side_effecting,
    }


def _audio_metadata_int(data: dict[str, Any], key: str, default: int = 0) -> int:
    """Parse one integer PCM metadata field from untrusted journal data."""
    try:
        return int(data.get(key) or default)
    except (TypeError, ValueError, OverflowError):
        return default


def _is_safe_audio_format(fmt: dict[str, int]) -> bool:
    """Return true for bounded linear PCM geometry the debugger will process."""
    rate = fmt.get("sample_rate", 0)
    channels = fmt.get("channels", 0)
    width = fmt.get("sample_width", 0)
    return (
        _AUDIO_MIN_SAMPLE_RATE <= rate <= _AUDIO_MAX_SAMPLE_RATE
        and channels in _AUDIO_VALID_CHANNELS
        and _is_supported_width(width)
    )


def _safe_audio_format_from_metadata(data: dict[str, Any]) -> dict[str, int]:
    """Read a WAV/PCM format from journal metadata with bounded geometry.

    The resampling/header-sensitive fields (``sample_rate`` and ``channels``)
    are clamped to safe defaults whenever the untrusted journal value is out of
    range, so a hostile bundle can never drive the resampler or WAV header out
    of bounds.  ``sample_width`` is *preserved* as-is — even when it is an
    unsupported width such as 8-bit mu-law telephony (``sample_width == 1``).
    Rewriting it to the 16-bit default here would mask a genuinely unsupported
    capture, so the route handlers would drop the only blobs and return 404/409
    instead of the explicit 415 "unsupported format" response.  Preserving the
    width lets ``_is_supported_width`` at the route layer surface that 415.
    """
    rate = _audio_metadata_int(data, "sample_rate", _AUDIO_DEFAULT_FMT["sample_rate"])
    channels = _audio_metadata_int(data, "channels", _AUDIO_DEFAULT_FMT["channels"])
    width = _audio_metadata_int(data, "sample_width", _AUDIO_DEFAULT_FMT["sample_width"])
    if not (_AUDIO_MIN_SAMPLE_RATE <= rate <= _AUDIO_MAX_SAMPLE_RATE):
        rate = _AUDIO_DEFAULT_FMT["sample_rate"]
    if channels not in _AUDIO_VALID_CHANNELS:
        channels = _AUDIO_DEFAULT_FMT["channels"]
    if width <= 0:
        width = _AUDIO_DEFAULT_FMT["sample_width"]
    return {"sample_rate": rate, "channels": channels, "sample_width": width}


def _apply_pcm_conversion(
    blob: bytes,
    *,
    rate: int,
    channels: int,
    width: int,
    target_rate: int,
    target_channels: int,
) -> bytes | None:
    """Downmix/resample validated PCM; ``None`` on a too-large ratio or bad PCM."""
    try:
        converted = blob
        if channels == 2 and target_channels == 1:
            if _audioop is not None:
                converted = _audioop.tomono(converted, width, 0.5, 0.5)
            else:
                converted = _np_tomono(converted, width)
        if rate != target_rate:
            if target_rate / rate > _AUDIO_MAX_RESAMPLE_RATIO:
                return None
            if _audioop is not None:
                converted, _ = _audioop.ratecv(
                    converted, width, target_channels, rate, target_rate, None
                )
            else:
                converted = _np_ratecv(converted, width, target_channels, rate, target_rate)
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        # audio helpers reject malformed PCM lengths; never abort the turn.
        return None
    return converted


def _convert_mic_frame(
    blob: bytes,
    *,
    rate: int,
    channels: int,
    width: int,
    target_rate: int,
    target_channels: int,
    target_width: int,
) -> bytes | None:
    """Best-effort convert a mismatched mic frame to the target PCM geometry.

    Returns the converted bytes, or ``None`` when the frame should be dropped
    (unsafe geometry, missing helper, unsupported channel change, or malformed
    PCM).  Raises :class:`ValueError` when a conversion would exceed the
    debugger's converted-frame size limit.
    """
    # Debug bundles are untrusted, so both the source and target geometry must be
    # within bounded voice-audio limits before we hand them to a resampler;
    # otherwise drop the blob rather than corrupt the stream or trigger a runaway
    # conversion.
    source_fmt = {"sample_rate": rate, "channels": channels, "sample_width": width}
    target_fmt = {
        "sample_rate": target_rate,
        "channels": target_channels,
        "sample_width": target_width,
    }
    if (
        (_audioop is None and _np is None)
        or not _is_safe_audio_format(source_fmt)
        or not _is_safe_audio_format(target_fmt)
        or width != target_width
    ):
        return None
    if channels == 2 and target_channels == 1:
        projected_target_channels = 1
    elif channels == target_channels:
        projected_target_channels = target_channels
    else:
        return None
    projected_bytes = _project_converted_pcm_bytes(
        blob,
        width=width,
        channels=channels,
        target_channels=projected_target_channels,
        rate=rate,
        target_rate=target_rate,
    )
    if projected_bytes > _AUDIO_MAX_CONVERTED_FRAME_BYTES:
        raise ValueError("resampled audio frame exceeds debugger size limit")
    return _apply_pcm_conversion(
        blob,
        rate=rate,
        channels=channels,
        width=width,
        target_rate=target_rate,
        target_channels=target_channels,
    )


def _coerce_frames_to_format(
    frames: list[tuple[int, bytes, dict[str, Any]]],
    fmt: dict[str, int],
    *,
    strict: bool,
) -> tuple[list[bytes], int]:
    """Reconcile *frames* to a single PCM *format*, returning ``(blobs, dropped)``.

    Every frame whose ``sample_rate``/``channels``/``sample_width`` already
    matches *fmt* passes through untouched.  For a mismatch:

    - ``strict=True`` (TTS) raises :class:`ValueError` — the bot's own output
      should never splice across formats, and the route maps this to a 409.
    - ``strict=False`` (mic) makes a best-effort conversion with
      :mod:`audioop` (stdlib, removed in 3.13) or :mod:`numpy` (optional
      extra) when ``sample_width`` matches, and otherwise *skips* the blob
      (incrementing the dropped counter) so a noisy caller capture never
      aborts the whole turn.  When neither helper is available any mismatch
      is skipped.
    """
    blobs: list[bytes] = []
    dropped = 0
    target_rate = fmt["sample_rate"]
    target_channels = fmt["channels"]
    target_width = fmt["sample_width"]
    for _seq, blob, data in frames:
        rate = _audio_metadata_int(data, "sample_rate")
        channels = _audio_metadata_int(data, "channels")
        width = _audio_metadata_int(data, "sample_width")
        if rate == target_rate and channels == target_channels and width == target_width:
            blobs.append(blob)
            continue
        if strict:
            raise ValueError(
                "tts_frame format mismatch: cannot stitch frames with differing "
                "sample_rate/channels/sample_width"
            )
        # Non-strict (mic): convert when sample widths match and at least one
        # audio helper (audioop/numpy) is present, otherwise drop the blob rather
        # than corrupt the stream.
        converted = _convert_mic_frame(
            blob,
            rate=rate,
            channels=channels,
            width=width,
            target_rate=target_rate,
            target_channels=target_channels,
            target_width=target_width,
        )
        if converted is None:
            dropped += 1
            continue
        blobs.append(converted)
    return blobs, dropped


def _wav_header(*, sample_rate: int, channels: int, sample_width: int, data_size: int) -> bytes:
    """Build a 44-byte RIFF/WAVE PCM header.

    Used by both the streaming HTTP route and the in-memory helper that
    """
    bits_per_sample = sample_width * 8
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_size),
            b"WAVE",
            b"fmt ",
            struct.pack(
                "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample
            ),
            b"data",
            struct.pack("<I", data_size),
        ]
    )
