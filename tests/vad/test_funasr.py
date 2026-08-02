"""VAD tests."""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

from easycat.audio_format import PCM16_MONO_8K, AudioChunk, AudioFormat
from easycat.events import VADStartSpeaking, VADStopSpeaking
from easycat.vad import (
    FunASROnnxVAD,
)
from easycat.vad import funasr as vad_funasr_module
from tests.vad._helpers import _assert_extra_hint, _make_chunk


def test_funasr_state_anomalies_use_logger_not_stdout(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numpy_stub = ModuleType("numpy")
    numpy_stub.ndarray = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", numpy_stub)
    monkeypatch.delitem(
        sys.modules,
        "easycat.vad._funasr_runtime.e2e_vad",
        raising=False,
    )

    from easycat.vad._funasr_runtime.e2e_vad import E2EVadModel

    model = E2EVadModel({})
    model.confirmed_start_frame = 12

    with caplog.at_level(logging.WARNING):
        model.OnVoiceStart(13, fake_result=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "FunASR VAD start frame was not reset: confirmed_start_frame=12" in caplog.text


def test_funasr_vad_fails_without_runtime_dependency(monkeypatch: pytest.MonkeyPatch):
    """FunASROnnxVAD should raise RuntimeError if runtime deps are missing."""

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "numpy":
            raise ImportError(
                "FunASR VAD requires the numpy package. Install with: "
                "uv add 'easycat[funasr-vad]'. From the EasyCat repo, use: "
                "uv sync --extra funasr-vad --group dev."
            )
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_funasr_module, "require_module", _require_module)

    with pytest.raises(RuntimeError) as exc_info:
        FunASROnnxVAD()
    message = str(exc_info.value)
    assert "FunASR" in message
    _assert_extra_hint(message, "funasr-vad")


def test_funasr_vad_initializes_in_tree_runtime(monkeypatch: pytest.MonkeyPatch):
    """FunASROnnxVAD should use EasyCat's in-tree runtime, not funasr_onnx."""
    import easycat.vad._funasr_runtime as runtime_pkg

    seen: dict[str, object] = {}

    class _FakeRuntime:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)
            self.max_end_sil = kwargs.get("max_end_sil")

    monkeypatch.setattr(vad_funasr_module, "require_module", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime_pkg, "FunASROnlineRuntime", _FakeRuntime)

    vad = FunASROnnxVAD(device_id=0, quantize=True, intra_op_num_threads=2)

    assert vad._model is not None
    assert seen == {
        "model_dir": str(vad_funasr_module._FUNASR_BUNDLED_MODEL_DIR),
        "device_id": 0,
        "quantize": True,
        "intra_op_num_threads": 2,
        "max_end_sil": 150,
    }


def test_funasr_vad_rejects_unsupported_cache_dir():
    """The in-tree runtime only supports local model dirs, not download caches."""
    with pytest.raises(ValueError, match="cache_dir is not supported"):
        FunASROnnxVAD(cache_dir="/tmp/funasr-cache")


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
async def test_funasr_vad_emits_complete_same_frame_segment(
    monkeypatch: pytest.MonkeyPatch,
):
    """Complete FunASR segments in one model result should not be dropped."""

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
            return [[0, 240]]

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = _FakeNumpy()
        self._model = _FakeModel()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD(chunk_size_ms=32)
    vad._min_speech_duration_ms = 0
    vad._min_silence_duration_ms = 0

    events = [event async for event in vad.process(_make_chunk(1000))]

    assert [type(event) for event in events] == [VADStartSpeaking, VADStopSpeaking]
    assert vad._funasr_active is False


@pytest.mark.asyncio
async def test_funasr_vad_inference_errors_are_not_silenced(monkeypatch: pytest.MonkeyPatch):
    """Runtime failures should not make FunASR behave like permanent silence."""

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

    class _BrokenModel:
        def __call__(self, audio_in: object, param_dict: dict[str, object]) -> list[list[int]]:
            raise RuntimeError("ORT shape mismatch")

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = _FakeNumpy()
        self._model = _BrokenModel()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD(chunk_size_ms=32)

    with pytest.raises(RuntimeError, match="FunASR ONNX VAD inference failed"):
        async for _ in vad.process(_make_chunk(1000)):
            pass


@pytest.mark.asyncio
async def test_funasr_vad_resamples_8k_input(monkeypatch: pytest.MonkeyPatch):
    """FunASR VAD should resample telephony audio to 16 kHz before inference."""
    model_calls = 0

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
            nonlocal model_calls
            model_calls += 1
            param_dict.setdefault("in_cache", [])
            return []

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = _FakeNumpy()
        self._model = _FakeModel()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD(chunk_size_ms=32)
    chunk_8k = AudioChunk(data=bytes(256 * 2), format=PCM16_MONO_8K)
    events = []
    for _ in range(4):
        events += [event async for event in vad.process(chunk_8k)]

    assert events == []
    assert vad._audio_resampler.source_rate == 8000
    assert model_calls >= 1
    assert len(vad._buffer) < vad._chunk_samples * 2


@pytest.mark.asyncio
async def test_funasr_preserves_stereo_frames_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
):
    """A split interleaved frame must be downmixed only after reassembly."""

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = object()
        self._model = object()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)
    vad = FunASROnnxVAD()
    fmt = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
    data = struct.pack("<6h", 100, 300, 1_000, 2_000, 3_000, 4_000)

    async for _ in vad.process(AudioChunk(data=data[:6], format=fmt)):
        pass
    async for _ in vad.process(AudioChunk(data=data[6:], format=fmt)):
        pass

    assert vad._buffer == struct.pack("<3h", 200, 1_500, 3_500)


@pytest.mark.asyncio
async def test_funasr_vad_yields_to_event_loop_while_draining_backlog(
    monkeypatch: pytest.MonkeyPatch,
):
    """Large silent chunks must not defer cancellation behind the whole backlog."""

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

    class _SilentModel:
        def __call__(self, **_kwargs: object) -> list[object]:
            return []

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = _FakeNumpy()
        self._model = _SilentModel()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)
    vad = FunASROnnxVAD()
    observer_ran = False

    async def observe_loop() -> None:
        nonlocal observer_ran
        observer_ran = True

    observer = asyncio.create_task(observe_loop())
    try:
        events = [
            event async for event in vad.process(_make_chunk(n_samples=vad._chunk_samples * 3))
        ]
        assert events == []
        assert observer_ran
    finally:
        await observer


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
