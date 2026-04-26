"""Echo cancellation tests: LiveKitAEC, PassthroughAEC, factory, and frame splitting."""

import struct
from unittest.mock import MagicMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_16K, PCM16_MONO_48K, AudioChunk
from easycat.echo_cancellation import (
    EchoCancellationConfig,
    LiveKitAEC,
    PassthroughAEC,
    _frame_samples_for_rate,
    _split_frames,
    create_echo_canceller,
)

# ── Frame splitting tests ──────────────────────────────────────────


def test_split_frames_exact():
    """split_frames with data that is an exact multiple of frame size."""
    data = b"\x01\x02" * 160  # 320 bytes = 2 frames of 160 bytes
    frames = _split_frames(data, 160)
    assert len(frames) == 2
    assert all(len(f) == 160 for f in frames)
    assert b"".join(frames) == data


def test_split_frames_with_remainder():
    """split_frames should zero-pad the last frame when data doesn't align."""
    data = b"\xab" * 200  # 200 bytes with 160-byte frames -> 2 frames
    frames = _split_frames(data, 160)
    assert len(frames) == 2
    assert len(frames[0]) == 160
    assert len(frames[1]) == 160
    assert frames[0] == b"\xab" * 160
    # Last frame: 40 bytes of data + 120 bytes of zero padding
    assert frames[1] == b"\xab" * 40 + b"\x00" * 120


def test_split_frames_empty():
    """split_frames with empty data returns no frames."""
    frames = _split_frames(b"", 160)
    assert frames == []


def test_frame_samples_common_rates():
    """frame_samples_for_rate returns correct 10ms frame sizes."""
    assert _frame_samples_for_rate(8000) == 80
    assert _frame_samples_for_rate(16000) == 160
    assert _frame_samples_for_rate(24000) == 240
    assert _frame_samples_for_rate(48000) == 480


def test_frame_samples_uncommon_rate():
    """frame_samples_for_rate falls back to sample_rate // 100."""
    assert _frame_samples_for_rate(44100) == 441


# ── LiveKitAEC tests ──────────────────────────────────────────────


def test_livekit_aec_fails_without_library():
    """LiveKitAEC should raise ImportError if livekit is missing."""
    with patch(
        "easycat.echo_cancellation.require_module",
        side_effect=ImportError("livekit unavailable"),
    ):
        with pytest.raises(ImportError, match="livekit"):
            LiveKitAEC()


def _fake_audio_frame(data: bytes, **_kwargs: object) -> MagicMock:
    """Return a mock AudioFrame whose .data holds the raw bytes."""
    frame = MagicMock()
    frame.data = bytearray(data)
    return frame


@pytest.mark.asyncio
async def test_livekit_aec_process_mocked():
    """LiveKitAEC.process with mocked APM should process and reassemble frames."""
    mock_rtc = MagicMock()
    mock_apm = MagicMock()

    def mock_process_stream(frame_data: bytes) -> bytes:
        # Return the frame unchanged.
        return frame_data

    mock_apm.process_stream.side_effect = mock_process_stream
    mock_rtc.AudioProcessingModule.return_value = mock_apm
    mock_rtc.AudioFrame.side_effect = _fake_audio_frame

    with patch("easycat.echo_cancellation.require_module", return_value=mock_rtc):
        aec = LiveKitAEC()

    # 20ms at 16kHz = 320 samples = 640 bytes
    samples = [100] * 320
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)

    result = await aec.process(chunk)

    assert result.format == PCM16_MONO_16K
    assert len(result.data) == len(data)
    # 20ms = 2 frames of 10ms each
    assert mock_apm.process_stream.call_count == 2


@pytest.mark.asyncio
async def test_livekit_aec_process_trims_padding():
    """LiveKitAEC.process trims zero-padding from the last frame."""
    mock_rtc = MagicMock()
    mock_apm = MagicMock()

    def mock_process_stream(frame_data: bytes) -> bytes:
        return frame_data

    mock_apm.process_stream.side_effect = mock_process_stream
    mock_rtc.AudioProcessingModule.return_value = mock_apm
    mock_rtc.AudioFrame.side_effect = _fake_audio_frame

    with patch("easycat.echo_cancellation.require_module", return_value=mock_rtc):
        aec = LiveKitAEC()

    # 15ms at 16kHz = 240 samples = 480 bytes (not a multiple of 10ms frame = 320 bytes)
    samples = [200] * 240
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)

    result = await aec.process(chunk)

    # Output should be trimmed to original length
    assert len(result.data) == len(data)
    assert mock_apm.process_stream.call_count == 2  # ceil(480/320) = 2


def test_livekit_aec_feed_reference_mocked():
    """LiveKitAEC.feed_reference with mocked APM should call process_reverse_stream."""
    mock_rtc = MagicMock()
    mock_apm = MagicMock()
    mock_rtc.AudioProcessingModule.return_value = mock_apm
    mock_rtc.AudioFrame.side_effect = _fake_audio_frame

    with patch("easycat.echo_cancellation.require_module", return_value=mock_rtc):
        aec = LiveKitAEC()

    # 10ms at 16kHz = 160 samples = 320 bytes (exactly 1 frame)
    samples = [50] * 160
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)

    aec.feed_reference(chunk)

    assert mock_apm.process_reverse_stream.call_count == 1


def test_livekit_aec_feed_reference_multiple_frames():
    """LiveKitAEC.feed_reference splits multi-frame chunks correctly."""
    mock_rtc = MagicMock()
    mock_apm = MagicMock()
    mock_rtc.AudioProcessingModule.return_value = mock_apm
    mock_rtc.AudioFrame.side_effect = _fake_audio_frame

    with patch("easycat.echo_cancellation.require_module", return_value=mock_rtc):
        aec = LiveKitAEC()

    # 30ms at 48kHz = 1440 samples = 2880 bytes -> 3 frames of 480 samples
    samples = [75] * 1440
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_48K)

    aec.feed_reference(chunk)

    assert mock_apm.process_reverse_stream.call_count == 3


# ── PassthroughAEC tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_passthrough_returns_unchanged():
    """PassthroughAEC should return the chunk unchanged."""
    aec = PassthroughAEC()
    chunk = AudioChunk(data=b"\x01\x02" * 80, format=PCM16_MONO_16K)
    result = await aec.process(chunk)
    assert result is chunk


def test_passthrough_feed_reference_noop():
    """PassthroughAEC.feed_reference should accept a chunk without error."""
    aec = PassthroughAEC()
    chunk = AudioChunk(data=b"\x00\x00" * 160, format=PCM16_MONO_16K)
    aec.feed_reference(chunk)  # should not raise


# ── Factory tests ────────────────────────────────────────────────


def test_factory_disabled_returns_passthrough():
    """Factory with enabled=False returns PassthroughAEC."""
    result = create_echo_canceller(EchoCancellationConfig(enabled=False))
    assert isinstance(result, PassthroughAEC)


def test_factory_default_returns_passthrough():
    """Factory with default config returns PassthroughAEC (disabled by default)."""
    result = create_echo_canceller()
    assert isinstance(result, PassthroughAEC)


def test_factory_enabled_without_livekit_falls_back():
    """Factory with enabled=True but no livekit falls back to passthrough."""
    with patch(
        "easycat.echo_cancellation.require_module",
        side_effect=ImportError("livekit unavailable"),
    ):
        result = create_echo_canceller(EchoCancellationConfig(enabled=True))
        assert isinstance(result, PassthroughAEC)


def test_factory_enabled_without_livekit_strict_fails():
    """Strict fallback policy should fail when enabled AEC cannot be loaded."""
    with patch(
        "easycat.echo_cancellation.require_module",
        side_effect=ImportError("livekit unavailable"),
    ):
        with pytest.raises(RuntimeError, match="LiveKit AEC is unavailable"):
            create_echo_canceller(EchoCancellationConfig(enabled=True, fallback_policy="error"))


def test_echo_cancellation_config_rejects_unknown_fallback_policy():
    """EchoCancellationConfig should reject unknown fallback policies."""
    with pytest.raises(ValueError, match="Unknown echo cancellation fallback_policy 'silent'"):
        EchoCancellationConfig(enabled=True, fallback_policy="silent")  # type: ignore[arg-type]


def test_factory_enabled_with_livekit():
    """Factory with enabled=True and livekit available returns LiveKitAEC."""
    mock_rtc = MagicMock()
    mock_rtc.AudioProcessingModule.return_value = MagicMock()

    with patch("easycat.echo_cancellation.require_module", return_value=mock_rtc):
        result = create_echo_canceller(EchoCancellationConfig(enabled=True))
        assert isinstance(result, LiveKitAEC)
