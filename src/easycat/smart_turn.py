"""Smart turn-taking endpoint detection.

A smart-turn provider classifies accumulated turn audio as "complete"
(user finished their turn) or "incomplete" (user is still talking/thinking).
This enables faster turn transitions when the model is confident the user
is done, while falling back to the silence timer for uncertain cases.

SmartTurnONNX wraps the smart-turn ONNX model (~8 MB quantized Whisper-Tiny
classifier).  ONNX inference is synchronous, so it runs in a thread executor
to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from easycat.audio_format import AudioChunk
from easycat.audio_utils import resample
from easycat.extras import require_module

_BUNDLED_MODEL = str(Path(__file__).parent / "models" / "smart-turn-v3.2-cpu.onnx")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmartTurnResult:
    """Result from smart-turn detection."""

    prediction: int  # 1 = complete (turn ended), 0 = incomplete
    probability: float  # sigmoid probability of completion


@runtime_checkable
class SmartTurnProvider(Protocol):
    """Protocol for smart-turn providers."""

    async def detect(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        """Classify accumulated turn audio as complete or incomplete."""
        ...


# ── Smart-Turn ONNX implementation ────────────────────────────────


class SmartTurnONNX:
    """Smart-turn provider using the ONNX model.

    Lazy-loads the ONNX model and WhisperFeatureExtractor on first use.
    Inference runs in a thread executor because ONNX Runtime is synchronous.

    Requires: numpy, onnxruntime, transformers
    """

    def __init__(self, model_path: str, threshold: float = 0.5) -> None:
        self._model_path = model_path
        self._threshold = threshold
        self._session: Any = None  # ort.InferenceSession (lazy)
        self._feature_extractor: Any = None  # WhisperFeatureExtractor (lazy)
        self._np: Any = None  # numpy module (lazy)

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
        transformers = require_module(
            "transformers",
            extra="smart-turn",
            purpose="Smart-turn endpoint detection",
        )

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(self._model_path, sess_options=so)

        self._feature_extractor = transformers.WhisperFeatureExtractor(chunk_length=8)
        logger.info("Smart-turn model loaded from %s", self._model_path)

    def _chunks_to_float32_16k(self, chunks: list[AudioChunk]) -> Any:
        """Convert AudioChunks to a single float32 numpy array at 16 kHz."""
        np = self._np
        if not chunks:
            return np.zeros(0, dtype=np.float32)

        all_samples: list[Any] = []
        for chunk in chunks:
            data = chunk.data
            if chunk.format.sample_rate != 16000:
                data = resample(data, chunk.format.sample_rate, 16000)
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            all_samples.append(samples)

        if not all_samples:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(all_samples)

    def _predict_sync(self, audio_array: Any) -> SmartTurnResult:
        """Run ONNX inference synchronously.  Called from thread executor."""
        np = self._np
        max_samples = 8 * 16000  # 8 seconds at 16 kHz

        if len(audio_array) > max_samples:
            audio_array = audio_array[-max_samples:]
        elif len(audio_array) < max_samples:
            padding = max_samples - len(audio_array)
            audio_array = np.pad(
                audio_array, (padding, 0), mode="constant", constant_values=0
            )

        inputs = self._feature_extractor(
            audio_array,
            sampling_rate=16000,
            return_tensors="np",
            padding="max_length",
            max_length=max_samples,
            truncation=True,
            do_normalize=True,
        )
        input_features = inputs.input_features.squeeze(0).astype(np.float32)
        input_features = np.expand_dims(input_features, axis=0)

        outputs = self._session.run(None, {"input_features": input_features})
        probability = outputs[0][0].item()
        prediction = 1 if probability > self._threshold else 0

        return SmartTurnResult(prediction=prediction, probability=probability)

    async def detect(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        """Classify accumulated turn audio as complete or incomplete."""
        self._ensure_loaded()
        audio_array = self._chunks_to_float32_16k(audio_chunks)

        if len(audio_array) == 0:
            return SmartTurnResult(prediction=0, probability=0.0)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._predict_sync, audio_array)


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
    threshold: float = 0.5


def create_smart_turn(
    config: SmartTurnConfig | None = None,
) -> SmartTurnProvider | None:
    """Create a smart-turn provider from config.  Returns None if disabled."""
    if config is None or not config.enabled:
        return None
    if not config.model_path:
        logger.warning(
            "SmartTurnConfig.enabled=True but model_path is empty; "
            "falling back to silence timeout"
        )
        return None
    return SmartTurnONNX(
        model_path=config.model_path,
        threshold=config.threshold,
    )
