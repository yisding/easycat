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


# ── Minimal Whisper feature extraction (NumPy only) ──────────────


def _hertz_to_mel(freq: Any, *, np: Any, mel_scale: str = "slaney") -> Any:
    if mel_scale != "slaney":
        raise ValueError(f"Unsupported mel scale: {mel_scale}")

    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = 27.0 / np.log(6.4)
    mels = 3.0 * freq / 200.0

    if isinstance(freq, np.ndarray):
        log_region = freq >= min_log_hertz
        mels[log_region] = min_log_mel + np.log(freq[log_region] / min_log_hertz) * logstep
    elif freq >= min_log_hertz:
        mels = min_log_mel + np.log(freq / min_log_hertz) * logstep

    return mels


def _mel_to_hertz(mels: Any, *, np: Any, mel_scale: str = "slaney") -> Any:
    if mel_scale != "slaney":
        raise ValueError(f"Unsupported mel scale: {mel_scale}")

    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = np.log(6.4) / 27.0
    freq = 200.0 * mels / 3.0

    if isinstance(mels, np.ndarray):
        log_region = mels >= min_log_mel
        freq[log_region] = min_log_hertz * np.exp(logstep * (mels[log_region] - min_log_mel))
    elif mels >= min_log_mel:
        freq = min_log_hertz * np.exp(logstep * (mels - min_log_mel))

    return freq


def _create_triangular_filter_bank(fft_freqs: Any, filter_freqs: Any, *, np: Any) -> Any:
    filter_diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    return np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))


def _mel_filter_bank(
    *,
    np: Any,
    num_frequency_bins: int,
    num_mel_filters: int,
    min_frequency: float,
    max_frequency: float,
    sampling_rate: int,
) -> Any:
    mel_min = _hertz_to_mel(min_frequency, np=np)
    mel_max = _hertz_to_mel(max_frequency, np=np)
    mel_freqs = np.linspace(mel_min, mel_max, num_mel_filters + 2)
    filter_freqs = _mel_to_hertz(mel_freqs, np=np)
    fft_freqs = np.linspace(0, sampling_rate // 2, num_frequency_bins)
    mel_filters = _create_triangular_filter_bank(fft_freqs, filter_freqs, np=np)

    enorm = 2.0 / (filter_freqs[2 : num_mel_filters + 2] - filter_freqs[:num_mel_filters])
    mel_filters *= np.expand_dims(enorm, 0)
    return mel_filters


def _window_function(window_length: int, *, np: Any) -> Any:
    return np.hanning(window_length + 1)[:-1]


def _spectrogram(
    waveform: Any,
    *,
    np: Any,
    window: Any,
    frame_length: int,
    hop_length: int,
    mel_filters: Any,
) -> Any:
    if waveform.size < 2:
        pad_mode = "edge"
    else:
        pad_mode = "reflect"
    waveform = np.pad(waveform, (frame_length // 2, frame_length // 2), mode=pad_mode)
    waveform = waveform.astype(np.float64)
    window = window.astype(np.float64)

    num_frames = int(1 + np.floor((waveform.size - frame_length) / hop_length))
    num_frequency_bins = (frame_length // 2) + 1
    spec = np.empty((num_frames, num_frequency_bins), dtype=np.complex64)
    buffer = np.zeros(frame_length, dtype=np.float64)

    timestep = 0
    for frame_idx in range(num_frames):
        buffer[:] = waveform[timestep : timestep + frame_length]
        buffer *= window
        spec[frame_idx] = np.fft.rfft(buffer)
        timestep += hop_length

    spec = (np.abs(spec, dtype=np.float64) ** 2.0).T
    spec = np.maximum(1e-10, np.dot(mel_filters.T, spec))
    spec = np.log10(spec)
    return spec.astype(np.float32)


class _WhisperFeatureExtractorNP:
    """Whisper-compatible log-mel frontend for the bundled ONNX model.

    This is a narrow, torch-free subset of Hugging Face's Whisper feature
    extraction logic. It only implements the path Smart Turn needs:
    single-waveform, CPU, NumPy output.
    """

    def __init__(
        self,
        *,
        np: Any,
        feature_size: int = 80,
        sampling_rate: int = 16000,
        hop_length: int = 160,
        chunk_length: int = 8,
        n_fft: int = 400,
        padding_value: float = 0.0,
    ) -> None:
        self._np = np
        self.feature_size = feature_size
        self.sampling_rate = sampling_rate
        self.hop_length = hop_length
        self.chunk_length = chunk_length
        self.n_fft = n_fft
        self.padding_value = padding_value
        self.n_samples = chunk_length * sampling_rate
        self.window = _window_function(n_fft, np=np)
        self.mel_filters = _mel_filter_bank(
            np=np,
            num_frequency_bins=1 + n_fft // 2,
            num_mel_filters=feature_size,
            min_frequency=0.0,
            max_frequency=8000.0,
            sampling_rate=sampling_rate,
        )

    def __call__(
        self,
        raw_speech: Any,
        *,
        sampling_rate: int,
        do_normalize: bool = True,
    ) -> Any:
        np = self._np
        if sampling_rate != self.sampling_rate:
            raise ValueError(f"expected sampling_rate={self.sampling_rate}, got {sampling_rate}")

        audio = np.asarray(raw_speech, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError(f"expected mono waveform, got shape={audio.shape}")
        if audio.size == 0:
            return np.zeros(
                (1, self.feature_size, self.n_samples // self.hop_length), dtype=np.float32
            )

        audio = audio[-self.n_samples :]
        valid_length = audio.shape[0]

        if do_normalize:
            audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)

        if valid_length < self.n_samples:
            padded = np.full((self.n_samples,), self.padding_value, dtype=np.float32)
            padded[:valid_length] = audio
            audio = padded

        log_spec = _spectrogram(
            audio,
            np=np,
            window=self.window,
            frame_length=self.n_fft,
            hop_length=self.hop_length,
            mel_filters=self.mel_filters,
        )
        log_spec = log_spec[:, :-1]
        log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
        log_spec = (log_spec + 4.0) / 4.0
        return np.expand_dims(log_spec.astype(np.float32), axis=0)


# ── Smart-Turn ONNX implementation ────────────────────────────────


class SmartTurnONNX:
    """Smart-turn provider using the ONNX model.

    Lazy-loads the ONNX model and a NumPy Whisper frontend on first use.
    Inference runs in a thread executor because ONNX Runtime is synchronous.

    Requires: numpy, onnxruntime
    """

    def __init__(self, model_path: str, threshold: float = 0.5) -> None:
        self._model_path = model_path
        self._threshold = threshold
        self._session: Any = None  # ort.InferenceSession (lazy)
        self._feature_extractor: Any = None  # NumPy Whisper frontend (lazy)
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

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(self._model_path, sess_options=so)

        self._feature_extractor = _WhisperFeatureExtractorNP(np=self._np, chunk_length=8)
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
            audio_array = np.pad(audio_array, (0, padding), mode="constant", constant_values=0)

        input_features = self._feature_extractor(
            audio_array,
            sampling_rate=16000,
            do_normalize=True,
        )

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
            "SmartTurnConfig.enabled=True but model_path is empty; falling back to silence timeout"
        )
        return None
    return SmartTurnONNX(
        model_path=config.model_path,
        threshold=config.threshold,
    )
