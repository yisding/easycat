"""Smart Turn runtime loading tests."""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.audio_format import PCM16_MONO_16K, PCM16_MONO_24K, AudioChunk, AudioFormat
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
    monkeypatch.setattr("easycat.smart_turn._intra_op_thread_count", lambda: 3)

    provider = SmartTurnONNX(model_path=str(tmp_path / "smart-turn.onnx"))
    provider._ensure_loaded()

    assert requested_modules == ["numpy", "onnxruntime"]
    assert created_feature_extractors == [(fake_np, 8)]
    assert provider._feature_extractor == "fake-feature-extractor"
    assert provider._session[0] == "fake-session"
    assert provider._session[2].inter_op_num_threads == 1
    assert provider._session[2].intra_op_num_threads == 3


@pytest.mark.parametrize(
    ("available", "expected"),
    [(1, 1), (2, 2), (4, 4), (8, 4)],
)
def test_intra_op_thread_count_respects_affinity_and_cap(
    available: int,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.smart_turn as smart_turn

    monkeypatch.setattr(
        smart_turn.os,
        "sched_getaffinity",
        lambda _pid: set(range(available)),
        raising=False,
    )
    monkeypatch.setattr(smart_turn, "_cgroup_cpu_count", lambda: None)

    assert smart_turn._intra_op_thread_count() == expected


@pytest.mark.parametrize(
    ("quota", "expected"),
    [(1, 1), (2, 2), (8, 4), (None, 4)],
)
def test_intra_op_thread_count_respects_cgroup_quota(
    quota: int | None,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.smart_turn as smart_turn

    monkeypatch.setattr(
        smart_turn.os,
        "sched_getaffinity",
        lambda _pid: set(range(16)),
        raising=False,
    )
    monkeypatch.setattr(smart_turn, "_cgroup_cpu_count", lambda: quota)

    assert smart_turn._intra_op_thread_count() == expected


def test_cgroup_cpu_count_reads_v2_quota(tmp_path: Path) -> None:
    from easycat.smart_turn import _cgroup_cpu_count

    (tmp_path / "cpu.max").write_text("150000 100000\n")

    assert _cgroup_cpu_count(tmp_path) == 2


def test_cgroup_cpu_count_reads_nested_v2_quota(tmp_path: Path) -> None:
    from easycat.smart_turn import _cgroup_cpu_count

    cgroup_file = tmp_path / "self.cgroup"
    cgroup_file.write_text("0::/system.slice/easycat.service\n")
    (tmp_path / "cpu.max").write_text("max 100000\n")
    service_root = tmp_path / "system.slice" / "easycat.service"
    service_root.mkdir(parents=True)
    (service_root / "cpu.max").write_text("50000 100000\n")

    assert _cgroup_cpu_count(tmp_path, cgroup_file) == 1


def test_cgroup_cpu_count_reads_v1_quota(tmp_path: Path) -> None:
    from easycat.smart_turn import _cgroup_cpu_count

    cpu_root = tmp_path / "cpu"
    cpu_root.mkdir()
    (cpu_root / "cpu.cfs_quota_us").write_text("250000\n")
    (cpu_root / "cpu.cfs_period_us").write_text("100000\n")

    assert _cgroup_cpu_count(tmp_path) == 3


def test_cgroup_cpu_count_reads_nested_v1_cpu_controller_quota(tmp_path: Path) -> None:
    from easycat.smart_turn import _cgroup_cpu_count

    cgroup_file = tmp_path / "self.cgroup"
    cgroup_file.write_text("7:cpu,cpuacct:/system.slice/easycat.service\n")
    service_root = tmp_path / "cpu,cpuacct" / "system.slice" / "easycat.service"
    service_root.mkdir(parents=True)
    (service_root / "cpu.cfs_quota_us").write_text("150000\n")
    (service_root / "cpu.cfs_period_us").write_text("100000\n")

    assert _cgroup_cpu_count(tmp_path, cgroup_file) == 2


def test_cgroup_cpu_count_uses_tightest_ancestor_quota(tmp_path: Path) -> None:
    from easycat.smart_turn import _cgroup_cpu_count

    cgroup_file = tmp_path / "self.cgroup"
    cgroup_file.write_text("0::/parent/child\n")
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "cpu.max").write_text("100000 100000\n")
    (child / "cpu.max").write_text("400000 100000\n")

    assert _cgroup_cpu_count(tmp_path, cgroup_file) == 1


@pytest.mark.parametrize("quota", ["max 100000\n", "-1 100000\n", "invalid\n"])
def test_cgroup_cpu_count_ignores_unbounded_or_invalid_v2_quota(
    quota: str,
    tmp_path: Path,
) -> None:
    from easycat.smart_turn import _cgroup_cpu_count

    (tmp_path / "cpu.max").write_text(quota)

    assert _cgroup_cpu_count(tmp_path) is None


def test_single_thread_mel_contraction_matches_dot() -> None:
    np = pytest.importorskip("numpy")

    from easycat.smart_turn import _spectrogram

    class DotReference:
        def __getattr__(self, name: str) -> Any:
            return getattr(np, name)

        def einsum(
            self,
            subscripts: str,
            left: Any,
            right: Any,
            *,
            optimize: bool,
        ) -> Any:
            assert subscripts == "ij,jk->ik"
            assert optimize is False
            return np.dot(left, right)

    rng = np.random.default_rng(7)
    waveform = rng.normal(size=1600).astype(np.float32)
    window = np.hanning(400).astype(np.float64)
    mel_filters = np.abs(rng.normal(size=(201, 80))).astype(np.float64)

    actual = _spectrogram(
        waveform,
        np=np,
        window=window,
        frame_length=400,
        hop_length=160,
        mel_filters=mel_filters,
    )
    expected = _spectrogram(
        waveform,
        np=DotReference(),
        window=window,
        frame_length=400,
        hop_length=160,
        mel_filters=mel_filters,
    )

    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)


def test_batched_spectrogram_matches_per_frame_reference() -> None:
    np = pytest.importorskip("numpy")

    from easycat.smart_turn import _spectrogram

    rng = np.random.default_rng(11)
    waveform = rng.normal(size=1600).astype(np.float32)
    window = np.hanning(400).astype(np.float64)
    mel_filters = np.abs(rng.normal(size=(201, 80))).astype(np.float64)

    actual = _spectrogram(
        waveform,
        np=np,
        window=window,
        frame_length=400,
        hop_length=160,
        mel_filters=mel_filters,
    )

    padded = np.pad(waveform, (200, 200), mode="reflect").astype(np.float64)
    num_frames = int(1 + np.floor((padded.size - 400) / 160))
    reference_fft = np.empty((num_frames, 201), dtype=np.complex64)
    for frame_index in range(num_frames):
        start = frame_index * 160
        reference_fft[frame_index] = np.fft.rfft(padded[start : start + 400] * window)
    power = (np.abs(reference_fft, dtype=np.float64) ** 2.0).T
    expected = np.maximum(
        1e-10,
        np.einsum("ij,jk->ik", mel_filters.T, power, optimize=False),
    )
    expected = np.log10(expected).astype(np.float32)

    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)


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


@pytest.mark.asyncio
async def test_smart_turn_warmup_loads_and_runs_dummy_inference() -> None:
    """warmup() loads the model and runs one zeroed-audio inference up front."""
    provider = SmartTurnONNX(model_path="unused.onnx")
    loaded = {"called": False}
    predicted: list[int] = []

    def _fake_ensure_loaded() -> None:
        loaded["called"] = True
        # warmup builds the zeroed audio array off ``self._np`` after load.
        provider._np = SimpleNamespace(
            zeros=lambda n, dtype=None: [0.0] * n,
            float32=float,
        )

    def _fake_predict_sync(audio: Any) -> SmartTurnResult:
        predicted.append(len(audio))
        return SmartTurnResult(prediction=0, probability=0.0)

    provider._ensure_loaded = _fake_ensure_loaded  # type: ignore[method-assign]
    provider._predict_sync = _fake_predict_sync  # type: ignore[method-assign]

    await provider.warmup()

    assert loaded["called"] is True
    assert predicted == [provider._max_audio_samples]


@pytest.mark.asyncio
async def test_smart_turn_warmup_swallows_load_errors() -> None:
    """A model-load failure during warmup must not propagate."""
    provider = SmartTurnONNX(model_path="unused.onnx")

    def _boom() -> None:
        raise RuntimeError("load failed")

    provider._ensure_loaded = _boom  # type: ignore[method-assign]

    # Returns cleanly despite the load raising.
    await provider.warmup()


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


def test_chunks_to_float32_16k_batches_same_rate_resampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default 400-frame window should cross the filtered resampler once."""

    from array import array
    from types import SimpleNamespace

    resample_calls = 0

    class FakeArray(list[float]):
        def astype(self, _dtype):
            return self

        def __mul__(self, factor: float):
            return FakeArray(value * factor for value in self)

        def __truediv__(self, divisor: float):
            return FakeArray(value / divisor for value in self)

    def frombuffer(data: bytes, *, dtype):
        del dtype
        samples = array("h")
        samples.frombytes(data)
        return FakeArray(float(value) for value in samples)

    def filtered_resample(data: bytes, from_rate: int, to_rate: int) -> bytes:
        nonlocal resample_calls
        assert from_rate == 24_000
        assert to_rate == 16_000
        resample_calls += 1
        return array("h", [0] * (len(data) // 3)).tobytes()

    fake_np = SimpleNamespace(
        float32=float,
        int16=int,
        concatenate=lambda arrays: FakeArray(
            value for array_values in arrays for value in array_values
        ),
        frombuffer=frombuffer,
        zeros=lambda size, dtype: FakeArray([0.0] * size),
    )
    monkeypatch.setattr("easycat.smart_turn.resample", filtered_resample)

    provider = SmartTurnONNX(model_path="unused.onnx")
    provider._np = fake_np
    chunks = [
        AudioChunk(
            data=array("h", [0] * 480).tobytes(),
            format=PCM16_MONO_24K,
        )
        for _ in range(400)
    ]

    audio = provider._chunks_to_float32_16k(chunks)

    assert len(audio) == 8 * 16000
    assert resample_calls == 1


def test_chunks_to_float32_16k_downmixes_multichannel_frames() -> None:
    from array import array

    np = pytest.importorskip("numpy")
    provider = SmartTurnONNX(model_path="unused.onnx")
    provider._np = np
    stereo = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
    chunk = AudioChunk(
        data=array("h", [1000, 3000, 2000, 4000]).tobytes(),
        format=stereo,
    )

    audio = provider._chunks_to_float32_16k([chunk])

    assert audio.tolist() == pytest.approx([2000 / 32768.0, 3000 / 32768.0])


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
async def test_executor_scheduling_failure_releases_detection_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed executor submission must not wedge later endpoint detection."""

    provider = SmartTurnONNX(model_path="unused.onnx")
    loop = asyncio.get_running_loop()

    def _reject_submission(*_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        raise RuntimeError("default executor is shut down")

    monkeypatch.setattr(loop, "run_in_executor", _reject_submission)

    with pytest.raises(RuntimeError, match="default executor is shut down"):
        await provider.detect([])

    # Use the public semaphore operation rather than its private value: it
    # proves a following detection can acquire the single-flight slot.
    await asyncio.wait_for(provider._detect_semaphore.acquire(), timeout=0.1)
    provider._detect_semaphore.release()


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
