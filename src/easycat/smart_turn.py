"""Smart turn-taking endpoint detection.

A smart-turn provider classifies accumulated turn audio as "complete"
(user finished their turn) or "incomplete" (user is still talking/thinking).
This enables faster turn transitions when the model is confident the user
is done, while falling back to the silence timer for uncertain cases.

SmartTurnONNX wraps the smart-turn ONNX model (~8 MB quantized Whisper-Tiny
classifier). ONNX inference is synchronous, so it runs in a thread executor
to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from easycat._audio_utils import resample, to_mono
from easycat._extras import require_module
from easycat._numeric import is_finite_number
from easycat._smart_turn_features import (
    _create_triangular_filter_bank as _create_triangular_filter_bank,
)
from easycat._smart_turn_features import _hertz_to_mel as _hertz_to_mel
from easycat._smart_turn_features import _mel_filter_bank as _mel_filter_bank
from easycat._smart_turn_features import _mel_to_hertz as _mel_to_hertz
from easycat._smart_turn_features import _spectrogram as _spectrogram
from easycat._smart_turn_features import _WhisperFeatureExtractorNP as _WhisperFeatureExtractorNP
from easycat._smart_turn_features import _window_function as _window_function
from easycat._smart_turn_resources import _CGROUP_ROOT as _CGROUP_ROOT
from easycat._smart_turn_resources import _MAX_INTRA_OP_THREADS as _MAX_INTRA_OP_THREADS
from easycat._smart_turn_resources import _SELF_CGROUP as _SELF_CGROUP
from easycat._smart_turn_resources import _cgroup_ancestors as _cgroup_ancestors
from easycat._smart_turn_resources import _cgroup_cpu_count as _cgroup_cpu_count
from easycat._smart_turn_resources import _cgroup_path as _cgroup_path
from easycat._smart_turn_resources import _current_cgroup_paths as _current_cgroup_paths
from easycat._smart_turn_resources import (
    _intra_op_thread_count as _resource_intra_op_thread_count,
)
from easycat._smart_turn_resources import _quota_cpu_count as _quota_cpu_count
from easycat._smart_turn_resources import _quota_from_paths as _quota_from_paths
from easycat.audio_format import AudioChunk

_BUNDLED_MODEL = str(Path(__file__).parent / "models" / "smart-turn-v3.2-cpu.onnx")

logger = logging.getLogger(__name__)


def _intra_op_thread_count() -> int:
    """Size ONNX's inference pool to the worker's available CPU set.

    ONNX Runtime's automatic pool can oversubscribe constrained containers,
    producing large endpointing-latency spikes.  Four threads is the measured
    latency optimum for the bundled model; smaller workers use only the CPUs
    available through their affinity mask (or ``os.cpu_count`` off Linux) and
    cgroup CPU bandwidth quota.
    """
    return _resource_intra_op_thread_count(
        os_module=os,
        cgroup_cpu_count=_cgroup_cpu_count,
        max_threads=_MAX_INTRA_OP_THREADS,
    )


def _validate_probability_threshold(name: str, value: float) -> float:
    """Validate a completion probability threshold in the inclusive 0..1 range."""
    if not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a number between 0 and 1")
    threshold = float(value)
    if not 0 <= threshold <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return threshold


def _finite_positive_duration(name: str, value: object) -> float:
    """Validate a public duration without accepting booleans or non-finite values."""
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


@dataclass(frozen=True)
class SmartTurnResult:
    """Result from smart-turn detection."""

    prediction: int  # 1 = complete (turn ended), 0 = incomplete
    probability: float  # sigmoid probability of completion (0.0–1.0)


@runtime_checkable
class SmartTurnProvider(Protocol):
    """Protocol for smart-turn providers."""

    async def detect(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        """Classify accumulated turn audio as complete or incomplete."""
        ...


# ── Smart-Turn ONNX implementation ────────────────────────────────


class SmartTurnONNX:
    """Smart-turn provider using the ONNX model.

    Lazy-loads the ONNX model and a NumPy Whisper frontend on first use.
    Inference runs in a thread executor because ONNX Runtime is synchronous.

    Requires: numpy, onnxruntime
    """

    _SAMPLE_RATE = 16000
    _MAX_AUDIO_SECONDS = 8.0
    _MAX_AUDIO_SAMPLES = int(_SAMPLE_RATE * _MAX_AUDIO_SECONDS)

    def __init__(
        self,
        model_path: str,
        threshold: float = 0.5,
        *,
        timeout_s: float = 2.0,
        max_audio_seconds: float = _MAX_AUDIO_SECONDS,
    ) -> None:
        timeout_s = _finite_positive_duration("timeout_s", timeout_s)
        max_audio_seconds = _finite_positive_duration("max_audio_seconds", max_audio_seconds)
        max_audio_samples = self._SAMPLE_RATE * max_audio_seconds
        if (
            not math.isfinite(max_audio_samples)
            or max_audio_samples < 1
            or max_audio_samples > sys.maxsize
        ):
            raise ValueError(
                "max_audio_seconds must produce a positive, representable audio window"
            )

        self._model_path = model_path
        self._threshold = _validate_probability_threshold("threshold", threshold)
        self._timeout_s = timeout_s
        self._max_audio_samples = int(max_audio_samples)
        self._session: Any = None  # ort.InferenceSession (lazy)
        self._feature_extractor: Any = None  # NumPy Whisper frontend (lazy)
        self._np: Any = None  # numpy module (lazy)
        self._detect_semaphore = asyncio.Semaphore(1)

    async def warmup(self) -> None:
        """Load the ONNX model and run one dummy inference up front.

        The first inference pays the model-load and onnxruntime
        graph-allocation cost; running it on zeroed audio at startup keeps
        that cold-start cost off the first real endpoint decision.  Inference
        is synchronous, so it runs in a thread executor.  All failures are
        swallowed — ``WarmupRunner`` re-raises, so a load error here must not
        abort ``Session.start()``.
        """
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._warmup_sync)
        except Exception as exc:
            logger.debug("Smart-turn warmup skipped: %s", exc)

    def _warmup_sync(self) -> None:
        """Load the model and run one zeroed-audio inference.  Runs in a thread."""
        self._ensure_loaded()
        audio = self._np.zeros(self._max_audio_samples, dtype=self._np.float32)
        self._predict_sync(audio)

    def _ensure_loaded(self) -> None:
        """Lazy-load model and feature extractor on first inference."""
        if self._session is not None:
            return

        self._np = require_module(
            "numpy",
            extra="smart-turn",
            purpose="Smart-turn endpoint detection",
        )
        ort = require_module(
            "onnxruntime",
            extra="smart-turn",
            purpose="Smart-turn endpoint detection",
        )

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = _intra_op_thread_count()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(self._model_path, sess_options=so)

        self._feature_extractor = _WhisperFeatureExtractorNP(np=self._np, chunk_length=8)
        logger.info("Smart-turn model loaded from %s", self._model_path)

    def _chunks_to_float32_16k(self, chunks: list[AudioChunk]) -> Any:
        """Convert only the trailing model window to a float32 16 kHz array."""
        np = self._np
        if not chunks:
            return np.zeros(0, dtype=np.float32)

        remaining_samples = self._max_audio_samples
        reversed_chunks: list[tuple[bytes, int]] = []

        for chunk in reversed(chunks):
            if remaining_samples <= 0:
                break

            data = chunk.data
            source_rate = chunk.format.sample_rate
            frame_size = chunk.format.frame_size
            if frame_size <= 0 or source_rate <= 0:
                continue

            source_sample_budget = max(1, int((remaining_samples * source_rate + 15999) // 16000))
            source_samples = len(data) // frame_size
            if source_samples > source_sample_budget:
                data = data[-source_sample_budget * frame_size :]
                source_samples = source_sample_budget
            if not data or source_samples <= 0:
                continue

            if chunk.format.channels > 1:
                data = to_mono(data, chunk.format.channels)
            reversed_chunks.append((data, source_rate))
            converted_samples = int(source_samples * 16000 / source_rate)
            remaining_samples -= min(remaining_samples, converted_samples)

        if not reversed_chunks:
            return np.zeros(0, dtype=np.float32)

        # The common 8-second / 20-ms workload contains ~400 chunks. Group
        # adjacent chunks with the same source rate and cross the Python/NumPy
        # boundary once per rate segment rather than once per frame.
        converted_groups: list[Any] = []
        grouped_bytes: list[bytes] = []
        grouped_rate: int | None = None

        def convert_group() -> None:
            if grouped_rate is None or not grouped_bytes:
                return
            payload = b"".join(grouped_bytes)
            if grouped_rate != 16000:
                payload = resample(payload, grouped_rate, 16000)
            samples = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
            converted_groups.append(samples)

        for data, source_rate in reversed(reversed_chunks):
            if grouped_rate is not None and source_rate != grouped_rate:
                convert_group()
                grouped_bytes = []
            grouped_rate = source_rate
            grouped_bytes.append(data)
        convert_group()

        audio = np.concatenate(converted_groups)
        if len(audio) > self._max_audio_samples:
            audio = audio[-self._max_audio_samples :]
        return audio

    def _predict_sync(self, audio_array: Any) -> SmartTurnResult:
        """Run ONNX inference synchronously.  Called from thread executor."""
        np = self._np
        max_samples = self._max_audio_samples

        if len(audio_array) > max_samples:
            audio_array = audio_array[-max_samples:]
        elif len(audio_array) < max_samples:
            padding = max_samples - len(audio_array)
            audio_array = np.pad(audio_array, (0, padding), mode="constant", constant_values=0)

        input_features = self._feature_extractor(
            audio_array,
            sampling_rate=16000,
            do_normalize=True,
        )

        outputs = self._session.run(None, {"input_features": input_features})
        probability = outputs[0][0].item()
        # Strict-greater boundary: probability *equal* to the threshold counts
        # as incomplete (prediction=0).  With the default threshold of 0.5 a
        # probability of exactly 0.5 therefore stays incomplete.  Callers that
        # need a different decision point can either set ``threshold`` here or,
        # at the TurnManager layer, decide on ``probability`` directly.
        prediction = 1 if probability > self._threshold else 0

        return SmartTurnResult(prediction=prediction, probability=probability)

    def _detect_sync(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        """Load, preprocess, and infer synchronously inside the bounded worker."""
        self._ensure_loaded()
        audio_array = self._chunks_to_float32_16k(audio_chunks)

        if len(audio_array) == 0:
            return SmartTurnResult(prediction=0, probability=0.0)

        return self._predict_sync(audio_array)

    def _release_detect_semaphore(self, future: asyncio.Future[Any]) -> None:
        # Consume any exception the worker raised after the coroutine had already
        # returned its timeout/cancel fallback.  Retrieving ``future.exception()``
        # clears asyncio's ``_log_traceback`` flag, so a late failure no longer
        # surfaces as a "Future exception was never retrieved" message at GC.
        # Guard ``cancelled()`` because ``exception()`` raises ``CancelledError``
        # on a cancelled future.
        if not future.cancelled():
            exc = future.exception()
            if exc is not None:
                logger.debug("Smart-turn worker failed after timeout/cancel: %r", exc)
        self._detect_semaphore.release()

    async def detect(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        """Classify accumulated turn audio as complete or incomplete.

        Only one smart-turn job may run at a time.  Cancellation or timeout of
        this coroutine does not stop an already-running executor thread, so the
        semaphore is released from the executor future's completion callback when
        that happens.  This prevents repeated VAD stop/start cycles from piling
        up costly ONNX jobs.
        """
        try:
            await asyncio.wait_for(self._detect_semaphore.acquire(), timeout=self._timeout_s)
        except TimeoutError:
            logger.warning("Smart-turn detection skipped while another detection is still running")
            return SmartTurnResult(prediction=0, probability=0.0)

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(None, self._detect_sync, list(audio_chunks))
        except BaseException:
            # Scheduling can fail synchronously (for example when the default
            # executor has already shut down).  The slot was acquired above,
            # but the normal try/finally below has not started yet, so release
            # it here rather than permanently skipping every later detection.
            self._detect_semaphore.release()
            raise
        release_now = True
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout_s)
        except TimeoutError:
            logger.warning("Smart-turn detection timed out after %.2fs", self._timeout_s)
            release_now = False
            future.add_done_callback(self._release_detect_semaphore)
            return SmartTurnResult(prediction=0, probability=0.0)
        except asyncio.CancelledError:
            release_now = False
            future.add_done_callback(self._release_detect_semaphore)
            raise
        finally:
            if release_now:
                self._detect_semaphore.release()


# ── Configuration & factory ────────────────────────────────────────


@dataclass
class SmartTurnConfig:
    """Configuration for smart-turn endpoint detection.

    Set enabled=True to activate smart-turn.  The bundled 8 MB quantized
    ONNX model is used by default; override *model_path* to use a custom one.
    When disabled (default), TurnManager uses its existing silence timeout.
    """

    enabled: bool = False
    model_path: str = field(default_factory=lambda: _BUNDLED_MODEL)
    # Decision threshold for the provider's prediction.  A turn is classified
    # "complete" only when the model's probability is *strictly greater* than
    # this value, so probability == threshold (e.g. exactly 0.5 by default)
    # stays "incomplete".
    #
    # Precedence: when you build a session via ``EasyConfig``/``create_session``
    # and leave ``TurnManagerConfig.endpoint_threshold`` unset (``None``), this
    # value is propagated to the manager so it stays the single source of truth.
    # If you also set ``turn_taking.endpoint_threshold`` to a *different* value,
    # the manager-level threshold wins and this one is ignored (a warning is
    # logged) — set only one of the two to avoid surprises.
    threshold: float = 0.5
    # Maximum time spent waiting to start or finish one endpoint detection.
    # Timeouts fall back to the normal silence timer.
    timeout_s: float = 2.0
    # Maximum trailing audio handed to the model, in seconds.
    max_audio_seconds: float = 8.0

    def __post_init__(self) -> None:
        _validate_probability_threshold("threshold", self.threshold)
        self.timeout_s = _finite_positive_duration("timeout_s", self.timeout_s)
        self.max_audio_seconds = _finite_positive_duration(
            "max_audio_seconds", self.max_audio_seconds
        )


def create_smart_turn(
    config: SmartTurnConfig | None = None,
) -> SmartTurnProvider | None:
    """Create a smart-turn provider from config.  Returns None if disabled."""
    if config is None or not config.enabled:
        return None
    if not config.model_path:
        logger.warning(
            "SmartTurnConfig.enabled=True but model_path is empty; falling back to silence timeout"
        )
        return None
    return SmartTurnONNX(
        model_path=config.model_path,
        threshold=config.threshold,
        timeout_s=config.timeout_s,
        max_audio_seconds=config.max_audio_seconds,
    )
