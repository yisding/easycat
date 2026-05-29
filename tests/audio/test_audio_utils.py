"""Tests for audio utilities: resampling, mono downmix, chunk sizing."""

import struct

import pytest

from easycat._audio_utils import (
    chunk_frames,
    resample,
    resample_chunk,
    to_mono,
    to_mono_chunk,
)
from easycat.audio_format import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    PCM16_MONO_48K,
    AudioChunk,
    AudioFormat,
)

# ── Resampling tests ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_resample_backend_state():
    """Reset the module-global resample backend cache and per-backend
    log-once tracking before each test so resample tests are
    order-independent (no cross-test coupling via process-wide globals)."""
    import easycat._audio_utils as au

    saved_backend = au._resolved_backend
    saved_logged = set(au._logged_runtime_failure)
    au._resolved_backend = None
    au._logged_runtime_failure.clear()
    try:
        yield
    finally:
        au._resolved_backend = saved_backend
        au._logged_runtime_failure.clear()
        au._logged_runtime_failure.update(saved_logged)


def test_resample_same_rate_noop():
    data = struct.pack("<4h", 100, 200, 300, 400)
    result = resample(data, 16000, 16000)
    assert result == data


def test_resample_8k_to_16k_doubles_samples():
    data = struct.pack("<4h", 0, 1000, 2000, 3000)
    result = resample(data, 8000, 16000)
    num_out = len(result) // 2
    assert num_out == 8


def test_resample_16k_to_8k_halves_samples():
    data = struct.pack("<8h", 0, 500, 1000, 1500, 2000, 2500, 3000, 3500)
    result = resample(data, 16000, 8000)
    num_out = len(result) // 2
    assert num_out == 4


def test_resample_preserves_dc_signal():
    # FIR-based resamplers (soxr, scipy.resample_poly) ring at the
    # boundaries; only the steady-state body should preserve DC to
    # ±1 LSB.  Use a long input and trim the settling region from
    # each end before comparing.
    value = 1234
    n_input = 2048
    data = struct.pack(f"<{n_input}h", *([value] * n_input))
    result = resample(data, 8000, 16000)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    trim = (len(samples) * 15) // 100
    body = samples[trim : len(samples) - trim]
    for s in body:
        assert abs(s - value) <= 1


def test_resample_chunk_updates_format():
    data = struct.pack("<4h", 100, 200, 300, 400)
    chunk = AudioChunk(data=data, format=PCM16_MONO_8K)
    result = resample_chunk(chunk, 16000)
    assert result.format.sample_rate == 16000
    assert result.format.channels == 1
    assert len(result.data) > len(chunk.data)


def test_resample_chunk_same_rate_returns_same():
    chunk = AudioChunk(data=b"\x00\x00\x00\x00", format=PCM16_MONO_16K)
    result = resample_chunk(chunk, 16000)
    assert result is chunk


# ── Extended resampling: 24k and 48k rates ────────────────────────


RATE_PAIRS = [
    (8000, 24000),
    (8000, 48000),
    (16000, 24000),
    (16000, 48000),
    (24000, 8000),
    (24000, 16000),
    (24000, 48000),
    (48000, 8000),
    (48000, 16000),
    (48000, 24000),
]


@pytest.mark.parametrize("from_rate,to_rate", RATE_PAIRS)
def test_resample_rate_pairs_sample_count(from_rate: int, to_rate: int):
    """Verify output sample count is correct for all supported rate pairs."""
    n_input = 480  # enough for any rate
    data = struct.pack(f"<{n_input}h", *([500] * n_input))
    result = resample(data, from_rate, to_rate)
    n_output = len(result) // 2
    expected = int(n_input * to_rate / from_rate)
    assert n_output == expected


@pytest.mark.parametrize("from_rate,to_rate", RATE_PAIRS)
def test_resample_rate_pairs_dc_preservation(from_rate: int, to_rate: int):
    """DC signal should be preserved across all rate pairs in the
    steady-state body.  Boundary samples ring (FIR edge artifact)
    and are excluded by trimming 15% from each end."""
    value = 2000
    n_input = 2048
    data = struct.pack(f"<{n_input}h", *([value] * n_input))
    result = resample(data, from_rate, to_rate)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    trim = (len(samples) * 15) // 100
    body = samples[trim : len(samples) - trim]
    for s in body:
        assert abs(s - value) <= 1


# ── Odd-length chunk handling (split 16-bit sample) ───────────────


def test_resample_odd_length_does_not_crash():
    """An odd byte count (a sample split across a streaming chunk) must not
    raise struct.error; the trailing byte is dropped instead."""
    result = resample(b"\x01\x02\x03", 16000, 24000)
    assert isinstance(result, bytes)


def test_resample_single_byte_returns_empty():
    """A lone byte carries no complete sample and resamples to nothing."""
    assert resample(b"\x01", 16000, 24000) == b""


def test_resample_odd_length_linear_fallback_does_not_crash(monkeypatch):
    """The pure-Python (no numpy/soxr/scipy) path must also tolerate an odd
    trailing byte rather than raising struct.error."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    result = au.resample(b"\x01\x02\x03\x04\x05", 16000, 8000)
    assert isinstance(result, bytes)


def test_resample_runtime_failure_retries_high_quality_backend(monkeypatch, caplog):
    """A transient soxr failure must fall back to linear for that chunk only
    and retry soxr on the next chunk, rather than permanently pinning the whole
    process to the linear resampler (the original one-strike-out bug)."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "soxr")

    calls: list[bytes] = []

    def flaky_soxr(data, from_rate, to_rate):
        calls.append(data)
        if len(calls) == 1:
            raise RuntimeError("transient native-lib hiccup")
        return b"\x00\x00" * 4

    monkeypatch.setattr(au, "_resample_soxr_impl", flaky_soxr, raising=False)

    payload = struct.pack("<4h", 0, 1000, 2000, 3000)

    # First call: soxr raises -> falls back to linear, but backend stays "soxr".
    with caplog.at_level("WARNING"):
        first = au.resample(payload, 8000, 16000)
    assert isinstance(first, bytes)
    assert au._resolved_backend == "soxr"
    assert len(calls) == 1
    assert sum("soxr resampling failed" in r.message for r in caplog.records) == 1

    # Second call: soxr is retried (not permanently downgraded to linear).
    second = au.resample(payload, 8000, 16000)
    assert isinstance(second, bytes)
    assert len(calls) == 2


def test_resample_runtime_failure_logs_once(monkeypatch, caplog):
    """Repeated soxr failures should warn only once to avoid per-chunk log
    spam, while still attempting the high-quality backend each time."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "soxr")

    def always_fail(data, from_rate, to_rate):
        raise RuntimeError("boom")

    monkeypatch.setattr(au, "_resample_soxr_impl", always_fail, raising=False)

    payload = struct.pack("<4h", 0, 1000, 2000, 3000)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            au.resample(payload, 8000, 16000)

    warnings = [r for r in caplog.records if "soxr resampling failed" in r.message]
    assert len(warnings) == 1


def test_resample_chunk_to_48k():
    data = struct.pack("<100h", *([1000] * 100))
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    result = resample_chunk(chunk, 48000)
    assert result.format.sample_rate == 48000
    assert result.format == PCM16_MONO_48K


def test_resample_chunk_to_24k():
    data = struct.pack("<100h", *([1000] * 100))
    chunk = AudioChunk(data=data, format=PCM16_MONO_8K)
    result = resample_chunk(chunk, 24000)
    assert result.format.sample_rate == 24000
    assert result.format == PCM16_MONO_24K


# ── Mono downmix tests ────────────────────────────────────────────


def test_to_mono_already_mono():
    data = struct.pack("<4h", 100, 200, 300, 400)
    result = to_mono(data, channels=1)
    assert result == data


def test_to_mono_stereo():
    data = struct.pack("<4h", 100, 300, 200, 400)
    result = to_mono(data, channels=2)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    assert samples == (200, 300)


def test_to_mono_stereo_symmetric():
    data = struct.pack("<4h", 500, 500, -1000, -1000)
    result = to_mono(data, channels=2)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    assert samples == (500, -1000)


def test_to_mono_chunk_returns_mono_format():
    stereo_fmt = AudioFormat(sample_rate=16000, channels=2, sample_width=2)
    data = struct.pack("<4h", 100, 200, 300, 400)
    chunk = AudioChunk(data=data, format=stereo_fmt)
    result = to_mono_chunk(chunk)
    assert result.format.channels == 1
    assert result.format.sample_rate == 16000


def test_to_mono_chunk_already_mono():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    result = to_mono_chunk(chunk)
    assert result is chunk


# ── Chunk sizing tests ────────────────────────────────────────────


def test_chunk_frames_10ms_at_16k():
    audio = bytes(640)
    frames = list(chunk_frames(audio, frame_duration_ms=10, sample_rate=16000))
    assert len(frames) == 2
    assert all(len(f) == 320 for f in frames)


def test_chunk_frames_20ms_at_16k():
    audio = bytes(1280)
    frames = list(chunk_frames(audio, frame_duration_ms=20, sample_rate=16000))
    assert len(frames) == 2
    assert all(len(f) == 640 for f in frames)


def test_chunk_frames_30ms_at_16k():
    audio = bytes(960)
    frames = list(chunk_frames(audio, frame_duration_ms=30, sample_rate=16000))
    assert len(frames) == 1
    assert len(frames[0]) == 960


def test_chunk_frames_partial_final_frame():
    audio = bytes(500)
    frames = list(chunk_frames(audio, frame_duration_ms=10, sample_rate=16000))
    assert len(frames) == 2
    assert len(frames[0]) == 320
    assert len(frames[1]) == 180


def test_chunk_frames_8k_10ms():
    audio = bytes(480)
    frames = list(chunk_frames(audio, frame_duration_ms=10, sample_rate=8000))
    assert len(frames) == 3
    assert all(len(f) == 160 for f in frames)


def test_chunk_frames_empty_audio():
    frames = list(chunk_frames(b"", frame_duration_ms=10, sample_rate=16000))
    assert frames == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_duration_ms": 0, "sample_rate": 16000},
        {"frame_duration_ms": -10, "sample_rate": 16000},
        {"frame_duration_ms": 10, "sample_rate": 0},
        {"frame_duration_ms": 10, "sample_rate": -16000},
        {"frame_duration_ms": 10, "sample_rate": 16000, "sample_width": 0},
        {"frame_duration_ms": 10, "sample_rate": 16000, "channels": 0},
        {"frame_duration_ms": 1, "sample_rate": 999},
    ],
)
def test_chunk_frames_rejects_non_positive_or_zero_byte_frames(kwargs):
    with pytest.raises(ValueError):
        list(chunk_frames(b"\x00" * 10, **kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_duration_ms": True, "sample_rate": 16000},
        {"frame_duration_ms": 10.0, "sample_rate": 16000},
        {"frame_duration_ms": 10, "sample_rate": True},
        {"frame_duration_ms": 10, "sample_rate": "16000"},
        {"frame_duration_ms": 10, "sample_rate": 16000, "sample_width": 2.0},
        {"frame_duration_ms": 10, "sample_rate": 16000, "channels": False},
    ],
)
def test_chunk_frames_rejects_non_integer_parameters(kwargs):
    with pytest.raises(TypeError):
        list(chunk_frames(b"\x00" * 10, **kwargs))
