"""VAD tests: Silero, Krisp, factory, and configuration."""

import struct
from unittest.mock import MagicMock

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import VADStartSpeaking
from easycat.vad import (
    KrispVAD,
    SileroVAD,
    VADConfig,
    create_vad,
)


def _make_chunk(value: int = 0, n_samples: int = 512) -> AudioChunk:
    """Create a PCM16 chunk filled with the given sample value."""
    data = struct.pack(f"<{n_samples}h", *([value] * n_samples))
    return AudioChunk(data=data, format=PCM16_MONO_16K)


# ── SileroVAD tests ─────────────────────────────────────────────────


def test_silero_fails_without_torch():
    """SileroVAD should raise RuntimeError if torch is missing."""
    with pytest.raises(RuntimeError, match="torch|PyTorch|Silero"):
        SileroVAD()


@pytest.mark.asyncio
async def test_silero_process_mocked():
    """SileroVAD with mocked model should detect speech/silence."""
    # Create a mock model that returns configurable probabilities
    mock_model = MagicMock()
    call_count = [0]

    def model_call(tensor, sr):
        call_count[0] += 1
        result = MagicMock()
        # First 3 calls return speech, then silence
        if call_count[0] <= 3:
            result.item.return_value = 0.9
        else:
            result.item.return_value = 0.1
        return result

    mock_model.side_effect = model_call
    mock_model.reset_states = MagicMock()

    # Manually construct SileroVAD with mocked model
    import sys

    mock_torch = MagicMock()
    mock_torch.hub.load.return_value = (mock_model, None)
    mock_torch.FloatTensor = lambda x: MagicMock()
    sys.modules["torch"] = mock_torch

    try:
        vad = SileroVAD()
        # Configure with 0ms min durations for easier testing
        vad._min_speech_duration_ms = 0
        vad._min_silence_duration_ms = 0
        vad._threshold = 0.5

        # Feed speech chunk - should get VADStartSpeaking
        speech_chunk = _make_chunk(1000)
        events = []
        async for event in vad.process(speech_chunk):
            events.append(event)

        assert any(isinstance(e, VADStartSpeaking) for e in events)
    finally:
        del sys.modules["torch"]


# ── KrispVAD tests ──────────────────────────────────────────────────


def test_krisp_vad_fails_without_sdk():
    """KrispVAD should raise RuntimeError if SDK is missing."""
    with pytest.raises(RuntimeError, match="Krisp"):
        KrispVAD()


@pytest.mark.asyncio
async def test_krisp_vad_process_mocked():
    """KrispVAD with mocked SDK should process audio."""
    mock_module = MagicMock()
    mock_session = MagicMock()
    mock_module.create_vad_session.return_value = mock_session
    mock_module.vad_process.return_value = 0.9  # Speech probability

    import sys

    sys.modules["krisp_audio"] = mock_module

    try:
        vad = KrispVAD()
        vad._min_speech_duration_ms = 0
        vad._threshold = 0.5

        chunk = _make_chunk(1000)
        events = []
        async for event in vad.process(chunk):
            events.append(event)

        assert any(isinstance(e, VADStartSpeaking) for e in events)
        mock_module.vad_process.assert_called_once()
    finally:
        del sys.modules["krisp_audio"]


@pytest.mark.asyncio
async def test_krisp_vad_silence():
    """KrispVAD should not emit events for silence."""
    mock_module = MagicMock()
    mock_session = MagicMock()
    mock_module.create_vad_session.return_value = mock_session
    mock_module.vad_process.return_value = 0.1  # Low probability = silence

    import sys

    sys.modules["krisp_audio"] = mock_module

    try:
        vad = KrispVAD()
        chunk = _make_chunk(0)
        events = []
        async for event in vad.process(chunk):
            events.append(event)

        assert len(events) == 0
    finally:
        del sys.modules["krisp_audio"]


# ── VAD configuration tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_krisp_vad_configure():
    """VAD configure should update internal parameters."""
    mock_module = MagicMock()
    mock_module.create_vad_session.return_value = MagicMock()

    import sys

    sys.modules["krisp_audio"] = mock_module

    try:
        vad = KrispVAD()
        vad.configure(
            min_speech_duration_ms=100,
            min_silence_duration_ms=200,
            sensitivity=0.8,
            pre_roll_ms=50,
            post_roll_ms=50,
        )

        assert vad._min_speech_duration_ms == 100
        assert vad._min_silence_duration_ms == 200
        assert vad._threshold == pytest.approx(0.2)  # 1.0 - 0.8
        assert vad._pre_roll_ms == 50
        assert vad._post_roll_ms == 50
    finally:
        del sys.modules["krisp_audio"]


# ── Factory tests ────────────────────────────────────────────────────


def test_vad_factory_no_backends():
    """Factory should raise RuntimeError when no backends are available."""
    with pytest.raises(RuntimeError, match="No VAD backend"):
        create_vad(VADConfig(backend="auto"))


def test_vad_factory_explicit_silero_fails():
    """Explicitly requesting silero without torch should raise."""
    with pytest.raises(RuntimeError, match="torch|PyTorch|Silero"):
        create_vad(VADConfig(backend="silero"))


def test_vad_factory_explicit_krisp_fails():
    """Explicitly requesting krisp without SDK should raise."""
    with pytest.raises(RuntimeError, match="Krisp"):
        create_vad(VADConfig(backend="krisp"))


def test_vad_factory_krisp_preferred():
    """In auto mode, Krisp should be tried first."""
    mock_module = MagicMock()
    mock_module.create_vad_session.return_value = MagicMock()

    import sys

    sys.modules["krisp_audio"] = mock_module

    try:
        vad = create_vad(VADConfig(backend="auto"))
        assert isinstance(vad, KrispVAD)
    finally:
        del sys.modules["krisp_audio"]


def test_vad_factory_applies_config():
    """Factory should apply configuration to the created VAD."""
    mock_module = MagicMock()
    mock_module.create_vad_session.return_value = MagicMock()

    import sys

    sys.modules["krisp_audio"] = mock_module

    try:
        cfg = VADConfig(
            backend="krisp",
            min_speech_duration_ms=100,
            sensitivity=0.7,
        )
        vad = create_vad(cfg)
        assert vad._min_speech_duration_ms == 100
        assert vad._threshold == pytest.approx(0.3)
    finally:
        del sys.modules["krisp_audio"]


# ── Short noise burst test ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_noise_burst_no_event():
    """Short noise bursts below min_speech_duration should not trigger events."""
    mock_module = MagicMock()
    mock_session = MagicMock()
    mock_module.create_vad_session.return_value = mock_session

    call_count = [0]

    def mock_vad_process(session, data, sr):
        call_count[0] += 1
        # One brief speech frame, then silence
        if call_count[0] == 1:
            return 0.9
        return 0.1

    mock_module.vad_process.side_effect = mock_vad_process

    import sys

    sys.modules["krisp_audio"] = mock_module

    try:
        vad = KrispVAD()
        vad._min_speech_duration_ms = 250  # Require 250ms

        events = []
        for _ in range(5):
            chunk = _make_chunk()
            async for event in vad.process(chunk):
                events.append(event)

        # Should not emit start because speech was too brief
        assert not any(isinstance(e, VADStartSpeaking) for e in events)
    finally:
        del sys.modules["krisp_audio"]
