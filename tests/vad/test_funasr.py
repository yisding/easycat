"""VAD tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from easycat.audio_format import PCM16_MONO_8K, AudioChunk
from easycat.events import VADStartSpeaking, VADStopSpeaking
from easycat.vad import (
    FunASROnnxVAD,
)
from easycat.vad import funasr as vad_funasr_module
from tests.vad._helpers import _assert_extra_hint, _make_chunk


def test_funasr_vad_fails_without_sdk(monkeypatch: pytest.MonkeyPatch):
    """FunASROnnxVAD should raise RuntimeError if funasr_onnx is missing."""

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "numpy":
            return object()
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_funasr_module, "require_module", _require_module)
    monkeypatch.setattr(
        vad_funasr_module,
        "find_spec",
        lambda name: None if name == "funasr_onnx" else None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        FunASROnnxVAD()
    message = str(exc_info.value)
    assert "FunASR" in message
    _assert_extra_hint(message, "funasr-vad")


@pytest.mark.asyncio
async def test_funasr_vad_process_streaming_segments(monkeypatch: pytest.MonkeyPatch):  # noqa: C901
    """FunASR boundaries should map to EasyCat start/stop events."""

    class _FakeWaveform:
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.dtype = None
            self.divisor = None

        def astype(self, dtype: object) -> _FakeWaveform:
            self.dtype = dtype
            return self

        def __truediv__(self, value: float) -> _FakeWaveform:
            self.divisor = value
            return self

    class _FakeNumpy:
        int16 = "int16"
        float32 = "float32"

        @staticmethod
        def frombuffer(data: bytes, dtype: object) -> _FakeWaveform:
            assert dtype == "int16"
            return _FakeWaveform(data)

    class _FakeModel:
        def __init__(self) -> None:
            self.calls = 0
            self.max_end_sil = None

        def __call__(self, audio_in: object, param_dict: dict[str, object]) -> list[list[int]]:
            self.calls += 1
            assert isinstance(audio_in, _FakeWaveform)
            assert audio_in.dtype == "float32"
            assert audio_in.divisor == 32768.0
            param_dict.setdefault("in_cache", [])
            if self.calls == 1:
                return [[0, -1]]
            if self.calls == 2:
                return [[-1, 240]]
            return []

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = _FakeNumpy()
        self._model = _FakeModel()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD(chunk_size_ms=32)
    vad._min_speech_duration_ms = 0
    vad._min_silence_duration_ms = 0

    events = []
    async for event in vad.process(_make_chunk(1000)):
        events.append(event)
    # With min_silence_duration_ms=0 the stop event fires on the first silent
    # frame, mirroring the speech path (no extra empty frame needed).
    async for event in vad.process(_make_chunk(0)):
        events.append(event)

    assert any(isinstance(e, VADStartSpeaking) for e in events)
    assert any(isinstance(e, VADStopSpeaking) for e in events)


@pytest.mark.asyncio
async def test_funasr_vad_resamples_8k_input(monkeypatch: pytest.MonkeyPatch):
    """FunASR VAD should resample telephony audio to 16 kHz before inference."""

    class _FakeWaveform:
        def astype(self, _dtype: object) -> _FakeWaveform:
            return self

        def __truediv__(self, _value: float) -> _FakeWaveform:
            return self

    class _FakeNumpy:
        int16 = "int16"
        float32 = "float32"

        @staticmethod
        def frombuffer(_data: bytes, dtype: object) -> _FakeWaveform:
            assert dtype == "int16"
            return _FakeWaveform()

    class _FakeModel:
        def __call__(self, audio_in: object, param_dict: dict[str, object]) -> list[list[int]]:
            param_dict.setdefault("in_cache", [])
            return []

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = _FakeNumpy()
        self._model = _FakeModel()
        self._param_dict = {"in_cache": []}

    def _resample_chunk(chunk: AudioChunk, sample_rate: int) -> AudioChunk:
        assert chunk.format == PCM16_MONO_8K
        assert sample_rate == 16000
        return _make_chunk(0)

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)
    monkeypatch.setattr(vad_funasr_module, "resample_chunk", _resample_chunk)

    vad = FunASROnnxVAD(chunk_size_ms=32)
    chunk_8k = AudioChunk(data=bytes(256 * 2), format=PCM16_MONO_8K)
    events = [event async for event in vad.process(chunk_8k)]
    assert events == []


def test_funasr_vad_configure_updates_model_silence(monkeypatch: pytest.MonkeyPatch):
    """Configuring FunASR VAD should update the runtime silence threshold."""

    class _FakeModel:
        def __init__(self) -> None:
            self.max_end_sil = 0

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = object()
        self._model = _FakeModel()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD()
    vad.configure(min_silence_duration_ms=320)
    assert vad._model.max_end_sil == 320


def test_funasr_vad_reset_clears_streaming_state(monkeypatch: pytest.MonkeyPatch):
    """Reset should clear buffered audio and cached FunASR state."""

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = object()
        self._model = object()
        self._param_dict = {"in_cache": ["cached"], "frontend": object()}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD()
    vad._buffer = b"abc"
    vad._is_speaking = True

    vad.reset()

    assert vad._buffer == b""
    assert vad._param_dict == {"in_cache": []}
    assert vad._is_speaking is False


def test_resolve_funasr_model_dir_uses_bundled_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Default FunASR model id should resolve to the bundled asset directory."""

    bundled = tmp_path / "funasr"
    bundled.mkdir()
    for name in ("model.onnx", "config.yaml", "am.mvn"):
        (bundled / name).write_bytes(b"x")

    monkeypatch.setattr(vad_funasr_module, "_FUNASR_BUNDLED_MODEL_DIR", bundled)

    resolved = vad_funasr_module._resolve_funasr_model_dir(vad_funasr_module._FUNASR_DEFAULT_MODEL)
    assert resolved == str(bundled)
