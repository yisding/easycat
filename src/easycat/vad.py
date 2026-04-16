"""Voice Activity Detection implementations: Silero, TEN, FunASR, and Krisp.

Each implements the VADProvider protocol from providers.py:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]
    def configure(self, ...) -> None

The factory function `create_vad` selects the best available backend
with automatic fallback from Silero -> FunASR -> TEN -> Krisp.  TEN VAD is
installed via the ``ten-vad`` optional extra; we no longer vendor
its binaries because the upstream license is incompatible with this
project's redistribution terms.
"""

from __future__ import annotations

import logging
import os
import platform
import struct
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any

from easycat.audio_format import AudioChunk
from easycat.audio_utils import resample_chunk
from easycat.events import Event, VADStartSpeaking, VADStopSpeaking
from easycat.extras import require_module

logger = logging.getLogger(__name__)

# Silero v5 VAD accepts 8 kHz or 16 kHz mono audio via the ``sr`` input.
# Telephony transports feed 8 kHz natively; WebRTC / local mic typically
# arrives at 16 kHz (or is resampled there from higher rates).  Chunk and
# context sizes are fixed 32 ms windows at each rate.
_SILERO_SUPPORTED_RATES: tuple[int, ...] = (8000, 16000)
_SILERO_DEFAULT_RATE = 16000
_SILERO_FRAME_SAMPLES_AT: dict[int, int] = {8000: 256, 16000: 512}
_SILERO_CONTEXT_SAMPLES_AT: dict[int, int] = {8000: 32, 16000: 64}
_SILERO_RISKY_TORCH_ARCHES = {"aarch64", "arm64"}
_SILERO_ONNX_MODEL = Path(__file__).parent / "models" / "silero_vad.onnx"
# TEN VAD expects 16 kHz audio and defaults to a hop size of 256 samples.
_TEN_SAMPLE_RATE = 16000
_TEN_HOP_SAMPLES = 256
_DEFAULT_VAD_SENSITIVITY = 0.5
_TEN_DEFAULT_THRESHOLD = 0.6
_TEN_DEFAULT_SENSITIVITY = 1.0 - _TEN_DEFAULT_THRESHOLD
# FunASR FSMN-VAD is published as a 16 kHz model.  We resample any
# non-16 kHz inputs before streaming chunks through the online runtime.
_FUNASR_SAMPLE_RATE = 16000
_FUNASR_DEFAULT_CHUNK_MS = 50
_FUNASR_BUNDLED_MODEL_DIR = Path(__file__).parent / "models" / "funasr_fsmn_vad"
_FUNASR_DEFAULT_MODEL = str(_FUNASR_BUNDLED_MODEL_DIR)


# ── VAD base class ────────────────────────────────────────────────


class _VADBase:
    """Internal base class holding the shared VAD state machine.

    Provides the threshold + timing state, ``configure()``, the synchronous
    ``_evaluate_speech()`` generator, and ``reset()`` for the common variables.
    """

    def __init__(self) -> None:
        self._threshold: float = 0.5
        self._min_speech_duration_ms: int = 250
        self._min_silence_duration_ms: int = 150
        self._pre_roll_ms: int = 100
        self._post_roll_ms: int = 100

        # Internal state
        self._is_speaking: bool = False
        self._speech_start_time: float | None = None
        self._silence_start_time: float | None = None
        self._speech_confirmed: bool = False

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        """Configure VAD thresholds and buffering parameters."""
        self._min_speech_duration_ms = min_speech_duration_ms
        self._min_silence_duration_ms = min_silence_duration_ms
        # Sensitivity maps inversely to threshold: higher sensitivity = lower threshold
        self._threshold = 1.0 - sensitivity
        self._pre_roll_ms = pre_roll_ms
        self._post_roll_ms = post_roll_ms

    def _evaluate_speech(self, speech_prob: float, now: float) -> Iterator[Event]:
        """Evaluate a single speech probability against the state machine.

        Yields VADStartSpeaking / VADStopSpeaking events as appropriate.
        """
        if speech_prob >= self._threshold:
            # Speech detected
            self._silence_start_time = None
            if not self._is_speaking:
                if self._speech_start_time is None:
                    self._speech_start_time = now
                if (
                    self._speech_start_time is not None
                    and not self._speech_confirmed
                    and (now - self._speech_start_time) * 1000 >= self._min_speech_duration_ms
                ):
                    self._is_speaking = True
                    self._speech_confirmed = True
                    yield VADStartSpeaking()
        else:
            # Silence detected
            self._speech_start_time = None
            self._speech_confirmed = False
            if self._is_speaking:
                if self._silence_start_time is None:
                    self._silence_start_time = now
                elif (now - self._silence_start_time) * 1000 >= self._min_silence_duration_ms:
                    self._is_speaking = False
                    self._silence_start_time = None
                    yield VADStopSpeaking()

    def reset(self) -> None:
        """Reset the common VAD state variables."""
        self._is_speaking = False
        self._speech_start_time = None
        self._silence_start_time = None
        self._speech_confirmed = False

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "unknown",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }


# ── Silero VAD (open-source) ────────────────────────────────────────


def _silero_backend_override() -> str | None:
    override = os.getenv("EASYCAT_SILERO_BACKEND", "").strip().lower()
    if override in {"torch", "onnx"}:
        return override
    return None


def _silero_backend_candidates() -> tuple[str, ...]:
    override = _silero_backend_override()
    if override is not None:
        return (override,)
    machine = platform.machine().strip().lower()
    if machine in _SILERO_RISKY_TORCH_ARCHES:
        return ("onnx",)
    return ("torch", "onnx")


def _silero_onnx_model_path() -> str:
    if not _SILERO_ONNX_MODEL.exists():
        raise RuntimeError(f"Bundled Silero VAD ONNX model file not found: {_SILERO_ONNX_MODEL}")
    return str(_SILERO_ONNX_MODEL)


class _SileroOnnxModel:
    """Small ONNX-only Silero wrapper that mirrors the recurrent model contract."""

    def __init__(self, model_path: str) -> None:
        numpy = require_module("numpy", extra="silero-vad", purpose="Silero VAD ONNX")
        onnxruntime = require_module("onnxruntime", extra="silero-vad", purpose="Silero VAD ONNX")

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1

        providers = None
        available = onnxruntime.get_available_providers()
        if "CPUExecutionProvider" in available:
            providers = ["CPUExecutionProvider"]

        if providers is None:
            self._session = onnxruntime.InferenceSession(model_path, sess_options=opts)
        else:
            self._session = onnxruntime.InferenceSession(
                model_path, providers=providers, sess_options=opts
            )
        self._numpy = numpy
        self.reset_states()

    def reset_states(self) -> None:
        np = self._numpy
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 0), dtype=np.float32)
        self._last_sr = 0

    def predict(self, samples: list[float], sample_rate: int) -> float:
        if sample_rate not in _SILERO_SUPPORTED_RATES:
            raise ValueError(
                f"Silero ONNX expects one of {_SILERO_SUPPORTED_RATES} Hz, got {sample_rate}"
            )
        expected_frame = _SILERO_FRAME_SAMPLES_AT[sample_rate]
        context_size = _SILERO_CONTEXT_SAMPLES_AT[sample_rate]

        np = self._numpy
        frame = np.asarray(samples, dtype=np.float32).reshape(1, -1)
        if frame.shape[-1] != expected_frame:
            raise ValueError(
                f"Silero ONNX at {sample_rate} Hz expects {expected_frame} samples, "
                f"got {frame.shape[-1]}"
            )

        if self._last_sr and self._last_sr != sample_rate:
            self.reset_states()
        if self._context.shape[1] == 0:
            self._context = np.zeros((frame.shape[0], context_size), dtype=np.float32)

        model_input = np.concatenate([self._context, frame], axis=1)
        outputs = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": np.asarray(sample_rate, dtype=np.int64),
            },
        )
        speech_prob, next_state = outputs
        self._state = next_state.astype(np.float32, copy=False)
        self._context = model_input[:, -context_size:]
        self._last_sr = sample_rate
        return float(np.asarray(speech_prob).reshape(-1)[0])


class SileroVAD(_VADBase):
    """Voice activity detection using the Silero VAD model.

    Loads the Silero VAD model (PyTorch or ONNX) and processes audio
    chunks to detect speech start/stop. Emits VADStartSpeaking and
    VADStopSpeaking events.

    Configurable parameters:
      - min_speech_duration_ms: minimum duration of speech to trigger start
      - min_silence_duration_ms: minimum silence to trigger stop
      - sensitivity: detection threshold (0.0-1.0, lower = more sensitive)
      - pre_roll_ms: audio to buffer before VAD trigger (informational)
      - post_roll_ms: extra audio after silence detected (informational)
    """

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._torch: Any = None
        self._backend: str | None = None

        # Accumulation buffer for sub-frame chunks
        self._buffer: bytes = b""

        self._load_model()

    def _load_model(self) -> None:
        """Load the Silero VAD model."""
        errors: list[str] = []
        for backend in _silero_backend_candidates():
            try:
                if backend == "onnx":
                    self._load_onnx_model()
                else:
                    self._load_torch_model()
                logger.info("Silero VAD model loaded successfully via %s", self._backend)
                return
            except (ImportError, RuntimeError) as exc:
                errors.append(f"{backend}: {exc}")
                logger.info("Silero VAD %s backend unavailable: %s", backend, exc)

        joined = "; ".join(errors) or "no backend candidates"
        raise RuntimeError(f"Failed to load Silero VAD model: {joined}")

    def _load_torch_model(self) -> None:
        try:
            torch = require_module("torch", extra="all", purpose="Silero VAD")
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
        except Exception as exc:
            raise RuntimeError(f"torch loader failed: {exc}") from exc
        self._model = model
        self._torch = torch
        self._backend = "torch"

    def _load_onnx_model(self) -> None:
        try:
            model_path = _silero_onnx_model_path()
            self._model = _SileroOnnxModel(model_path)
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"onnx loader failed: {exc}") from exc
        self._torch = None
        self._backend = "onnx"

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process an audio chunk and yield VAD events."""
        # Silero v5 handles 8 kHz and 16 kHz natively.  Anything else (24 k,
        # 48 k, …) resamples to 16 kHz to preserve fidelity.
        if chunk.format.sample_rate not in _SILERO_SUPPORTED_RATES:
            chunk = resample_chunk(chunk, _SILERO_DEFAULT_RATE)
        target_rate = chunk.format.sample_rate

        # Accumulate into buffer
        self._buffer += chunk.data

        # Process complete frames
        frame_samples = _SILERO_FRAME_SAMPLES_AT[target_rate]
        frame_bytes = frame_samples * 2  # 2 bytes per PCM16 sample
        while len(self._buffer) >= frame_bytes:
            frame_data = self._buffer[:frame_bytes]
            self._buffer = self._buffer[frame_bytes:]

            # Convert PCM16 to float32 tensor
            n = len(frame_data) // 2
            samples = struct.unpack(f"<{n}h", frame_data)
            float_samples = [s / 32768.0 for s in samples]

            if self._backend == "onnx":
                speech_prob = self._model.predict(float_samples, target_rate)
            else:
                if self._torch is None:
                    self._torch = require_module("torch", extra="all", purpose="Silero VAD")
                tensor = self._torch.FloatTensor(float_samples)
                speech_prob = self._model(tensor, target_rate).item()
            now = time.monotonic()

            for event in self._evaluate_speech(speech_prob, now):
                yield event

    def reset(self) -> None:
        """Reset VAD internal state."""
        super().reset()
        self._buffer = b""
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass

    def version_info(self) -> dict[str, str]:
        sdk_package = "torch" if self._backend == "torch" else "onnxruntime"
        sdk_ver = "unknown"
        try:
            sdk_ver = version(sdk_package)
        except Exception:
            pass
        model_name = "silero-vad-torch"
        if self._backend == "onnx":
            model_name = "silero-vad-v6.2.1-onnx"
        return {
            "provider": "silero",
            "model": model_name if self._backend is not None else "silero-vad-unknown",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


# ── Krisp VAD (commercial) ──────────────────────────────────────────


class KrispVAD(_VADBase):
    """Voice activity detection using Krisp VIVA VAD SDK.

    Requires the Krisp SDK with a valid license.
    Same event interface and configuration as Silero.
    """

    def __init__(self, model_path: str | None = None) -> None:
        super().__init__()
        self._session: Any = None
        self._model_path = model_path
        self._krisp_audio: Any = None

        self._initialize()

    def _initialize(self) -> None:
        """Initialize the Krisp VAD SDK session."""
        try:
            krisp_audio = require_module("krisp_audio", purpose="Krisp VAD")
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        config = {}
        if self._model_path:
            config["model_path"] = self._model_path
        try:
            self._session = krisp_audio.create_vad_session(**config)
        except Exception as exc:
            raise RuntimeError(
                f"Krisp VAD initialization failed (license or config issue): {exc}"
            ) from exc
        self._krisp_audio = krisp_audio
        logger.info("Krisp VAD initialized")

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process audio through Krisp VAD and yield events."""
        if self._krisp_audio is None:
            self._krisp_audio = require_module("krisp_audio", extra="krisp", purpose="Krisp VAD")
        speech_prob = self._krisp_audio.vad_process(
            self._session, chunk.data, chunk.format.sample_rate
        )
        now = time.monotonic()

        for event in self._evaluate_speech(speech_prob, now):
            yield event

    def reset(self) -> None:
        """Reset VAD internal state."""
        super().reset()

    def close(self) -> None:
        """Release Krisp session resources."""
        if self._session is not None:
            try:
                if self._krisp_audio is None:
                    self._krisp_audio = require_module(
                        "krisp_audio", extra="krisp", purpose="Krisp VAD"
                    )
                self._krisp_audio.destroy_session(self._session)
            except Exception:
                pass
            self._session = None

    def version_info(self) -> dict[str, str]:
        sdk_ver = "unknown"
        try:
            from importlib.metadata import version

            sdk_ver = version("krisp-audio")
        except Exception:
            pass
        return {
            "provider": "krisp",
            "model": "krisp-vad",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }

    def __del__(self) -> None:
        self.close()


# ── TEN VAD (via ten-vad PyPI package) ─────────────────────────────


class TenVAD(_VADBase):
    """Voice activity detection using the ``ten-vad`` PyPI package.

    TEN VAD consumes PCM16 int16 frames with a fixed hop size, and returns
    a speech probability plus flags.  We do not vendor the upstream
    binaries because the license is incompatible with this project's
    redistribution terms — users who accept the TEN VAD license install
    it themselves via the ``ten-vad`` optional extra.
    """

    def __init__(self, hop_size: int = _TEN_HOP_SAMPLES) -> None:
        super().__init__()
        self._hop_size = hop_size
        self._buffer: bytes = b""
        self._ten_vad: Any = None
        self._numpy: Any = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            ten_vad = require_module("ten_vad", extra="ten-vad", purpose="TEN VAD")
            numpy = require_module("numpy", extra="ten-vad", purpose="TEN VAD")
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc

        try:
            self._ten_vad = ten_vad.TenVad(hop_size=self._hop_size, threshold=0.5)
        except Exception as exc:
            raise RuntimeError(
                "If you are on macOS/Windows, install a recent ten-vad build "
                "with ONNX support. Original error: "
                f"{exc}"
            ) from exc

        self._numpy = numpy
        logger.info("TEN VAD initialized")

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process an audio chunk and yield VAD events."""
        if self._ten_vad is None or self._numpy is None:
            self._initialize()

        # TEN currently runs at 16 kHz in this integration.
        if chunk.format.sample_rate != _TEN_SAMPLE_RATE:
            chunk = resample_chunk(chunk, _TEN_SAMPLE_RATE)

        self._buffer += chunk.data
        frame_bytes = self._hop_size * 2  # PCM16: 2 bytes per sample

        while len(self._buffer) >= frame_bytes:
            frame_data = self._buffer[:frame_bytes]
            self._buffer = self._buffer[frame_bytes:]

            frame = self._numpy.frombuffer(frame_data, dtype=self._numpy.int16).copy()
            speech_prob, _ = self._ten_vad.process(frame)
            now = time.monotonic()
            for event in self._evaluate_speech(float(speech_prob), now):
                yield event

    def reset(self) -> None:
        """Reset VAD internal state."""
        super().reset()
        self._buffer = b""

    def version_info(self) -> dict[str, str]:
        try:
            sdk_ver = version("ten-vad")
        except Exception:
            sdk_ver = "unknown"
        return {
            "provider": "ten",
            "model": "ten-vad",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


# ── FunASR ONNX VAD ────────────────────────────────────────────────


def _iter_funasr_segment_pairs(value: Any) -> Iterator[tuple[int, int]]:
    """Flatten FunASR segment outputs into ``(beg_ms, end_ms)`` pairs."""
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(item, (int, float)) for item in value):
            yield int(value[0]), int(value[1])
            return
        for item in value:
            yield from _iter_funasr_segment_pairs(item)


class FunASROnnxVAD(_VADBase):
    """Voice activity detection via ``funasr_onnx.Fsmn_vad_online``.

    FunASR's streaming VAD emits absolute segment boundaries rather than
    frame-level speech probabilities, so this adapter maps those
    boundaries into EasyCat's ``VADStartSpeaking`` / ``VADStopSpeaking``
    events.  The published FunASR FSMN-VAD models are 16 kHz; 8 kHz
    telephony audio is upsampled before inference.
    """

    def __init__(
        self,
        model_dir: str = _FUNASR_DEFAULT_MODEL,
        *,
        chunk_size_ms: int = _FUNASR_DEFAULT_CHUNK_MS,
        device_id: str | int = "-1",
        quantize: bool = False,
        intra_op_num_threads: int = 4,
        cache_dir: str | None = None,
    ) -> None:
        super().__init__()
        if chunk_size_ms <= 0:
            raise ValueError("chunk_size_ms must be positive")

        self._model_dir = model_dir
        self._chunk_size_ms = chunk_size_ms
        self._device_id = device_id
        self._quantize = quantize
        self._intra_op_num_threads = intra_op_num_threads
        self._cache_dir = cache_dir
        self._chunk_samples = int(_FUNASR_SAMPLE_RATE * chunk_size_ms / 1000)
        if self._chunk_samples <= 0:
            raise ValueError("chunk_size_ms produced an empty chunk")

        self._buffer: bytes = b""
        self._numpy: Any = None
        self._model: Any = None
        self._param_dict: dict[str, Any] = {"in_cache": []}
        self._initialize()

    def _initialize(self) -> None:
        try:
            numpy = require_module("numpy", extra="funasr-vad", purpose="FunASR VAD")
            funasr_vad_cls = _load_funasr_onnx_vad_online_class()
            model_dir = _resolve_funasr_model_dir(self._model_dir)
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc

        try:
            self._model = funasr_vad_cls(
                model_dir=model_dir,
                device_id=self._device_id,
                quantize=self._quantize,
                intra_op_num_threads=self._intra_op_num_threads,
                max_end_sil=self._min_silence_duration_ms,
                cache_dir=self._cache_dir,
            )
        except Exception as exc:
            raise RuntimeError(f"FunASR ONNX VAD initialization failed: {exc}") from exc

        self._numpy = numpy
        self._param_dict = {"in_cache": []}
        logger.info("FunASR ONNX VAD initialized")

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        super().configure(
            min_speech_duration_ms=min_speech_duration_ms,
            min_silence_duration_ms=min_silence_duration_ms,
            sensitivity=sensitivity,
            pre_roll_ms=pre_roll_ms,
            post_roll_ms=post_roll_ms,
        )
        if self._model is not None and hasattr(self._model, "max_end_sil"):
            try:
                self._model.max_end_sil = self._min_silence_duration_ms
            except Exception:
                pass

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process a chunk and yield translated FunASR boundary events."""
        if self._numpy is None or self._model is None:
            self._initialize()

        if chunk.format.sample_rate != _FUNASR_SAMPLE_RATE:
            chunk = resample_chunk(chunk, _FUNASR_SAMPLE_RATE)

        self._buffer += chunk.data
        frame_bytes = self._chunk_samples * 2

        while len(self._buffer) >= frame_bytes:
            frame_data = self._buffer[:frame_bytes]
            self._buffer = self._buffer[frame_bytes:]

            waveform = self._numpy.frombuffer(frame_data, dtype=self._numpy.int16)
            waveform = waveform.astype(self._numpy.float32) / 32768.0
            segments = self._model(audio_in=waveform, param_dict=self._param_dict)

            for beg_ms, end_ms in _iter_funasr_segment_pairs(segments):
                if beg_ms >= 0 and not self._is_speaking:
                    self._is_speaking = True
                    yield VADStartSpeaking()
                if end_ms >= 0 and self._is_speaking:
                    self._is_speaking = False
                    yield VADStopSpeaking()

    def reset(self) -> None:
        """Reset adapter state and FunASR streaming caches."""
        super().reset()
        self._buffer = b""
        self._param_dict = {"in_cache": []}

    def version_info(self) -> dict[str, str]:
        try:
            sdk_ver = version("funasr-onnx")
        except Exception:
            sdk_ver = "unknown"
        model_name = self._model_dir
        if self._model_dir == _FUNASR_DEFAULT_MODEL:
            model_name = "funasr-fsmn-vad-bundled"
        return {
            "provider": "funasr",
            "model": model_name,
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


def _load_funasr_onnx_vad_online_class() -> Any:
    """Load ``Fsmn_vad_online`` without executing ``funasr_onnx.__init__``.

    ``funasr-onnx`` 0.4.1 imports unrelated modules like SenseVoice from the
    top-level package, which in turn pull in optional dependencies such as
    ``torch``.  The VAD runtime itself lives in ``funasr_onnx.vad_bin`` and
    does not require those extras, so we seed a package stub and import the
    submodule directly.
    """

    spec = find_spec("funasr_onnx")
    if spec is None or not spec.submodule_search_locations:
        raise ImportError(
            "FunASR VAD requires the funasr_onnx package. Install easycat[funasr-vad]."
        )

    package = sys.modules.get("funasr_onnx")
    if package is None or not hasattr(package, "__path__"):
        package = ModuleType("funasr_onnx")
        package.__path__ = list(spec.submodule_search_locations)
        package.__spec__ = spec
        sys.modules["funasr_onnx"] = package

    try:
        module = import_module("funasr_onnx.vad_bin")
    except Exception as exc:
        raise ImportError(f"Failed to import funasr_onnx.vad_bin: {exc}") from exc

    try:
        return getattr(module, "Fsmn_vad_online")
    except AttributeError as exc:
        raise ImportError("funasr_onnx.vad_bin does not export Fsmn_vad_online") from exc


def _resolve_funasr_model_dir(model_dir: str) -> str:
    if model_dir != _FUNASR_DEFAULT_MODEL:
        return model_dir

    required = ("model.onnx", "config.yaml", "am.mvn")
    missing = [name for name in required if not (_FUNASR_BUNDLED_MODEL_DIR / name).exists()]
    if missing:
        raise ImportError("Bundled FunASR FSMN-VAD assets are missing: " + ", ".join(missing))
    return str(_FUNASR_BUNDLED_MODEL_DIR)


# ── Factory ─────────────────────────────────────────────────────────


@dataclass
class VADConfig:
    """Configuration for VAD factory."""

    # "funasr", "krisp", "ten", "silero", or "auto" (try silero -> ten -> krisp)
    backend: str = "auto"
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
    pre_roll_ms: int = 100
    post_roll_ms: int = 100


def create_vad(config: VADConfig | None = None) -> Any:
    """Create the best available VAD provider.

    Selection order:
      1. If config.backend == "silero": use Silero (fail if unavailable)
      2. If config.backend == "funasr": use FunASR ONNX VAD (fail if unavailable)
      3. If config.backend == "ten": use TEN VAD (fail if unavailable)
      4. If config.backend == "krisp": use Krisp (fail if unavailable)
      4. If config.backend == "auto" (default):
         - Try Silero first (permissively-licensed, bundled ONNX model)
         - Fall back to FunASR ONNX VAD
         - Fall back to TEN VAD (PyPI ``ten-vad`` if user installed it)
         - Fall back to Krisp (requires commercial SDK)

    Returns an object satisfying the VADProvider protocol.
    """
    cfg = config or VADConfig()

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
            pre_roll_ms=cfg.pre_roll_ms,
            post_roll_ms=cfg.post_roll_ms,
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
