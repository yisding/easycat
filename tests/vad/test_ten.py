"""VAD tests."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from easycat.events import VADStartSpeaking
from easycat.vad import (
    TenVAD,
)
from easycat.vad import ten as vad_ten_module
from tests.vad._helpers import _make_chunk


def test_ten_vad_fails_without_sdk(monkeypatch: pytest.MonkeyPatch):
    """TenVAD should raise RuntimeError if ten_vad package is missing."""

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "ten_vad":
            raise ImportError("TEN VAD requires ten_vad")
        if module_name == "numpy":
            return types.SimpleNamespace()
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_ten_module, "require_module", _require_module)
    with pytest.raises(RuntimeError, match="TEN VAD|ten_vad"):
        TenVAD()


@pytest.mark.asyncio
async def test_ten_vad_process_mocked(monkeypatch: pytest.MonkeyPatch):
    """TenVAD with mocked SDK should process audio."""
    mock_ten_vad = MagicMock()
    mock_instance = MagicMock()
    mock_instance.process.return_value = (0.9, 1)
    mock_ten_vad.TenVad.return_value = mock_instance

    import sys

    sys.modules["ten_vad"] = mock_ten_vad

    import types

    class _FakeArray:
        def copy(self):
            return self

    fake_numpy = types.SimpleNamespace(
        int16="int16",
        frombuffer=lambda data, dtype: _FakeArray(),
    )
    sys.modules["numpy"] = fake_numpy

    try:
        vad = TenVAD()
        vad._min_speech_duration_ms = 0
        vad._threshold = 0.5

        chunk = _make_chunk(1000)
        events = []
        async for event in vad.process(chunk):
            events.append(event)

        assert any(isinstance(e, VADStartSpeaking) for e in events)
        assert mock_instance.process.call_count == 2
    finally:
        del sys.modules["ten_vad"]
        del sys.modules["numpy"]
