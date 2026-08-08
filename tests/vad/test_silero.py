"""VAD tests."""

from __future__ import annotations

import os
import select
import struct
from unittest.mock import MagicMock

import pytest

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
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


def test_silero_onnx_session_is_cached_while_recurrent_state_is_not(
    monkeypatch: pytest.MonkeyPatch,
):
    numpy = pytest.importorskip("numpy")

    class _SessionOptions:
        inter_op_num_threads = 0
        intra_op_num_threads = 0

    class _FakeOnnxRuntime:
        SessionOptions = _SessionOptions

        def __init__(self) -> None:
            self.sessions: list[object] = []

        def get_available_providers(self) -> list[str]:
            return ["CPUExecutionProvider"]

        def InferenceSession(self, *_args, **_kwargs):
            session = object()
            self.sessions.append(session)
            return session

    runtime = _FakeOnnxRuntime()
    monkeypatch.setattr(
        vad_silero_module,
        "require_module",
        lambda name, **_kwargs: numpy if name == "numpy" else runtime,
    )
    monkeypatch.setattr(vad_silero_module, "_ONNX_SESSION_CACHE", {})

    first = vad_silero_module._SileroOnnxModel("model.onnx")
    second = vad_silero_module._SileroOnnxModel("model.onnx")

    assert len(runtime.sessions) == 1
    assert first._session is second._session
    assert first._state is not second._state
    first.close()
    assert second._session is runtime.sessions[0]
    entry = next(iter(vad_silero_module._ONNX_SESSION_CACHE.values()))
    assert entry.owners == 1
    second.close()
    assert vad_silero_module._ONNX_SESSION_CACHE == {}


def test_silero_onnx_cache_releases_failed_model_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenNumpy:
        float32 = object()

        @staticmethod
        def zeros(*_args, **_kwargs):
            raise RuntimeError("state allocation failed")

    class _SessionOptions:
        inter_op_num_threads = 0
        intra_op_num_threads = 0

    class _FakeOnnxRuntime:
        SessionOptions = _SessionOptions

        @staticmethod
        def get_available_providers() -> list[str]:
            return ["CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(*_args, **_kwargs):
            return object()

    monkeypatch.setattr(
        vad_silero_module,
        "require_module",
        lambda name, **_kwargs: _BrokenNumpy if name == "numpy" else _FakeOnnxRuntime,
    )
    monkeypatch.setattr(vad_silero_module, "_ONNX_SESSION_CACHE", {})

    with pytest.raises(RuntimeError, match="state allocation failed"):
        vad_silero_module._SileroOnnxModel("model.onnx")

    assert vad_silero_module._ONNX_SESSION_CACHE == {}


@pytest.mark.serial
@pytest.mark.timeout(0)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_silero_cache_lock_is_reset_after_fork() -> None:
    read_fd, write_fd = os.pipe()
    vad_silero_module._ONNX_SESSION_CACHE_LOCK.acquire()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            with vad_silero_module._ONNX_SESSION_CACHE_LOCK:
                os.write(write_fd, b"ok")
        finally:
            os._exit(0)

    os.close(write_fd)
    vad_silero_module._ONNX_SESSION_CACHE_LOCK.release()
    try:
        ready, _, _ = select.select([read_fd], [], [], 2.0)
        assert ready and os.read(read_fd, 2) == b"ok"
    finally:
        os.close(read_fd)
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == 0:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


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
    # RecursionError can occur in numpy 2.x under pytest's import hooks.
    try:
        import numpy  # noqa: F401
    except (ImportError, RecursionError):
        pytest.skip("numpy not importable")

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
async def test_silero_burst_debounce_uses_consumed_audio_time(
    monkeypatch: pytest.MonkeyPatch,
):
    """A buffered second of speech must trigger without wall-clock sleeps."""
    try:
        import numpy  # noqa: F401
    except (ImportError, RecursionError):
        pytest.skip("numpy not importable")

    class _SpeechModel:
        def predict(self, samples: list[float], sample_rate: int) -> float:
            assert len(samples) == 512
            assert sample_rate == 16000
            return 0.9

        def reset_states(self) -> None:
            pass

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _SpeechModel()
        self._backend = "onnx"

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)

    vad = SileroVAD()
    vad.configure(min_speech_duration_ms=250, min_silence_duration_ms=150)
    # 32 x 32 ms inference frames are delivered in one buffered chunk and
    # processed far faster than realtime.
    chunk = _make_chunk(1000, n_samples=512 * 32)

    events = [event async for event in vad.process(chunk)]

    assert [type(event) for event in events] == [VADStartSpeaking]
    assert vad._audio_time_s == pytest.approx(1.024)


def test_vad_reset_restarts_audio_position_clock(monkeypatch: pytest.MonkeyPatch):
    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = MagicMock()
        self._backend = "onnx"

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)
    vad = SileroVAD()
    vad._advance_audio_time(0.5)

    vad.reset()

    assert vad._audio_time_s == 0.0


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
    # RecursionError can occur in numpy 2.x under pytest's import hooks.
    try:
        import numpy  # noqa: F401
    except (ImportError, RecursionError):
        pytest.skip("numpy not importable")
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


@pytest.mark.asyncio
async def test_silero_preserves_stereo_frames_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
):
    """A split interleaved frame must be downmixed only after reassembly."""

    class _FakeOnnxModel:
        def reset_states(self) -> None:
            pass

    def _load_onnx_model(self: SileroVAD) -> None:
        self._model = _FakeOnnxModel()
        self._backend = "onnx"

    monkeypatch.setattr(vad_silero_module, "_silero_backend_candidates", lambda: ("onnx",))
    monkeypatch.setattr(SileroVAD, "_load_onnx_model", _load_onnx_model)
    monkeypatch.setattr(vad_silero_module, "require_module", lambda *_args, **_kwargs: object())
    vad = SileroVAD()
    fmt = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
    data = struct.pack("<6h", 100, 300, 1_000, 2_000, 3_000, 4_000)

    async for _ in vad.process(AudioChunk(data=data[:6], format=fmt)):
        pass
    async for _ in vad.process(AudioChunk(data=data[6:], format=fmt)):
        pass

    assert vad._buffer == struct.pack("<3h", 200, 1_500, 3_500)


@pytest.mark.asyncio
async def test_silero_discards_stale_remainder_on_rate_change(
    monkeypatch: pytest.MonkeyPatch,
):
    """A mid-stream 8k<->16k switch must not garble a boundary frame.

    The sub-frame remainder left over from the 8 kHz chunk is tagged with the
    old rate; when a 16 kHz chunk arrives it must be dropped rather than
    prepended and sliced at the new frame size (which would contaminate one
    frame with stale 8 kHz samples while keeping the correct length).
    """
    try:
        import numpy  # noqa: F401
    except (ImportError, RecursionError):
        pytest.skip("numpy not importable")

    recorded: list[tuple[int, frozenset[float]]] = []

    class _FakeOnnxModel:
        def predict(self, samples: object, sample_rate: int) -> float:
            assert len(samples) == vad_silero_module._SILERO_FRAME_SAMPLES_AT[sample_rate]
            recorded.append((sample_rate, frozenset(float(s) for s in samples)))
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

    # 356 samples at 8 kHz => one 256-sample frame consumed, 100-sample (200
    # byte) remainder retained and tagged as 8 kHz.
    chunk8k = AudioChunk(data=struct.pack("<356h", *([1000] * 356)), format=PCM16_MONO_8K)
    async for _ in vad.process(chunk8k):
        pass

    # One full 512-sample frame at 16 kHz. The stale 8 kHz remainder must be
    # dropped, not prepended, so this frame contains only 16 kHz samples.
    chunk16k = AudioChunk(data=struct.pack("<512h", *([2000] * 512)), format=PCM16_MONO_16K)
    async for _ in vad.process(chunk16k):
        pass

    stale = 1000 / 32768.0
    fresh = 2000 / 32768.0
    frames_16k = [values for rate, values in recorded if rate == 16000]
    assert frames_16k, "expected at least one 16 kHz frame"
    for values in frames_16k:
        assert stale not in values
        assert values == frozenset({fresh})
