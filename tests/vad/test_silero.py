"""VAD tests."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

from easycat.audio_format import AudioChunk
from easycat.events import VADStartSpeaking
from easycat.vad import (
    SileroVAD,
)
from easycat.vad import silero as vad_silero_module
from tests.vad._helpers import _assert_extra_hint, _make_chunk


def test_silero_backend_candidates_prefer_onnx_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_SILERO_BACKEND", raising=False)
    assert vad_silero_module._silero_backend_candidates() == ("onnx",)


def test_silero_backend_candidates_respect_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_SILERO_BACKEND", "torch")
    assert vad_silero_module._silero_backend_candidates() == ("torch",)


def test_silero_onnx_model_path_uses_bundled_asset():
    model_path = vad_silero_module._silero_onnx_model_path()
    assert model_path.endswith("src/easycat/models/silero_vad.onnx")


def test_silero_fails_when_only_torch_backend_is_allowed(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should reject the remote-code-executing torch backend."""
    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("torch",))

    with pytest.raises(RuntimeError) as exc_info:
        SileroVAD()
    message = str(exc_info.value)
    assert "torch backend is disabled" in message
    _assert_extra_hint(message, "silero-vad")


def test_silero_torch_backend_does_not_call_torch_hub(monkeypatch: pytest.MonkeyPatch):
    """The disabled torch path must not reach torch.hub.load."""
    mock_torch = MagicMock()

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "torch":
            return mock_torch
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_silero_module, "require_module", _require_module)

    with pytest.raises(RuntimeError) as exc_info:
        SileroVAD._load_torch_model(MagicMock())
    message = str(exc_info.value)
    assert "torch backend is disabled" in message
    _assert_extra_hint(message, "silero-vad")

    mock_torch.hub.load.assert_not_called()


@pytest.mark.asyncio
async def test_silero_process_mocked_onnx(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should detect speech with the ONNX fallback backend."""
    # ``process()`` vectorizes PCM via ``numpy`` (part of the silero-vad
    # extra), so skip when it is absent (e.g. the minimal validate-quick lane).
    pytest.importorskip("numpy")

    class _FakeOnnxModel:
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, samples: list[float], sample_rate: int) -> float:
            assert sample_rate == 16000
            assert len(samples) == 512
            self.calls += 1
            return 0.9 if self.calls <= 3 else 0.1

        def reset_states(self) -> None:
            pass

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _FakeOnnxModel()
        self._backend = "onnx"
        self._torch = None

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()
    vad._min_speech_duration_ms = 0
    vad._min_silence_duration_ms = 0
    vad._threshold = 0.5

    events = []
    async for event in vad.process(_make_chunk(1000)):
        events.append(event)

    assert any(isinstance(e, VADStartSpeaking) for e in events)
    assert vad._backend == "onnx"


@pytest.mark.asyncio
async def test_silero_warmup_primes_model_then_resets(monkeypatch: pytest.MonkeyPatch):
    """warmup() runs one zeroed 16k/512-sample predict then resets state."""

    class _FakeOnnxModel:
        def __init__(self) -> None:
            self.predict_calls: list[tuple[int, int]] = []
            self.reset_calls = 0

        def predict(self, samples: list[float], sample_rate: int) -> float:
            self.predict_calls.append((len(samples), sample_rate))
            assert all(s == 0.0 for s in samples)
            return 0.0

        def reset_states(self) -> None:
            self.reset_calls += 1

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _FakeOnnxModel()
        self._backend = "onnx"
        self._torch = None

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()
    await vad.warmup()

    assert vad._model.predict_calls == [(512, 16000)]
    # reset_states is called once by warmup (the __init__ load path resets
    # internally inside the real model, not through this fake).
    assert vad._model.reset_calls == 1


@pytest.mark.asyncio
async def test_silero_warmup_swallows_errors(monkeypatch: pytest.MonkeyPatch):
    """A predict failure during warmup must not propagate to Session.start()."""

    class _ExplodingOnnxModel:
        def predict(self, samples: list[float], sample_rate: int) -> float:
            raise RuntimeError("boom")

        def reset_states(self) -> None:
            pass

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _ExplodingOnnxModel()
        self._backend = "onnx"
        self._torch = None

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()
    # Returns cleanly despite the predict raising.
    await vad.warmup()


def test_silero_falls_back_to_onnx_after_torch_failure(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should use ONNX if an overridden torch loader fails."""
    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("torch", "onnx"))

    def _load_torch_model(self: SileroVAD) -> None:
        raise RuntimeError("torch loader failed")

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = MagicMock()
        self._backend = "onnx"
        self._torch = None

    monkeypatch.setattr(SileroVAD, "_load_torch_model", _load_torch_model)
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()
    assert vad._backend == "onnx"


@pytest.mark.asyncio
async def test_silero_downmixes_stereo_to_mono(monkeypatch: pytest.MonkeyPatch):
    """Interleaved stereo input is downmixed before frame slicing."""
    # ``process()`` vectorizes PCM via ``numpy`` (part of the silero-vad
    # extra), so skip when it is absent (e.g. the minimal validate-quick lane).
    pytest.importorskip("numpy")
    from easycat.audio_format import AudioFormat

    seen: list[int] = []

    class _FakeOnnxModel:
        def predict(self, samples: list[float], sample_rate: int) -> float:
            assert sample_rate == 16000
            seen.append(len(samples))
            return 0.1

        def reset_states(self) -> None:
            pass

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _FakeOnnxModel()
        self._backend = "onnx"
        self._torch = None

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()

    # 512 mono frames worth of audio interleaved across 2 channels => 1024
    # int16 samples. Without downmix, frame slicing would read 2x the frames
    # at the wrong sample boundaries.
    stereo_fmt = AudioFormat(sample_rate=16000, channels=2, sample_width=2)
    data = struct.pack(f"<{512 * 2}h", *([1000] * (512 * 2)))
    chunk = AudioChunk(data=data, format=stereo_fmt)

    async for _ in vad.process(chunk):
        pass

    # After downmix there are exactly 512 mono samples => one 512-sample frame.
    assert seen == [512]
