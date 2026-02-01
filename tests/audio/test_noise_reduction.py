"""Noise reduction tests: RNNoise, Krisp, factory, and helpers."""

import struct
from unittest.mock import MagicMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.noise_reduction import (
    KrispNoiseReducer,
    NoiseReducerConfig,
    PassthroughNoiseReducer,
    RNNoiseReducer,
    _float32_to_pcm16,
    _pcm16_to_float32,
    create_noise_reducer,
)

# ── PCM16 <-> float32 conversion tests ──────────────────────────────


def test_pcm16_to_float32_silence():
    """Silence (all zeros) should convert to all 0.0."""
    data = b"\x00\x00" * 10
    result = _pcm16_to_float32(data)
    assert len(result) == 10
    assert all(s == 0.0 for s in result)


def test_pcm16_to_float32_max_positive():
    """Max int16 (32767) should convert to ~1.0."""
    data = struct.pack("<h", 32767)
    result = _pcm16_to_float32(data)
    assert len(result) == 1
    assert abs(result[0] - (32767 / 32768.0)) < 0.001


def test_pcm16_to_float32_max_negative():
    """Min int16 (-32768) should convert to -1.0."""
    data = struct.pack("<h", -32768)
    result = _pcm16_to_float32(data)
    assert len(result) == 1
    assert result[0] == -1.0


def test_float32_to_pcm16_roundtrip():
    """PCM16 -> float32 -> PCM16 should preserve values."""
    original = [0, 1000, -1000, 32767, -32768]
    data = struct.pack(f"<{len(original)}h", *original)
    floats = _pcm16_to_float32(data)
    roundtrip = _float32_to_pcm16(floats)
    recovered = list(struct.unpack(f"<{len(original)}h", roundtrip))
    # Allow +-1 for rounding
    for orig, rec in zip(original, recovered):
        assert abs(orig - rec) <= 1, f"Expected {orig}, got {rec}"


def test_float32_to_pcm16_clipping():
    """Values outside [-1, 1] should clip to int16 range."""
    samples = [2.0, -2.0]
    result = _float32_to_pcm16(samples)
    values = struct.unpack("<2h", result)
    assert values[0] == 32767
    assert values[1] == -32768


# ── RNNoiseReducer tests ────────────────────────────────────────────


def test_rnnoise_fails_without_library():
    """RNNoiseReducer should raise RuntimeError if library is missing."""
    with pytest.raises(RuntimeError, match="RNNoise"):
        RNNoiseReducer()


@pytest.mark.asyncio
async def test_rnnoise_process_mocked():
    """RNNoiseReducer.process with mocked C library calls."""
    mock_lib = MagicMock()
    mock_state = MagicMock()
    mock_lib.rnnoise_create.return_value = mock_state

    # Mock process_frame to copy input to output (passthrough)
    def mock_process_frame(state, out_buf, in_buf):
        for i in range(480):
            out_buf[i] = in_buf[i]

    mock_lib.rnnoise_process_frame.side_effect = mock_process_frame

    with (
        patch("ctypes.util.find_library", return_value="/fake/librnnoise.so"),
        patch("ctypes.CDLL", return_value=mock_lib),
    ):
        reducer = RNNoiseReducer()

    # Create a 16 kHz chunk (320 samples = 20ms)
    samples = [100] * 320
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)

    result = await reducer.process(chunk)

    # Output should be at original sample rate
    assert result.format.sample_rate == 16000
    assert len(result.data) > 0
    # RNNoise should have been called
    assert mock_lib.rnnoise_process_frame.called


def test_rnnoise_sets_ctypes_argtypes():
    """RNNoiseReducer must set restype/argtypes for 64-bit pointer safety."""
    import ctypes

    mock_lib = MagicMock()
    mock_lib.rnnoise_create.return_value = 0xDEADBEEF

    with (
        patch("ctypes.util.find_library", return_value="/fake/librnnoise.so"),
        patch("ctypes.CDLL", return_value=mock_lib),
    ):
        RNNoiseReducer()

    # rnnoise_create must return c_void_p (pointer), not the default c_int
    assert mock_lib.rnnoise_create.restype == ctypes.c_void_p
    # rnnoise_destroy must accept a pointer and return nothing
    assert mock_lib.rnnoise_destroy.restype is None
    assert mock_lib.rnnoise_destroy.argtypes == [ctypes.c_void_p]
    # rnnoise_process_frame must accept pointer + two float pointers
    assert mock_lib.rnnoise_process_frame.restype == ctypes.c_float
    assert mock_lib.rnnoise_process_frame.argtypes == [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]


# ── KrispNoiseReducer tests ─────────────────────────────────────────


def test_krisp_fails_without_sdk():
    """KrispNoiseReducer should raise RuntimeError if SDK is missing."""
    with pytest.raises(RuntimeError, match="Krisp"):
        KrispNoiseReducer()


@pytest.mark.asyncio
async def test_krisp_process_mocked():
    """KrispNoiseReducer.process with mocked SDK."""
    mock_module = MagicMock()
    mock_session = MagicMock()
    mock_module.create_noise_cancellation_session.return_value = mock_session

    data = b"\x00\x00" * 160
    mock_module.process_frame.return_value = data

    import sys

    sys.modules["krisp_audio"] = mock_module
    try:
        reducer = KrispNoiseReducer()
        chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
        result = await reducer.process(chunk)

        assert result.format == PCM16_MONO_16K
        assert result.data == data
        mock_module.process_frame.assert_called_once()
    finally:
        del sys.modules["krisp_audio"]


# ── PassthroughNoiseReducer tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_passthrough_returns_unchanged():
    """PassthroughNoiseReducer should return the chunk unchanged."""
    reducer = PassthroughNoiseReducer()
    chunk = AudioChunk(data=b"\x01\x02" * 80, format=PCM16_MONO_16K)
    result = await reducer.process(chunk)
    assert result is chunk


# ── Factory tests ────────────────────────────────────────────────────


def test_factory_auto_falls_back_to_passthrough():
    """In auto mode with no SDKs available, factory returns passthrough."""
    reducer = create_noise_reducer(NoiseReducerConfig(backend="auto"))
    assert isinstance(reducer, PassthroughNoiseReducer)


def test_factory_explicit_krisp_fails():
    """Explicitly requesting krisp without SDK should raise."""
    with pytest.raises(RuntimeError, match="Krisp"):
        create_noise_reducer(NoiseReducerConfig(backend="krisp"))


def test_factory_explicit_rnnoise_fails():
    """Explicitly requesting rnnoise without library should raise."""
    with pytest.raises(RuntimeError, match="RNNoise"):
        create_noise_reducer(NoiseReducerConfig(backend="rnnoise"))


@pytest.mark.asyncio
async def test_factory_auto_passthrough_processes_audio():
    """Factory auto -> passthrough should still process audio."""
    reducer = create_noise_reducer()
    chunk = AudioChunk(data=b"\x00\x00" * 160, format=PCM16_MONO_16K)
    result = await reducer.process(chunk)
    assert result.data == chunk.data


def test_factory_krisp_preferred_in_auto():
    """In auto mode, Krisp should be tried first."""
    mock_module = MagicMock()
    mock_module.create_noise_cancellation_session.return_value = MagicMock()

    import sys

    sys.modules["krisp_audio"] = mock_module
    try:
        reducer = create_noise_reducer(NoiseReducerConfig(backend="auto"))
        assert isinstance(reducer, KrispNoiseReducer)
    finally:
        del sys.modules["krisp_audio"]


# ── Resample round-trip test ─────────────────────────────────────────


def test_resample_roundtrip_quality():
    """Resample 16k -> 48k -> 16k should approximately preserve audio."""
    from easycat.audio_utils import resample

    # Create a simple tone-like pattern
    samples = [int(1000 * (i % 10) / 10) for i in range(160)]
    data_16k = struct.pack(f"<{len(samples)}h", *samples)

    # Resample up to 48k
    data_48k = resample(data_16k, 16000, 48000)
    assert len(data_48k) > len(data_16k)

    # Resample back to 16k
    data_back = resample(data_48k, 48000, 16000)

    # Should be approximately the same length
    orig_samples = len(data_16k) // 2
    back_samples = len(data_back) // 2
    assert abs(orig_samples - back_samples) <= 1

    # Values should be close (linear interpolation introduces some error)
    orig = list(struct.unpack(f"<{orig_samples}h", data_16k))
    n = min(orig_samples, back_samples)
    back = list(struct.unpack(f"<{n}h", data_back[: n * 2]))
    errors = [abs(a - b) for a, b in zip(orig[:n], back)]
    avg_error = sum(errors) / len(errors)
    # Average error should be small relative to signal
    assert avg_error < 200, f"Average round-trip error too high: {avg_error}"
