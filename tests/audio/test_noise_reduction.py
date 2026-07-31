"""Noise reduction tests: RNNoise, Krisp, factory, and helpers."""

import struct
from unittest.mock import MagicMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
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
    tail = reducer.flush()

    # Output should be at original sample rate
    assert result.format.sample_rate == 16000
    assert tail.format.sample_rate == 16000
    assert len(result.data) + len(tail.data) > 0
    # RNNoise should have been called
    assert mock_rnnoise.process_mono_frame.called


async def test_rnnoise_downmixes_stereo_before_mono_filtering():
    np = pytest.importorskip("numpy")
    seen_frames: list = []
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480
    mock_rnnoise.create.return_value = MagicMock()

    def record_frame(_state, frame):
        seen_frames.append(np.array(frame, copy=True))
        return frame, 0.0

    mock_rnnoise.process_mono_frame.side_effect = record_frame
    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    stereo_format = AudioFormat(sample_rate=48_000, channels=2, sample_width=2)
    # Ten milliseconds of stereo audio is 480 frames, not 960 mono samples.
    data = struct.pack("<960h", *([1000, -1000] * 480))
    source = AudioChunk(data=data, format=stereo_format)

    result = await reducer.process(source)
    tail = reducer.flush()

    assert len(seen_frames) == 1
    assert not seen_frames[0].any()
    assert result.format == AudioFormat(sample_rate=48_000, channels=1, sample_width=2)
    assert len(result.data) + len(tail.data) == 480 * 2
    assert (len(result.data) + len(tail.data)) / result.format.bytes_per_second == pytest.approx(
        source.duration_ms / 1000
    )


async def test_rnnoise_preserves_buffered_mono_audio_across_channel_transition():
    pytest.importorskip("numpy")
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480
    mock_rnnoise.create.return_value = MagicMock()
    mock_rnnoise.process_mono_frame.side_effect = lambda _state, frame: (frame, 0.0)
    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    mono_format = AudioFormat(sample_rate=48_000, channels=1, sample_width=2)
    stereo_format = AudioFormat(sample_rate=48_000, channels=2, sample_width=2)
    first = await reducer.process(
        AudioChunk(data=struct.pack("<100h", *([100] * 100)), format=mono_format)
    )
    second = await reducer.process(
        AudioChunk(
            data=struct.pack("<760h", *([200, 200] * 380)),
            format=stereo_format,
        )
    )
    tail = reducer.flush()
    output = struct.unpack("<480h", first.data + second.data + tail.data)

    assert output[:100] == (100,) * 100
    assert output[100:] == (200,) * 380
    assert mock_rnnoise.process_mono_frame.call_count == 1


async def test_rnnoise_carries_split_multichannel_frame_before_downmix():
    pytest.importorskip("numpy")
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 1
    mock_rnnoise.create.return_value = MagicMock()
    mock_rnnoise.process_mono_frame.side_effect = lambda _state, frame: (frame, 0.0)
    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    stereo_format = AudioFormat(sample_rate=48_000, channels=2, sample_width=2)
    # Three continuous stereo frames split after the left sample of frame 2.
    first = AudioChunk(data=struct.pack("<3h", 100, 300, 1000), format=stereo_format)
    second = AudioChunk(data=struct.pack("<3h", 2000, 3000, 4000), format=stereo_format)

    first_result = await reducer.process(first)
    second_result = await reducer.process(second)
    tail = reducer.flush()
    output = first_result.data + second_result.data + tail.data

    assert struct.unpack("<3h", output) == (200, 1500, 3500)
    assert reducer._source_frame_carry == b""


async def test_rnnoise_discards_partial_source_frame_on_format_change_and_flush():
    pytest.importorskip("numpy")
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 1
    mock_rnnoise.create.return_value = MagicMock()
    mock_rnnoise.process_mono_frame.side_effect = lambda _state, frame: (frame, 0.0)
    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    stereo_format = AudioFormat(sample_rate=48_000, channels=2, sample_width=2)
    mono_format = AudioFormat(sample_rate=48_000, channels=1, sample_width=2)
    orphan = await reducer.process(AudioChunk(data=struct.pack("<h", 1000), format=stereo_format))
    assert orphan.data == b""
    assert reducer._source_frame_carry == struct.pack("<h", 1000)

    mono = await reducer.process(AudioChunk(data=struct.pack("<h", 2000), format=mono_format))
    assert struct.unpack("<h", mono.data) == (2000,)
    assert reducer._source_frame_carry == b""

    await reducer.process(AudioChunk(data=b"\x01", format=mono_format))
    assert reducer._source_frame_carry == b"\x01"
    reducer.flush()
    assert reducer._source_frame_carry == b""
    assert reducer._source_format is None


@pytest.mark.parametrize(
    "fmt",
    [
        AudioFormat(sample_rate=48_000, channels=1, sample_width=1, encoding="pcm"),
        AudioFormat(sample_rate=48_000, channels=1, sample_width=2, encoding="mulaw"),
    ],
)
async def test_rnnoise_rejects_non_pcm16_audio(fmt):
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480
    mock_rnnoise.create.return_value = MagicMock()
    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    with pytest.raises(ValueError, match="PCM16"):
        await reducer.process(AudioChunk(data=b"\x00" * 480, format=fmt))


@pytest.mark.asyncio
async def test_rnnoise_buffers_subframe_remainder_across_chunks():
    """Non-480-aligned chunks must not be zero-padded mid-stream.

    RNNoise is a stateful recurrent denoiser; padding the tail of every chunk
    would inject silence between chunk boundaries.  The reducer must instead
    buffer the sub-frame remainder and only submit whole 480-sample frames,
    deferring the rest to the next call (and flushing on ``flush()``).
    """
    np = pytest.importorskip("numpy")

    seen_frames: list = []
    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480

    def record_frame(state, frame):
        seen_frames.append(np.array(frame, copy=True))
        return frame, 0.0

    mock_rnnoise.process_mono_frame.side_effect = record_frame
    mock_rnnoise.create.return_value = MagicMock()

    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    from easycat.audio_format import PCM16_MONO_48K

    # Already at 48 kHz so no resampling reshapes the frame boundaries.
    # 500 samples = one whole 480-sample frame + a 20-sample remainder.
    first = struct.pack("<500h", *([1000] * 500))
    await reducer.process(AudioChunk(data=first, format=PCM16_MONO_48K))
    # Exactly one whole frame submitted; the 20-sample tail is buffered.
    assert len(seen_frames) == 1
    assert len(reducer._buffer_48k) == 20 * 2

    # Next chunk of 460 samples completes the buffered tail to a full frame.
    second = struct.pack("<460h", *([2000] * 460))
    await reducer.process(AudioChunk(data=second, format=PCM16_MONO_48K))
    assert len(seen_frames) == 2
    # No frame submitted mid-stream contained a zero-padded silence tail
    # (every value is one of the two real amplitudes we wrote).
    for frame in seen_frames:
        assert not (frame == 0).any()

    # Flush drains the final partial frame (zero-padded only at end-of-stream).
    reducer.flush()
    assert reducer._buffer_48k == b""


@pytest.mark.asyncio
async def test_rnnoise_flush_preserves_source_rate_and_resampler_tail():
    pytest.importorskip("numpy")

    mock_rnnoise = MagicMock()
    mock_rnnoise.FRAME_SIZE = 480
    mock_rnnoise.create.return_value = MagicMock()
    mock_rnnoise.process_mono_frame.side_effect = lambda _state, frame: (frame, 0.0)

    with patch("easycat.noise_reduction.require_module", return_value=mock_rnnoise):
        reducer = RNNoiseReducer()

    data = struct.pack("<100h", *([1000] * 100))
    result = await reducer.process(AudioChunk(data=data, format=PCM16_MONO_16K))
    tail = reducer.flush()

    assert result.format == PCM16_MONO_16K
    assert tail.format == PCM16_MONO_16K
    assert len(result.data) + len(tail.data) == len(data)


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


def test_factory_auto_fallback_policy_error_raises():
    """auto + fallback_policy='error' should fail loudly with an install hint."""
    with patch(
        "easycat.noise_reduction.require_module", side_effect=ImportError("RNNoise unavailable")
    ):
        with pytest.raises(RuntimeError) as exc_info:
            create_noise_reducer(NoiseReducerConfig(backend="auto", fallback_policy="error"))
    message = str(exc_info.value)
    assert "uv add 'easycat[rnnoise]'" in message
    assert "uv sync --extra rnnoise --group dev" in message


def test_factory_auto_fallback_policy_passthrough_warns(caplog: pytest.LogCaptureFixture):
    """auto + default passthrough policy should warn but return passthrough."""
    import logging

    with patch(
        "easycat.noise_reduction.require_module", side_effect=ImportError("RNNoise unavailable")
    ):
        with caplog.at_level(logging.WARNING, logger="easycat.noise_reduction"):
            reducer = create_noise_reducer(NoiseReducerConfig(backend="auto"))
    assert isinstance(reducer, PassthroughNoiseReducer)
    assert any("passthrough" in record.message.lower() for record in caplog.records)
    assert any("uv add 'easycat[rnnoise]'" in record.message for record in caplog.records)
    assert any(
        "uv sync --extra rnnoise --group dev" in record.message for record in caplog.records
    )


def test_noise_reducer_config_rejects_unknown_fallback_policy():
    """NoiseReducerConfig should reject typo fallback_policy strings."""
    with pytest.raises(ValueError, match="Unknown noise reducer fallback_policy 'boom'"):
        NoiseReducerConfig(fallback_policy="boom")  # type: ignore[arg-type]


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


# ── Vectorized clip/round regression test ────────────────────────────


def test_clip_round_to_pcm16_bytes_matches_scalar_reference():
    """The vectorized clip+round+pack must be byte-identical to the scalar
    ``max(-32768, min(32767, int(round(v))))`` + ``struct.pack`` loop it
    replaced, including out-of-range values and exact .5 rounding boundaries
    (both use round-half-to-even, so this must hold exactly, not just
    approximately).
    """
    import random

    np = pytest.importorskip("numpy")

    from easycat.noise_reduction import _clip_round_to_pcm16_bytes

    def scalar_reference(samples: list[float]) -> bytes:
        clipped = (max(-32768, min(32767, int(round(v)))) for v in samples)
        return struct.pack(f"<{len(samples)}h", *clipped)

    rng = random.Random(1234)

    # Random floats across and beyond the int16 range.
    random_samples = [rng.uniform(-40000, 40000) for _ in range(2000)]

    # Exact .5 boundaries (banker's rounding: rounds to nearest even integer).
    half_boundary_samples = [
        -32768.5,
        -3.5,
        -2.5,
        -1.5,
        -0.5,
        0.5,
        1.5,
        2.5,
        3.5,
        32766.5,
        32767.5,
        32768.5,
    ]

    # Exact int16 range edges and beyond.
    edge_samples = [-32769.0, -32768.0, -32767.9, 32767.0, 32767.9, 32768.0, 32769.0, 0.0]

    for samples in (random_samples, half_boundary_samples, edge_samples):
        expected = scalar_reference(samples)
        actual = _clip_round_to_pcm16_bytes(np.asarray(samples, dtype=np.float64))
        assert actual == expected


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
