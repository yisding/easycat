"""VAD tests: Silero, Krisp, factory, and configuration."""

import struct
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk
from easycat.events import VADStartSpeaking, VADStopSpeaking
from easycat.vad import (
    FunASROnnxVAD,
    KrispVAD,
    SileroVAD,
    TenVAD,
    VADConfig,
    create_vad,
)
from easycat.vad import factory as vad_factory_module
from easycat.vad import funasr as vad_funasr_module
from easycat.vad import krisp as vad_krisp_module
from easycat.vad import silero as vad_silero_module
from easycat.vad import ten as vad_ten_module
from easycat.vad._base import _VADBase


def _make_chunk(value: int = 0, n_samples: int = 512) -> AudioChunk:
    """Create a PCM16 chunk filled with the given sample value."""
    data = struct.pack(f"<{n_samples}h", *([value] * n_samples))
    return AudioChunk(data=data, format=PCM16_MONO_16K)


# ── SileroVAD tests ─────────────────────────────────────────────────


def test_silero_backend_candidates_prefer_onnx_on_arm64(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_SILERO_BACKEND", raising=False)
    monkeypatch.setattr(vad_silero_module.platform, "machine", lambda: "aarch64")
    assert vad_silero_module._silero_backend_candidates() == ("onnx",)


def test_silero_backend_candidates_prefer_torch_elsewhere(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_SILERO_BACKEND", raising=False)
    monkeypatch.setattr(vad_silero_module.platform, "machine", lambda: "x86_64")
    assert vad_silero_module._silero_backend_candidates() == ("torch", "onnx")


def test_silero_backend_candidates_respect_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_SILERO_BACKEND", "torch")
    monkeypatch.setattr(vad_silero_module.platform, "machine", lambda: "aarch64")
    assert vad_silero_module._silero_backend_candidates() == ("torch",)


def test_silero_onnx_model_path_uses_bundled_asset():
    model_path = vad_silero_module._silero_onnx_model_path()
    assert model_path.endswith("src/easycat/models/silero_vad.onnx")


def test_silero_fails_when_only_torch_backend_is_allowed(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should raise RuntimeError if torch is unavailable and ONNX is disabled."""
    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("torch",))

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "torch":
            raise ImportError("Silero VAD requires the torch package.")
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_silero_module, "require_module", _require_module)

    with pytest.raises(RuntimeError, match="torch|PyTorch|Silero"):
        SileroVAD()


@pytest.mark.asyncio
async def test_silero_process_mocked_torch(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should still work with the torch backend when it loads."""
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

    mock_torch = MagicMock()
    mock_torch.hub.load.return_value = (mock_model, None)
    mock_torch.FloatTensor = lambda x: MagicMock()

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "torch":
            return mock_torch
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("torch",))
    monkeypatch.setattr(vad_silero_module, "require_module", _require_module)

    vad = SileroVAD()
    vad._min_speech_duration_ms = 0
    vad._min_silence_duration_ms = 0
    vad._threshold = 0.5

    speech_chunk = _make_chunk(1000)
    events = []
    async for event in vad.process(speech_chunk):
        events.append(event)

    assert any(isinstance(e, VADStartSpeaking) for e in events)
    assert vad._backend == "torch"


@pytest.mark.asyncio
async def test_silero_process_mocked_onnx(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should detect speech with the ONNX fallback backend."""

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


def test_silero_falls_back_to_onnx_after_torch_failure(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should use ONNX if the torch loader fails on safe architectures."""
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


# ── KrispVAD tests ──────────────────────────────────────────────────


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


# ── TenVAD tests ────────────────────────────────────────────────────


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


# ── FunASROnnxVAD tests ────────────────────────────────────────────


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

    with pytest.raises(RuntimeError, match="FunASR|funasr_onnx"):
        FunASROnnxVAD()


@pytest.mark.asyncio
async def test_funasr_vad_process_streaming_segments(monkeypatch: pytest.MonkeyPatch):
    """FunASR boundaries should map to EasyCat start/stop events."""

    class _FakeWaveform:
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.dtype = None
            self.divisor = None

        def astype(self, dtype: object) -> "_FakeWaveform":
            self.dtype = dtype
            return self

        def __truediv__(self, value: float) -> "_FakeWaveform":
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
    async for event in vad.process(_make_chunk(0)):
        events.append(event)
    # _evaluate_speech latches silence on the first silent frame and
    # emits VADStopSpeaking on the next one, so feed an extra empty
    # frame to drive the state machine past that latch.
    async for event in vad.process(_make_chunk(0)):
        events.append(event)

    assert any(isinstance(e, VADStartSpeaking) for e in events)
    assert any(isinstance(e, VADStopSpeaking) for e in events)


@pytest.mark.asyncio
async def test_funasr_vad_resamples_8k_input(monkeypatch: pytest.MonkeyPatch):
    """FunASR VAD should resample telephony audio to 16 kHz before inference."""

    class _FakeWaveform:
        def astype(self, _dtype: object) -> "_FakeWaveform":
            return self

        def __truediv__(self, _value: float) -> "_FakeWaveform":
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


def test_vad_config_rejects_unknown_backend():
    """VADConfig should reject typo backend strings before probing dependencies."""
    with pytest.raises(ValueError, match="Unknown VAD backend 'silreo'"):
        VADConfig(backend="silreo")


def test_vad_factory_revalidates_mutated_backend():
    """Factory should reject configs mutated after dataclass construction."""
    config = VADConfig()
    config.backend = "silreo"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="Unknown VAD backend 'silreo'"):
        create_vad(config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("min_speech_duration_ms", -1, "min_speech_duration_ms must be non-negative"),
        ("min_silence_duration_ms", -1, "min_silence_duration_ms must be non-negative"),
        ("pre_roll_ms", -1, "pre_roll_ms must be non-negative"),
        ("post_roll_ms", -1, "post_roll_ms must be non-negative"),
        ("min_speech_duration_ms", float("nan"), "min_speech_duration_ms"),
        ("min_silence_duration_ms", float("inf"), "min_silence_duration_ms"),
        ("pre_roll_ms", float("-inf"), "pre_roll_ms"),
        ("sensitivity", -0.1, "sensitivity must be between 0 and 1"),
        ("sensitivity", 1.1, "sensitivity must be between 0 and 1"),
        ("sensitivity", float("nan"), "sensitivity must be a number between 0 and 1"),
        ("sensitivity", float("inf"), "sensitivity must be a number between 0 and 1"),
        ("funasr_chunk_size_ms", 0, "funasr_chunk_size_ms must be a positive integer"),
        (
            "funasr_intra_op_num_threads",
            0,
            "funasr_intra_op_num_threads must be a positive integer",
        ),
    ],
)
def test_vad_config_validates_numeric_knobs(field: str, value: object, message: str):
    kwargs = {field: value}
    with pytest.raises(ValueError, match=message):
        VADConfig(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_speech_duration_ms": -1}, "min_speech_duration_ms must be non-negative"),
        ({"min_silence_duration_ms": -1}, "min_silence_duration_ms must be non-negative"),
        ({"pre_roll_ms": -1}, "pre_roll_ms must be non-negative"),
        ({"post_roll_ms": -1}, "post_roll_ms must be non-negative"),
        ({"min_speech_duration_ms": float("nan")}, "min_speech_duration_ms"),
        ({"min_silence_duration_ms": float("inf")}, "min_silence_duration_ms"),
        ({"post_roll_ms": float("-inf")}, "post_roll_ms"),
        ({"sensitivity": -0.1}, "sensitivity must be between 0 and 1"),
        ({"sensitivity": 1.1}, "sensitivity must be between 0 and 1"),
        ({"sensitivity": float("nan")}, "sensitivity must be a number between 0 and 1"),
        ({"sensitivity": float("inf")}, "sensitivity must be a number between 0 and 1"),
    ],
)
def test_vad_base_configure_validates_numeric_knobs(kwargs: dict[str, object], message: str):
    vad = _VADBase()
    with pytest.raises(ValueError, match=message):
        vad.configure(**kwargs)


def test_vad_factory_no_backends(monkeypatch: pytest.MonkeyPatch):
    """Factory should raise RuntimeError when no backends are available."""

    class _BrokenKrisp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("krisp missing")

    class _BrokenTen:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("ten missing")

    class _BrokenFunASR:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("funasr missing")

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("silero missing")

    monkeypatch.setattr(vad_factory_module, "KrispVAD", _BrokenKrisp)
    monkeypatch.setattr(vad_factory_module, "TenVAD", _BrokenTen)
    monkeypatch.setattr(vad_factory_module, "FunASROnnxVAD", _BrokenFunASR)
    monkeypatch.setattr(vad_factory_module, "SileroVAD", _BrokenSilero)

    with pytest.raises(RuntimeError, match="No VAD backend"):
        create_vad(VADConfig(backend="auto"))


def test_vad_factory_explicit_silero_fails(monkeypatch: pytest.MonkeyPatch):
    """Explicitly requesting silero without torch should raise."""

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("Silero missing")

    monkeypatch.setattr(vad_factory_module, "SileroVAD", _BrokenSilero)
    with pytest.raises(RuntimeError, match="torch|PyTorch|Silero"):
        create_vad(VADConfig(backend="silero"))


def test_vad_factory_explicit_krisp_fails():
    """Explicitly requesting krisp without SDK should raise."""
    with pytest.raises(RuntimeError, match="Krisp"):
        create_vad(VADConfig(backend="krisp"))


def test_vad_factory_explicit_ten_fails(monkeypatch: pytest.MonkeyPatch):
    """Explicitly requesting TEN without package should raise."""

    class _BrokenTen:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("TEN VAD missing")

    monkeypatch.setattr(vad_factory_module, "TenVAD", _BrokenTen)
    with pytest.raises(RuntimeError, match="TEN VAD|ten_vad"):
        create_vad(VADConfig(backend="ten"))


def test_vad_factory_explicit_funasr(monkeypatch: pytest.MonkeyPatch):
    """Explicitly requesting FunASR should instantiate the adapter."""

    class _FakeFunASR:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def configure(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(vad_factory_module, "FunASROnnxVAD", _FakeFunASR)
    vad = create_vad(
        VADConfig(
            backend="funasr",
            funasr_model_dir="local-funasr-model",
            funasr_chunk_size_ms=160,
            funasr_device_id=0,
            funasr_quantize=True,
            funasr_intra_op_num_threads=2,
            funasr_cache_dir="/tmp/funasr-cache",
        )
    )
    assert isinstance(vad, _FakeFunASR)
    assert vad.kwargs == {
        "model_dir": "local-funasr-model",
        "chunk_size_ms": 160,
        "device_id": 0,
        "quantize": True,
        "intra_op_num_threads": 2,
        "cache_dir": "/tmp/funasr-cache",
    }


def test_vad_factory_silero_preferred(monkeypatch: pytest.MonkeyPatch):
    """In auto mode Silero is tried first (permissively licensed, bundled)."""

    class _FakeSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def configure(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(vad_factory_module, "SileroVAD", _FakeSilero)
    vad = create_vad(VADConfig(backend="auto"))
    assert isinstance(vad, _FakeSilero)


def test_vad_factory_funasr_fallback_before_ten(monkeypatch: pytest.MonkeyPatch):
    """In auto mode, FunASR is used before TEN when Silero is unavailable."""

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("silero missing")

    class _FakeFunASR:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def configure(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(vad_factory_module, "SileroVAD", _BrokenSilero)
    monkeypatch.setattr(vad_factory_module, "FunASROnnxVAD", _FakeFunASR)

    class _BrokenTen:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("TEN should not be tried before FunASR")

    monkeypatch.setattr(vad_factory_module, "TenVAD", _BrokenTen)

    vad = create_vad(VADConfig(backend="auto"))
    assert isinstance(vad, _FakeFunASR)


def test_vad_factory_ten_fallback_after_funasr(monkeypatch: pytest.MonkeyPatch):
    """In auto mode, TEN is used when Silero and FunASR are unavailable."""

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("silero missing")

    class _BrokenFunASR:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("funasr missing")

    monkeypatch.setattr(vad_factory_module, "SileroVAD", _BrokenSilero)
    monkeypatch.setattr(vad_factory_module, "FunASROnnxVAD", _BrokenFunASR)

    mock_ten_vad = MagicMock()
    mock_ten_vad.TenVad.return_value = MagicMock()

    import sys

    sys.modules["ten_vad"] = mock_ten_vad

    import types

    fake_numpy = types.SimpleNamespace(int16="int16", frombuffer=lambda data, dtype: data)
    sys.modules["numpy"] = fake_numpy

    try:
        vad = create_vad(VADConfig(backend="auto"))
        assert isinstance(vad, TenVAD)
        assert abs(vad._threshold - 0.6) < 1e-9
    finally:
        del sys.modules["ten_vad"]
        del sys.modules["numpy"]


def test_vad_factory_ten_respects_explicit_sensitivity(monkeypatch: pytest.MonkeyPatch):
    """TEN should keep using caller-provided sensitivity when one is set."""

    mock_ten_vad = MagicMock()
    mock_ten_vad.TenVad.return_value = MagicMock()

    import sys

    sys.modules["ten_vad"] = mock_ten_vad

    import types

    fake_numpy = types.SimpleNamespace(int16="int16", frombuffer=lambda data, dtype: data)
    sys.modules["numpy"] = fake_numpy

    try:
        vad = create_vad(VADConfig(backend="ten", sensitivity=0.7))
        assert isinstance(vad, TenVAD)
        assert abs(vad._threshold - 0.3) < 1e-9
    finally:
        del sys.modules["ten_vad"]
        del sys.modules["numpy"]


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
