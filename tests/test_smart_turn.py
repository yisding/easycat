"""Smart Turn runtime loading tests."""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.smart_turn import SmartTurnConfig, SmartTurnONNX, SmartTurnResult

_WORKER_EVENT_TIMEOUT = 2.0


def _complete_future(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _route_default_executor(
    loop: asyncio.AbstractEventLoop,
    executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run_in_executor = loop.run_in_executor

    def run_in_executor(
        executor_arg: Any,
        func: Callable[..., Any],
        *args: Any,
    ) -> asyncio.Future[Any]:
        selected = executor if executor_arg is None else executor_arg
        return original_run_in_executor(selected, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", run_in_executor)


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


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "strict"])
def test_smart_turn_config_rejects_invalid_threshold(value: object) -> None:
    with pytest.raises(ValueError, match="threshold"):
        SmartTurnConfig(threshold=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "strict"])
def test_smart_turn_onnx_rejects_invalid_threshold(value: object) -> None:
    with pytest.raises(ValueError, match="threshold"):
        SmartTurnONNX(model_path="unused.onnx", threshold=value)  # type: ignore[arg-type]


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
async def test_cancelled_detection_keeps_executor_slot_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation should not let more executor detections pile up."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    _route_default_executor(loop, executor, monkeypatch)

    class BlockingSmartTurn(SmartTurnONNX):
        def __init__(self) -> None:
            super().__init__(model_path="unused.onnx", timeout_s=0.05)
            self.started: asyncio.Future[None] = loop.create_future()
            self.finish = threading.Event()
            self.finished = threading.Event()
            self.semaphore_released: asyncio.Future[None] = loop.create_future()
            self.calls = 0

        def _detect_sync(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
            self.calls += 1
            loop.call_soon_threadsafe(_complete_future, self.started)
            try:
                self.finish.wait(timeout=1)
                return SmartTurnResult(prediction=1, probability=0.9)
            finally:
                self.finished.set()

        def _release_detect_semaphore(self, future: asyncio.Future[object]) -> None:
            super()._release_detect_semaphore(future)
            _complete_future(self.semaphore_released)

    provider = BlockingSmartTurn()
    chunk = AudioChunk(data=b"\0" * 640, format=PCM16_MONO_16K)
    try:
        first = asyncio.create_task(provider.detect([chunk]))

        try:
            await asyncio.wait_for(provider.started, timeout=_WORKER_EVENT_TIMEOUT)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            result = await provider.detect([chunk])

            assert result == SmartTurnResult(prediction=0, probability=0.0)
            assert provider.calls == 1
        finally:
            provider.finish.set()
            assert provider.finished.wait(timeout=_WORKER_EVENT_TIMEOUT)
            await asyncio.wait_for(
                provider.semaphore_released,
                timeout=_WORKER_EVENT_TIMEOUT,
            )
    finally:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_timed_out_worker_that_raises_is_not_logged_as_unretrieved(
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that raises after the timeout fallback must not leak a future error.

    When detection times out, ``detect`` keeps the executor future alive and
    attaches ``_release_detect_semaphore`` as its done-callback.  If that worker
    later raises, the callback must consume the exception so asyncio does not
    emit "Future exception was never retrieved" when the future is garbage
    collected.
    """

    import gc
    import logging

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    _route_default_executor(loop, executor, monkeypatch)

    class FailingSmartTurn(SmartTurnONNX):
        def __init__(self) -> None:
            super().__init__(model_path="unused.onnx", timeout_s=0.05)
            self.may_raise = threading.Event()
            self.worker_done = threading.Event()
            self.semaphore_released: asyncio.Future[None] = loop.create_future()

        def _detect_sync(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
            # Block past the timeout, then raise so the exception lands on the
            # future only after detect() has returned its fallback result.
            try:
                self.may_raise.wait(timeout=1)
                raise RuntimeError("smart-turn worker boom")
            finally:
                self.worker_done.set()

        def _release_detect_semaphore(self, future: asyncio.Future[object]) -> None:
            super()._release_detect_semaphore(future)
            _complete_future(self.semaphore_released)

    provider = FailingSmartTurn()
    chunk = AudioChunk(data=b"\0" * 640, format=PCM16_MONO_16K)

    # Capture all log records (asyncio logs the unretrieved-future error).
    try:
        with caplog.at_level(logging.DEBUG):
            try:
                result = await provider.detect([chunk])
                # Times out -> fallback to the silence timer (incomplete).
                assert result == SmartTurnResult(prediction=0, probability=0.0)

                # Let the still-running worker raise, then force the future through GC.
                provider.may_raise.set()
                assert provider.worker_done.wait(timeout=_WORKER_EVENT_TIMEOUT)
                await asyncio.wait_for(
                    provider.semaphore_released,
                    timeout=_WORKER_EVENT_TIMEOUT,
                )
                # Drop references and collect so any unretrieved-future log would fire.
                gc.collect()
            finally:
                provider.may_raise.set()
    finally:
        executor.shutdown(wait=True)

    unretrieved = [
        record
        for record in caplog.records
        if "Future exception was never retrieved" in record.getMessage()
    ]
    assert unretrieved == [], f"unexpected unretrieved-future log: {unretrieved}"
