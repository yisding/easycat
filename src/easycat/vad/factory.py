"""VAD configuration dataclass and the ``create_vad`` factory function."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from easycat.vad._base import (
    _DEFAULT_VAD_SENSITIVITY,
    VADBackend,
    _validate_non_negative_ms,
    _validate_positive_int,
    _validate_vad_backend,
    _validate_vad_sensitivity,
)
from easycat.vad.funasr import _FUNASR_DEFAULT_CHUNK_MS, _FUNASR_DEFAULT_MODEL, FunASROnnxVAD
from easycat.vad.krisp import KrispVAD
from easycat.vad.silero import SileroVAD
from easycat.vad.ten import _TEN_DEFAULT_SENSITIVITY, TenVAD

logger = logging.getLogger(__name__)


@dataclass
class VADConfig:
    """Configuration for VAD factory."""

    # "funasr", "krisp", "ten", "silero", or "auto"
    # (auto tries silero -> funasr -> ten -> krisp)
    backend: VADBackend = "auto"
    # FunASR-specific
    funasr_model_dir: str = _FUNASR_DEFAULT_MODEL
    funasr_chunk_size_ms: int = _FUNASR_DEFAULT_CHUNK_MS
    funasr_device_id: str | int = "-1"
    funasr_quantize: bool = False
    funasr_intra_op_num_threads: int = 4
    funasr_cache_dir: str | None = None
    # Krisp-specific
    krisp_model_path: str | None = None
    # Shared VAD settings
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 150
    sensitivity: float | None = None

    def __post_init__(self) -> None:
        self.backend = _validate_vad_backend(self.backend)
        _validate_positive_int("funasr_chunk_size_ms", self.funasr_chunk_size_ms)
        _validate_positive_int("funasr_intra_op_num_threads", self.funasr_intra_op_num_threads)
        _validate_non_negative_ms("min_speech_duration_ms", self.min_speech_duration_ms)
        _validate_non_negative_ms("min_silence_duration_ms", self.min_silence_duration_ms)
        if self.sensitivity is not None:
            _validate_vad_sensitivity(self.sensitivity)


def create_vad(config: VADConfig | None = None) -> Any:
    """Create the best available VAD provider.

    Selection order:
      1. If config.backend == "silero": use Silero (fail if unavailable)
      2. If config.backend == "ten": use TEN VAD (fail if unavailable)
      3. If config.backend == "krisp": use Krisp (fail if unavailable)
      4. If config.backend == "funasr": use FunASR ONNX VAD (fail if unavailable)
      5. If config.backend == "auto" (default):
         - Try Silero first (permissively-licensed, bundled ONNX model)
         - Fall back to FunASR ONNX VAD
         - Fall back to TEN VAD (PyPI ``ten-vad`` if user installed it)
         - Fall back to Krisp (requires commercial SDK)

    Returns an object satisfying the VADProvider protocol.
    """
    cfg = config or VADConfig()
    cfg.backend = _validate_vad_backend(cfg.backend)

    def _default_sensitivity(backend: str) -> float:
        if backend == "ten":
            return _TEN_DEFAULT_SENSITIVITY
        return _DEFAULT_VAD_SENSITIVITY

    def _configure(vad: Any, *, backend: str) -> Any:
        sensitivity = cfg.sensitivity
        if sensitivity is None:
            sensitivity = _default_sensitivity(backend)
        vad.configure(
            min_speech_duration_ms=cfg.min_speech_duration_ms,
            min_silence_duration_ms=cfg.min_silence_duration_ms,
            sensitivity=sensitivity,
        )
        return vad

    if cfg.backend == "silero":
        return _configure(SileroVAD(), backend="silero")

    if cfg.backend == "ten":
        return _configure(TenVAD(), backend="ten")

    if cfg.backend == "krisp":
        return _configure(KrispVAD(model_path=cfg.krisp_model_path), backend="krisp")

    if cfg.backend == "funasr":
        return _configure(
            FunASROnnxVAD(
                model_dir=cfg.funasr_model_dir,
                chunk_size_ms=cfg.funasr_chunk_size_ms,
                device_id=cfg.funasr_device_id,
                quantize=cfg.funasr_quantize,
                intra_op_num_threads=cfg.funasr_intra_op_num_threads,
                cache_dir=cfg.funasr_cache_dir,
            ),
            backend="funasr",
        )

    # Auto mode: try Silero -> FunASR -> TEN -> Krisp.
    try:
        return _configure(SileroVAD(), backend="silero")
    except (RuntimeError, ImportError):
        logger.info("Silero VAD not available, trying FunASR fallback")

    try:
        return _configure(
            FunASROnnxVAD(
                model_dir=cfg.funasr_model_dir,
                chunk_size_ms=cfg.funasr_chunk_size_ms,
                device_id=cfg.funasr_device_id,
                quantize=cfg.funasr_quantize,
                intra_op_num_threads=cfg.funasr_intra_op_num_threads,
                cache_dir=cfg.funasr_cache_dir,
            ),
            backend="funasr",
        )
    except (RuntimeError, ImportError):
        logger.info("FunASR VAD not available, trying TEN fallback")

    try:
        return _configure(TenVAD(), backend="ten")
    except (RuntimeError, ImportError):
        logger.info("TEN VAD not available, trying Krisp fallback")

    try:
        return _configure(KrispVAD(model_path=cfg.krisp_model_path), backend="krisp")
    except (RuntimeError, ImportError):
        logger.info("Krisp VAD not available either")
        raise RuntimeError(
            "No VAD backend available. Install easycat[silero-vad], "
            "easycat[ten-vad], easycat[funasr-vad] (with backend='funasr'), "
            "or krisp-audio (for Krisp)."
        )
