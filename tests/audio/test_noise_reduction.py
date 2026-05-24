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
    create_noise_reducer,
)

# ── RNNoiseReducer tests ────────────────────────────────────────────


def test_rnnoise_fails_without_library():
    """RNNoiseReducer should raise RuntimeError if pyrnnoise is missing."""
    with patch(
        "easycat.noise_reduction.require_module", side_effect=ImportError("RNNoise unavailable")
    ):
        with pytest.raises(RuntimeError, match="RNNoise"):
            RNNoiseReducer()


@pytest.mark.asyncio
async def test_rnnoise_process_mocked():
    """RNNoiseReducer.process with mocked pyrnnoise bindings."""
    pytest.importorskip("numpy")

    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480
    mock_rnnoise.create.return_value = MagicMock()

    def mock_process_mono_frame(state, frame):
        return frame, 0.0

    mock_rnnoise.process_mono_frame.side_effect = mock_process_mono_frame

    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
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
    assert mock_rnnoise.process_mono_frame.called


def test_rnnoise_uses_pyrnnoise_state_lifecycle():
    """RNNoiseReducer should create and destroy pyrnnoise state."""
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480
    mock_state = MagicMock()
    mock_rnnoise.create.return_value = mock_state

    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()
        reducer.close()

    mock_rnnoise.create.assert_called_once()
    mock_rnnoise.destroy.assert_called_once_with(mock_state)


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


def test_noise_reducer_config_rejects_unknown_backend():
    """NoiseReducerConfig should reject typo backend strings before probing dependencies."""
    with pytest.raises(ValueError, match="Unknown noise reducer backend 'rnnoize'"):
        NoiseReducerConfig(backend="rnnoize")


def test_noise_reducer_factory_revalidates_mutated_backend():
    """Factory should reject configs mutated after dataclass construction."""
    config = NoiseReducerConfig()
    config.backend = "rnnoize"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="Unknown noise reducer backend 'rnnoize'"):
        create_noise_reducer(config)


def test_factory_auto_falls_back_to_passthrough():
    """In auto mode with no SDKs available, factory returns passthrough."""
    with patch(
        "easycat.noise_reduction.require_module", side_effect=ImportError("RNNoise unavailable")
    ):
        reducer = create_noise_reducer(NoiseReducerConfig(backend="auto"))
        assert isinstance(reducer, PassthroughNoiseReducer)


def test_factory_explicit_krisp_fails():
    """Explicitly requesting krisp without SDK should raise."""
    with pytest.raises(RuntimeError, match="Krisp"):
        create_noise_reducer(NoiseReducerConfig(backend="krisp"))


def test_factory_explicit_rnnoise_fails():
    """Explicitly requesting rnnoise without pyrnnoise should raise."""
    with patch(
        "easycat.noise_reduction.require_module", side_effect=ImportError("RNNoise unavailable")
    ):
        with pytest.raises(RuntimeError, match="RNNoise"):
            create_noise_reducer(NoiseReducerConfig(backend="rnnoise"))


@pytest.mark.asyncio
async def test_factory_auto_passthrough_processes_audio():
    """Factory auto -> passthrough should still process audio."""
    with patch(
        "easycat.noise_reduction.require_module", side_effect=ImportError("RNNoise unavailable")
    ):
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
    from easycat._audio_utils import resample

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
