"""Voice Activity Detection implementations: Silero, TEN, and Krisp.

Both implement the VADProvider protocol from providers.py:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]
    def configure(self, ...) -> None

The factory function `create_vad` selects the best available backend
with automatic fallback from Krisp -> TEN -> Silero.
"""

from __future__ import annotations

import logging
import os
import platform
import struct
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from ctypes import CDLL, POINTER, RTLD_GLOBAL, c_float, c_int, c_int32, c_size_t, c_void_p
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from easycat.audio_format import AudioChunk
from easycat.audio_utils import resample_chunk
from easycat.events import Event, VADStartSpeaking, VADStopSpeaking
from easycat.extras import require_module

logger = logging.getLogger(__name__)

# Silero VAD expects 16 kHz mono audio
_SILERO_SAMPLE_RATE = 16000
# Silero processes 512-sample frames (32 ms at 16 kHz)
_SILERO_FRAME_SAMPLES = 512
_SILERO_CONTEXT_SAMPLES = 64
_SILERO_RISKY_TORCH_ARCHES = {"aarch64", "arm64"}
_SILERO_ONNX_MODEL = Path(__file__).parent / "models" / "silero_vad_16k_op15.onnx"
_TEN_BUNDLED_PACKAGE = "easycat_ten_vad_linux_arm64"
# TEN VAD expects 16 kHz audio and defaults to a hop size of 256 samples.
_TEN_SAMPLE_RATE = 16000
_TEN_HOP_SAMPLES = 256


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


def _ten_backend_override() -> str | None:
    override = os.getenv("EASYCAT_TEN_BACKEND", "").strip().lower()
    if override in {"bundled", "package"}:
        return override
    return None


def _ten_bundled_root() -> Path | None:
    if platform.system() != "Linux":
        return None
    if platform.machine().strip().lower() not in {"aarch64", "arm64"}:
        return None
    try:
        bundled = __import__(_TEN_BUNDLED_PACKAGE, fromlist=["bundle_root"])
    except ImportError:
        return None
    bundle_root = Path(bundled.bundle_root())
    required = (bundle_root / "libten_vad.so", bundle_root / "onnx_model" / "ten-vad.onnx")
    if not all(path.exists() for path in required):
        return None
    return bundle_root


def _ten_onnxruntime_library_path(onnxruntime_module: Any) -> Path:
    capi_dir = Path(onnxruntime_module.__file__).resolve().parent / "capi"
    candidates = sorted(capi_dir.glob("libonnxruntime.so.*"))
    if not candidates:
        raise RuntimeError(f"onnxruntime shared library not found in {capi_dir}")
    return candidates[-1]


def _ten_backend_candidates() -> tuple[str, ...]:
    override = _ten_backend_override()
    if override is not None:
        return (override,)
    if _ten_bundled_root() is not None:
        return ("bundled", "package")
    return ("package",)


_cwd_lock = threading.Lock()


@contextmanager
def _temporary_cwd(path: Path) -> Iterator[None]:
    # The bundled TEN VAD library hardcodes "onnx_model/ten-vad.onnx" as a
    # relative path, so os.chdir() is unavoidable.  We use an fd-based restore
    # to guarantee we return to the original directory even if another thread
    # changes cwd concurrently, and hold _cwd_lock to serialize our own callers.
    with _cwd_lock:
        orig_fd = os.open(".", os.O_RDONLY)
        try:
            os.chdir(path)
            yield
        finally:
            os.fchdir(orig_fd)
            os.close(orig_fd)


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
        if sample_rate != _SILERO_SAMPLE_RATE:
            raise ValueError(f"Silero ONNX expects {_SILERO_SAMPLE_RATE} Hz audio")

        np = self._numpy
        frame = np.asarray(samples, dtype=np.float32).reshape(1, -1)
        if frame.shape[-1] != _SILERO_FRAME_SAMPLES:
            raise ValueError(
                f"Silero ONNX expects {_SILERO_FRAME_SAMPLES} samples, got {frame.shape[-1]}"
            )

        if self._last_sr and self._last_sr != sample_rate:
            self.reset_states()
        if self._context.shape[1] == 0:
            self._context = np.zeros((frame.shape[0], _SILERO_CONTEXT_SAMPLES), dtype=np.float32)

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
        self._context = model_input[:, -_SILERO_CONTEXT_SAMPLES:]
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
        # Resample to 16 kHz if needed
        if chunk.format.sample_rate != _SILERO_SAMPLE_RATE:
            chunk = resample_chunk(chunk, _SILERO_SAMPLE_RATE)

        # Accumulate into buffer
        self._buffer += chunk.data

        # Process complete frames
        frame_bytes = _SILERO_FRAME_SAMPLES * 2  # 2 bytes per PCM16 sample
        while len(self._buffer) >= frame_bytes:
            frame_data = self._buffer[:frame_bytes]
            self._buffer = self._buffer[frame_bytes:]

            # Convert PCM16 to float32 tensor
            n = len(frame_data) // 2
            samples = struct.unpack(f"<{n}h", frame_data)
            float_samples = [s / 32768.0 for s in samples]

            if self._backend == "onnx":
                speech_prob = self._model.predict(float_samples, _SILERO_SAMPLE_RATE)
            else:
                if self._torch is None:
                    self._torch = require_module("torch", extra="all", purpose="Silero VAD")
                tensor = self._torch.FloatTensor(float_samples)
                speech_prob = self._model(tensor, _SILERO_SAMPLE_RATE).item()
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
            model_name = "silero-vad-16k-op15-onnx"
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


# ── TEN VAD (open-source) ──────────────────────────────────────────


class TenVAD(_VADBase):
    """Voice activity detection using TEN VAD.

    TEN VAD consumes PCM16 int16 frames with a fixed hop size, and returns
    a speech probability plus flags.
    """

    def __init__(self, hop_size: int = _TEN_HOP_SAMPLES) -> None:
        super().__init__()
        self._hop_size = hop_size
        self._buffer: bytes = b""
        self._ten_vad: Any = None
        self._numpy: Any = None
        self._backend: str | None = None
        self._initialize()

    def _initialize(self) -> None:
        errors: list[str] = []
        for backend in _ten_backend_candidates():
            try:
                if backend == "bundled":
                    self._initialize_bundled()
                else:
                    self._initialize_package()
                logger.info("TEN VAD initialized via %s backend", self._backend)
                return
            except (ImportError, RuntimeError) as exc:
                errors.append(f"{backend}: {exc}")
                logger.info("TEN VAD %s backend unavailable: %s", backend, exc)

        joined = "; ".join(errors) or "no backend candidates"
        raise RuntimeError(f"TEN VAD initialization failed: {joined}")

    def _initialize_bundled(self) -> None:
        bundle_root = _ten_bundled_root()
        if bundle_root is None:
            raise RuntimeError("bundled TEN VAD assets not available for this platform")
        try:
            numpy = require_module("numpy", extra="ten-vad", purpose="TEN VAD")
            onnxruntime = require_module("onnxruntime", extra="ten-vad", purpose="TEN VAD")
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        self._ten_vad = _BundledTenVad(
            bundle_root=bundle_root,
            hop_size=self._hop_size,
            onnxruntime_module=onnxruntime,
        )
        self._numpy = numpy
        self._backend = "bundled"

    def _initialize_package(self) -> None:
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
        self._backend = "package"

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
        sdk_ver = "unknown"
        if self._backend == "package":
            try:
                sdk_ver = version("ten-vad")
            except Exception:
                pass
        elif self._backend == "bundled":
            try:
                sdk_ver = version("easycat-ten-vad-linux-arm64")
            except Exception:
                sdk_ver = "bundled"
        return {
            "provider": "ten",
            "model": "ten-vad-bundled-onnx" if self._backend == "bundled" else "ten-vad",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


class _BundledTenVad:
    """Minimal ctypes wrapper around the vendored TEN VAD shared library."""

    def __init__(
        self,
        *,
        bundle_root: Path,
        hop_size: int,
        onnxruntime_module: Any,
        threshold: float = 0.5,
    ) -> None:
        self._bundle_root = bundle_root
        self._hop_size = hop_size
        self._threshold = threshold
        self._onnxruntime_library = CDLL(
            str(_ten_onnxruntime_library_path(onnxruntime_module)),
            mode=RTLD_GLOBAL,
        )
        self._vad_library = CDLL(str(bundle_root / "libten_vad.so"))
        self._vad_handler = c_void_p(0)
        self._out_probability = c_float()
        self._out_flags = c_int32()

        self._vad_library.ten_vad_create.argtypes = [
            POINTER(c_void_p),
            c_size_t,
            c_float,
        ]
        self._vad_library.ten_vad_create.restype = c_int

        self._vad_library.ten_vad_destroy.argtypes = [POINTER(c_void_p)]
        self._vad_library.ten_vad_destroy.restype = c_int

        self._vad_library.ten_vad_process.argtypes = [
            c_void_p,
            c_void_p,
            c_size_t,
            POINTER(c_float),
            POINTER(c_int32),
        ]
        self._vad_library.ten_vad_process.restype = c_int

        with _temporary_cwd(bundle_root):
            result = self._vad_library.ten_vad_create(
                POINTER(c_void_p)(self._vad_handler),
                c_size_t(self._hop_size),
                c_float(self._threshold),
            )
        if result != 0:
            raise RuntimeError("bundled TEN VAD create failed")

    def process(self, audio_data: Any) -> tuple[float, int]:
        audio_data = audio_data.squeeze()
        if len(audio_data.shape) != 1 or audio_data.shape[0] != self._hop_size:
            raise ValueError(f"TEN VAD audio data shape should be [{self._hop_size}]")
        input_pointer = c_void_p(audio_data.__array_interface__["data"][0])
        result = self._vad_library.ten_vad_process(
            self._vad_handler,
            input_pointer,
            c_size_t(self._hop_size),
            POINTER(c_float)(self._out_probability),
            POINTER(c_int32)(self._out_flags),
        )
        if result != 0:
            raise RuntimeError("bundled TEN VAD processing failed")
        return self._out_probability.value, self._out_flags.value

    def __del__(self) -> None:
        try:
            if self._vad_handler:
                self._vad_library.ten_vad_destroy(POINTER(c_void_p)(self._vad_handler))
                self._vad_handler = c_void_p(0)
        except Exception:
            pass


# ── Factory ─────────────────────────────────────────────────────────


@dataclass
class VADConfig:
    """Configuration for VAD factory."""

    # "krisp", "ten", "silero", or "auto" (try krisp -> ten -> silero)
    backend: str = "auto"
    # Krisp-specific
    krisp_model_path: str | None = None
    # Shared VAD settings
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 150
    sensitivity: float = 0.5
    pre_roll_ms: int = 100
    post_roll_ms: int = 100


def create_vad(config: VADConfig | None = None) -> Any:
    """Create the best available VAD provider.

    Selection order:
      1. If config.backend == "krisp": use Krisp (fail if unavailable)
      2. If config.backend == "ten": use TEN VAD (fail if unavailable)
      3. If config.backend == "silero": use Silero (fail if unavailable)
      4. If config.backend == "auto" (default):
         - Try Krisp first
         - Fall back to TEN VAD
         - Fall back to Silero

    Returns an object satisfying the VADProvider protocol.
    """
    cfg = config or VADConfig()

    def _configure(vad: Any) -> Any:
        vad.configure(
            min_speech_duration_ms=cfg.min_speech_duration_ms,
            min_silence_duration_ms=cfg.min_silence_duration_ms,
            sensitivity=cfg.sensitivity,
            pre_roll_ms=cfg.pre_roll_ms,
            post_roll_ms=cfg.post_roll_ms,
        )
        return vad

    if cfg.backend == "krisp":
        return _configure(KrispVAD(model_path=cfg.krisp_model_path))

    if cfg.backend == "silero":
        return _configure(SileroVAD())

    if cfg.backend == "ten":
        return _configure(TenVAD())

    # Auto mode: try Krisp -> TEN -> Silero
    try:
        return _configure(KrispVAD(model_path=cfg.krisp_model_path))
    except (RuntimeError, ImportError):
        logger.info("Krisp VAD not available, trying TEN fallback")

    try:
        return _configure(TenVAD())
    except (RuntimeError, ImportError):
        logger.info("TEN VAD not available, trying Silero fallback")

    try:
        return _configure(SileroVAD())
    except (RuntimeError, ImportError):
        logger.info("Silero VAD not available either")
        raise RuntimeError(
            "No VAD backend available. Install easycat[ten-vad], easycat[silero-vad], "
            "or krisp-audio (for Krisp)."
        )
