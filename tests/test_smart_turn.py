"""Smart Turn runtime loading tests."""

import asyncio
from types import SimpleNamespace

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.smart_turn import SmartTurnONNX, SmartTurnResult


def test_smart_turn_ensure_loaded_uses_numpy_and_onnxruntime_only(
    monkeypatch,
    tmp_path,
) -> None:
    """Smart Turn should load through NumPy + ONNX Runtime without transformers."""

    requested_modules: list[str] = []
    fake_np = object()

    def make_session_options() -> SimpleNamespace:
        return SimpleNamespace()

    fake_ort = SimpleNamespace(
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL="all"),
        SessionOptions=make_session_options,
        InferenceSession=lambda model_path, sess_options=None: (
            "fake-session",
            model_path,
            sess_options,
        ),
    )

    def fake_require_module(name: str, *, extra: str, purpose: str):
        assert extra == "smart-turn"
        assert purpose == "Smart-turn endpoint detection"
        requested_modules.append(name)
        if name == "numpy":
            return fake_np
        if name == "onnxruntime":
            return fake_ort
        raise AssertionError(f"unexpected module request: {name}")

    created_feature_extractors: list[tuple[object, int]] = []

    def fake_feature_extractor(*, np, chunk_length: int):
        created_feature_extractors.append((np, chunk_length))
        return "fake-feature-extractor"

    monkeypatch.setattr("easycat.smart_turn.require_module", fake_require_module)
    monkeypatch.setattr("easycat.smart_turn._WhisperFeatureExtractorNP", fake_feature_extractor)

    provider = SmartTurnONNX(model_path=str(tmp_path / "smart-turn.onnx"))
    provider._ensure_loaded()

    assert requested_modules == ["numpy", "onnxruntime"]
    assert created_feature_extractors == [(fake_np, 8)]
    assert provider._feature_extractor == "fake-feature-extractor"
    assert provider._session[0] == "fake-session"


def _make_provider_with_probability(probability: float, threshold: float) -> SmartTurnONNX:
    """Build a SmartTurnONNX whose inference returns a fixed probability."""

    provider = SmartTurnONNX(model_path="unused.onnx", threshold=threshold)
    # numpy is only used for length checks / padding in _predict_sync; a slice
    # of length >= max_samples skips padding entirely.
    fake_np = SimpleNamespace(pad=lambda *a, **k: a[0])
    provider._np = fake_np
    provider._feature_extractor = lambda *a, **k: "features"

    fake_output = SimpleNamespace(item=lambda: probability)
    provider._session = SimpleNamespace(run=lambda *a, **k: [[fake_output]])
    return provider


def test_predict_boundary_equal_threshold_is_incomplete() -> None:
    """probability == threshold must classify as incomplete (strict-greater)."""

    provider = _make_provider_with_probability(0.5, threshold=0.5)
    audio = [0.0] * (8 * 16000)

    result = provider._predict_sync(audio)

    assert result.probability == 0.5
    assert result.prediction == 0


def test_predict_above_threshold_is_complete() -> None:
    """probability strictly above threshold classifies as complete."""

    provider = _make_provider_with_probability(0.51, threshold=0.5)
    audio = [0.0] * (8 * 16000)

    result = provider._predict_sync(audio)

    assert result.prediction == 1


def test_chunks_to_float32_16k_truncates_before_concatenate() -> None:
    """Only the trailing model window should be converted/concatenated."""

    from array import array
    from types import SimpleNamespace

    class FakeArray(list[float]):
        def astype(self, _dtype):
            return self

        def __truediv__(self, divisor: float):
            return FakeArray(value / divisor for value in self)

    def frombuffer(data: bytes, *, dtype):
        del dtype
        samples = array("h")
        samples.frombytes(data)
        return FakeArray(float(value) for value in samples)

    fake_np = SimpleNamespace(
        float32=float,
        int16=int,
        frombuffer=frombuffer,
        zeros=lambda size, dtype: FakeArray([0.0] * size),
        concatenate=lambda arrays: FakeArray(
            value for array_values in arrays for value in array_values
        ),
    )

    provider = SmartTurnONNX(model_path="unused.onnx")
    provider._np = fake_np
    chunks = [
        AudioChunk(
            data=array("h", [value] * 16000).tobytes(),
            format=PCM16_MONO_16K,
        )
        for value in range(10)
    ]

    audio = provider._chunks_to_float32_16k(chunks)

    assert len(audio) == 8 * 16000
    assert audio[0] == 2 / 32768.0
    assert audio[-1] == 9 / 32768.0


@pytest.mark.asyncio
async def test_cancelled_detection_keeps_executor_slot_until_worker_finishes() -> None:
    """Cancellation should not let more executor detections pile up."""

    import threading

    class BlockingSmartTurn(SmartTurnONNX):
        def __init__(self) -> None:
            super().__init__(model_path="unused.onnx", timeout_s=0.05)
            self.started = threading.Event()
            self.finish = threading.Event()
            self.calls = 0

        def _detect_sync(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
            self.calls += 1
            self.started.set()
            self.finish.wait(timeout=1)
            return SmartTurnResult(prediction=1, probability=0.9)

    provider = BlockingSmartTurn()
    chunk = AudioChunk(data=b"\0" * 640, format=PCM16_MONO_16K)
    first = asyncio.create_task(provider.detect([chunk]))
    while not provider.started.is_set():
        await asyncio.sleep(0.01)

    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        result = await provider.detect([chunk])

        assert result == SmartTurnResult(prediction=0, probability=0.0)
        assert provider.calls == 1
    finally:
        provider.finish.set()
        await asyncio.sleep(0.05)
