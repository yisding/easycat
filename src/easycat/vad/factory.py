"""VAD configuration dataclass and the ``create_vad`` factory function."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from easycat.providers import VADProvider
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

_ConcreteVADBackend: TypeAlias = Literal["silero", "funasr", "ten", "krisp"]


@dataclass(slots=True)
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
    # min_speech_duration_ms doubles as the only barge-in debounce: a
    # confirmed VADStartSpeaking during bot playback cancels in-flight
    # TTS/agent work, so keep it high enough to reject residual echo,
    # coughs, and brief background noise.
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 50
    sensitivity: float | None = None

    def __post_init__(self) -> None:
        self.backend = _validate_vad_backend(self.backend)
        _validate_positive_int("funasr_chunk_size_ms", self.funasr_chunk_size_ms)
        _validate_positive_int("funasr_intra_op_num_threads", self.funasr_intra_op_num_threads)
        _validate_non_negative_ms("min_speech_duration_ms", self.min_speech_duration_ms)
        _validate_non_negative_ms("min_silence_duration_ms", self.min_silence_duration_ms)
        if self.sensitivity is not None:
            _validate_vad_sensitivity(self.sensitivity)


@dataclass(frozen=True, slots=True)
class _VADBackendSpec:
    name: _ConcreteVADBackend
    label: str
    factory: Callable[[VADConfig], VADProvider]
    default_sensitivity: float = _DEFAULT_VAD_SENSITIVITY


@dataclass(frozen=True, slots=True)
class _VADBackendFailure:
    backend: _VADBackendSpec
    error: RuntimeError | ImportError


def _build_silero(_config: VADConfig) -> VADProvider:
    return SileroVAD()


def _build_funasr(config: VADConfig) -> VADProvider:
    return FunASROnnxVAD(
        model_dir=config.funasr_model_dir,
        chunk_size_ms=config.funasr_chunk_size_ms,
        device_id=config.funasr_device_id,
        quantize=config.funasr_quantize,
        intra_op_num_threads=config.funasr_intra_op_num_threads,
        cache_dir=config.funasr_cache_dir,
    )


def _build_ten(_config: VADConfig) -> VADProvider:
    return TenVAD()


def _build_krisp(config: VADConfig) -> VADProvider:
    return KrispVAD(model_path=config.krisp_model_path)


_AUTO_BACKENDS: tuple[_VADBackendSpec, ...] = (
    _VADBackendSpec("silero", "Silero", _build_silero),
    _VADBackendSpec("funasr", "FunASR", _build_funasr),
    _VADBackendSpec("ten", "TEN", _build_ten, _TEN_DEFAULT_SENSITIVITY),
    _VADBackendSpec("krisp", "Krisp", _build_krisp),
)
_BACKEND_BY_NAME: dict[_ConcreteVADBackend, _VADBackendSpec] = {
    backend.name: backend for backend in _AUTO_BACKENDS
}


def create_vad(config: VADConfig | None = None) -> VADProvider:
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
    cfg = config if config is not None else VADConfig()
    backend = _validate_vad_backend(cfg.backend)
    if backend == "auto":
        return _create_first_available(cfg)
    return _create_backend(_BACKEND_BY_NAME[backend], cfg)


def _create_backend(backend: _VADBackendSpec, config: VADConfig) -> VADProvider:
    provider = backend.factory(config)
    sensitivity = (
        config.sensitivity if config.sensitivity is not None else backend.default_sensitivity
    )
    provider.configure(
        min_speech_duration_ms=config.min_speech_duration_ms,
        min_silence_duration_ms=config.min_silence_duration_ms,
        sensitivity=sensitivity,
    )
    return provider


def _create_first_available(config: VADConfig) -> VADProvider:
    failures: list[_VADBackendFailure] = []
    for index, backend in enumerate(_AUTO_BACKENDS):
        try:
            return _create_backend(backend, config)
        except (RuntimeError, ImportError) as exc:
            failures.append(_VADBackendFailure(backend, exc))
            next_backend = _AUTO_BACKENDS[index + 1] if index + 1 < len(_AUTO_BACKENDS) else None
            if next_backend is None:
                logger.info("%s VAD not available either", backend.label)
            else:
                logger.info(
                    "%s VAD not available, trying %s fallback",
                    backend.label,
                    next_backend.label,
                )

    cause = failures[-1].error if failures else None
    raise _no_backend_available(failures) from cause


def _no_backend_available(failures: list[_VADBackendFailure]) -> RuntimeError:
    error = RuntimeError(
        "No VAD backend available. Install Silero VAD with: "
        "uv add 'easycat[silero-vad]' (repo: uv sync --extra silero-vad --group dev), "
        "TEN VAD with: uv add 'easycat[ten-vad]' "
        "(repo: uv sync --extra ten-vad --group dev), or FunASR VAD with "
        "backend='funasr' and uv add 'easycat[funasr-vad]' "
        "(repo: uv sync --extra funasr-vad --group dev). Krisp users can configure "
        "krisp-audio."
    )
    for failure in failures:
        error.add_note(f"{failure.backend.label}: {type(failure.error).__name__}: {failure.error}")
    return error
