"""VAD tests: Silero, Krisp, factory, and configuration."""

import struct
import types
from unittest.mock import MagicMock

import pytest

from easycat import vad as vad_module
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import VADStartSpeaking
from easycat.vad import (
    KrispVAD,
    SileroVAD,
    TenVAD,
    VADConfig,
    create_vad,
)


def _make_chunk(value: int = 0, n_samples: int = 512) -> AudioChunk:
    """Create a PCM16 chunk filled with the given sample value."""
    data = struct.pack(f"<{n_samples}h", *([value] * n_samples))
    return AudioChunk(data=data, format=PCM16_MONO_16K)


# ── SileroVAD tests ─────────────────────────────────────────────────


def test_silero_backend_candidates_prefer_onnx_on_arm64(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_SILERO_BACKEND", raising=False)
    monkeypatch.setattr(vad_module.platform, "machine", lambda: "aarch64")
    assert vad_module._silero_backend_candidates() == ("onnx",)


def test_silero_backend_candidates_prefer_torch_elsewhere(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_SILERO_BACKEND", raising=False)
    monkeypatch.setattr(vad_module.platform, "machine", lambda: "x86_64")
    assert vad_module._silero_backend_candidates() == ("torch", "onnx")


def test_silero_backend_candidates_respect_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_SILERO_BACKEND", "torch")
    monkeypatch.setattr(vad_module.platform, "machine", lambda: "aarch64")
    assert vad_module._silero_backend_candidates() == ("torch",)


def test_silero_onnx_model_path_uses_bundled_asset():
    model_path = vad_module._silero_onnx_model_path()
    assert model_path.endswith("src/easycat/models/silero_vad.onnx")


def test_silero_fails_when_only_torch_backend_is_allowed(monkeypatch: pytest.MonkeyPatch):
    """SileroVAD should raise RuntimeError if torch is unavailable and ONNX is disabled."""
    monkeypatch.setattr(vad_module, "_silero_backend_candidates", lambda: ("torch",))

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "torch":
            raise ImportError("Silero VAD requires the torch package.")
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_module, "require_module", _require_module)

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

    monkeypatch.setattr(vad_module, "_silero_backend_candidates", lambda: ("torch",))
    monkeypatch.setattr(vad_module, "require_module", _require_module)

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

    monkeypatch.setattr(vad_module, "_silero_backend_candidates", lambda: ("onnx",))
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
    monkeypatch.setattr(vad_module, "_silero_backend_candidates", lambda: ("torch", "onnx"))

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
        vad_module,
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

    monkeypatch.setattr(vad_module, "require_module", _require_module)
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


def test_vad_factory_no_backends(monkeypatch: pytest.MonkeyPatch):
    """Factory should raise RuntimeError when no backends are available."""

    class _BrokenKrisp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("krisp missing")

    class _BrokenTen:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("ten missing")

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("silero missing")

    monkeypatch.setattr(vad_module, "KrispVAD", _BrokenKrisp)
    monkeypatch.setattr(vad_module, "TenVAD", _BrokenTen)
    monkeypatch.setattr(vad_module, "SileroVAD", _BrokenSilero)

    with pytest.raises(RuntimeError, match="No VAD backend"):
        create_vad(VADConfig(backend="auto"))


def test_vad_factory_explicit_silero_fails(monkeypatch: pytest.MonkeyPatch):
    """Explicitly requesting silero without torch should raise."""

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("Silero missing")

    monkeypatch.setattr(vad_module, "SileroVAD", _BrokenSilero)
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

    monkeypatch.setattr(vad_module, "TenVAD", _BrokenTen)
    with pytest.raises(RuntimeError, match="TEN VAD|ten_vad"):
        create_vad(VADConfig(backend="ten"))


def test_vad_factory_silero_preferred(monkeypatch: pytest.MonkeyPatch):
    """In auto mode Silero is tried first (permissively licensed, bundled)."""

    class _FakeSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def configure(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(vad_module, "SileroVAD", _FakeSilero)
    vad = create_vad(VADConfig(backend="auto"))
    assert isinstance(vad, _FakeSilero)


def test_vad_factory_ten_fallback_before_krisp(monkeypatch: pytest.MonkeyPatch):
    """In auto mode, TEN is used when Silero is unavailable but ten_vad is installed."""

    class _BrokenSilero:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("silero missing")

    monkeypatch.setattr(vad_module, "SileroVAD", _BrokenSilero)

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
