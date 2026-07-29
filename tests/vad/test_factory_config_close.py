"""VAD tests."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from easycat.vad import (
    FunASROnnxVAD,
    SileroVAD,
    TenVAD,
    VADConfig,
    create_vad,
)
from easycat.vad import factory as vad_factory_module
from easycat.vad import silero as vad_silero_module
from easycat.vad._base import _VADBase
from tests.vad._helpers import _assert_extra_hint


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
        ("min_speech_duration_ms", float("nan"), "min_speech_duration_ms"),
        ("min_silence_duration_ms", float("inf"), "min_silence_duration_ms"),
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
        ({"min_speech_duration_ms": float("nan")}, "min_speech_duration_ms"),
        ({"min_silence_duration_ms": float("inf")}, "min_silence_duration_ms"),
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

    with pytest.raises(RuntimeError) as exc_info:
        create_vad(VADConfig(backend="auto"))
    message = str(exc_info.value)
    assert "No VAD backend" in message
    _assert_extra_hint(message, "silero-vad")
    _assert_extra_hint(message, "ten-vad")
    _assert_extra_hint(message, "funasr-vad")
    assert exc_info.value.__notes__ == [
        "Silero: RuntimeError: silero missing",
        "FunASR: RuntimeError: funasr missing",
        "TEN: RuntimeError: ten missing",
        "Krisp: RuntimeError: krisp missing",
    ]
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "krisp missing"


def test_vad_auto_backend_policy_order_is_explicit():
    assert tuple(backend.name for backend in vad_factory_module._AUTO_BACKENDS) == (
        "silero",
        "funasr",
        "ten",
        "krisp",
    )


def test_vad_factory_explicit_silero_fails(monkeypatch: pytest.MonkeyPatch):
    """Explicitly requesting silero with no backend available should raise."""

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
        )
    )
    assert isinstance(vad, _FakeFunASR)
    assert vad.kwargs == {
        "model_dir": "local-funasr-model",
        "chunk_size_ms": 160,
        "device_id": 0,
        "quantize": True,
        "intra_op_num_threads": 2,
        "cache_dir": None,
    }


def test_vad_factory_explicit_funasr_rejects_cache_dir():
    """funasr_cache_dir is obsolete with the in-tree runtime."""
    with pytest.raises(ValueError, match="cache_dir is not supported"):
        create_vad(VADConfig(backend="funasr", funasr_cache_dir="/tmp/funasr-cache"))


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

    monkeypatch.setitem(sys.modules, "ten_vad", mock_ten_vad)

    import types

    fake_numpy = types.SimpleNamespace(int16="int16", frombuffer=lambda data, dtype: data)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    vad = create_vad(VADConfig(backend="auto"))
    assert isinstance(vad, TenVAD)
    assert abs(vad._threshold - 0.6) < 1e-9


def test_vad_factory_ten_respects_explicit_sensitivity(monkeypatch: pytest.MonkeyPatch):
    """TEN should keep using caller-provided sensitivity when one is set."""

    mock_ten_vad = MagicMock()
    mock_ten_vad.TenVad.return_value = MagicMock()

    import sys

    monkeypatch.setitem(sys.modules, "ten_vad", mock_ten_vad)

    import types

    fake_numpy = types.SimpleNamespace(int16="int16", frombuffer=lambda data, dtype: data)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    vad = create_vad(VADConfig(backend="ten", sensitivity=0.7))
    assert isinstance(vad, TenVAD)
    assert abs(vad._threshold - 0.3) < 1e-9


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


def test_silero_close_releases_model(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD.close() drops the model and inner onnxruntime session."""

    class _FakeOnnxModel:
        def __init__(self) -> None:
            self._session = object()
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self._session = None

        def reset_states(self) -> None:
            pass

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _FakeOnnxModel()
        self._backend = "onnx"
        self._torch = None

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()
    inner = vad._model
    assert inner is not None

    vad.close()

    assert inner.closed is True
    assert vad._model is None
    assert vad._torch is None


def test_silero_onnx_model_close_drops_session():
    """_SileroOnnxModel.close() releases the InferenceSession reference."""
    model = vad_silero_module._SileroOnnxModel.__new__(vad_silero_module._SileroOnnxModel)
    model._session = object()
    model.close()
    assert model._session is None


def test_ten_vad_close_releases_handle(monkeypatch: pytest.MonkeyPatch):
    """TenVAD.close() drops the native ten_vad handle."""
    import sys

    mock_ten_vad = MagicMock()
    mock_ten_vad.TenVad.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "ten_vad", mock_ten_vad)
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(int16="int16"))

    vad = TenVAD()
    assert vad._ten_vad is not None
    vad.close()
    assert vad._ten_vad is None
    assert vad._buffer == b""


def test_funasr_vad_close_releases_model(monkeypatch: pytest.MonkeyPatch):
    """FunASROnnxVAD.close() drops the model handle and streaming cache."""

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = object()
        self._model = object()
        self._param_dict = {"in_cache": ["cached"]}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD()
    assert vad._model is not None

    vad.close()

    assert vad._model is None
    assert vad._buffer == b""
    assert vad._param_dict == {"in_cache": []}


@pytest.mark.asyncio
async def test_close_if_supported_invokes_vad_close(monkeypatch: pytest.MonkeyPatch):
    """Session teardown's close_if_supported reaches the VAD close hook."""
    from easycat.runtime.capabilities import close_if_supported

    def _initialize(self: FunASROnnxVAD) -> None:
        self._numpy = object()
        self._model = object()
        self._param_dict = {"in_cache": []}

    monkeypatch.setattr(FunASROnnxVAD, "_initialize", _initialize)

    vad = FunASROnnxVAD()
    assert vad._model is not None

    await close_if_supported(vad)

    assert vad._model is None
