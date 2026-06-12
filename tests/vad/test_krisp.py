"""VAD tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from easycat.events import VADStartSpeaking
from easycat.vad import (
    KrispVAD,
)
from easycat.vad import krisp as vad_krisp_module
from tests.vad._helpers import _make_chunk


def test_krisp_vad_fails_without_sdk(monkeypatch: pytest.MonkeyPatch):
    """KrispVAD should raise RuntimeError if SDK is missing."""

    def _require_module(_module_name: str, **_: object) -> object:
        raise ImportError("Krisp VAD requires krisp_audio")

    monkeypatch.setattr(
        vad_krisp_module,
        "require_module",
        _require_module,
    )
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
        )

        assert vad._min_speech_duration_ms == 100
        assert vad._min_silence_duration_ms == 200
        assert vad._threshold == pytest.approx(0.2)  # 1.0 - 0.8
    finally:
        del sys.modules["krisp_audio"]


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
