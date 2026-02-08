"""Noise reduction implementations: RNNoise (open-source) and Krisp (commercial).

Both implement the NoiseReducer protocol from providers.py:
    async def process(self, chunk: AudioChunk) -> AudioChunk

The factory function `create_noise_reducer` selects the best available backend
with automatic fallback from Krisp -> RNNoise -> passthrough.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Any

from easycat.audio_format import PCM16_MONO_48K, AudioChunk
from easycat.audio_utils import resample_chunk
from easycat.extras import require_module

logger = logging.getLogger(__name__)

# Frame size expected by RNNoise: 480 samples at 48 kHz (10 ms)
_RNNOISE_FRAME_SAMPLES = 480


# ── RNNoise integration (open-source fallback) ─────────────────────


def _pcm16_to_float32(data: bytes) -> list[float]:
    """Convert PCM16 LE bytes to float32 samples in [-1.0, 1.0]."""
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data)
    return [s / 32768.0 for s in samples]


def _float32_to_pcm16(samples: list[float]) -> bytes:
    """Convert float32 samples in [-1.0, 1.0] to PCM16 LE bytes."""
    clamped = [max(-32768, min(32767, int(round(s * 32768.0)))) for s in samples]
    return struct.pack(f"<{len(clamped)}h", *clamped)


class RNNoiseReducer:
    """Noise reducer using RNNoise (open-source, C library via ctypes/cffi).

    RNNoise expects 48 kHz float32 input in frames of 480 samples (10 ms).
    Internal pipeline: PCM16 at pipeline rate -> resample to 48 kHz ->
    convert to float32 -> RNNoise process -> convert back to PCM16 ->
    resample to pipeline rate.

    If the rnnoise shared library is not available, raises RuntimeError
    on construction.
    """

    def __init__(self) -> None:
        self._rnnoise: Any = None
        self._state: Any = None
        self._load_rnnoise()

    def _load_rnnoise(self) -> None:
        """Attempt to load the RNNoise shared library."""
        try:
            import ctypes
            import ctypes.util

            lib_path = ctypes.util.find_library("rnnoise")
            if lib_path is None:
                raise RuntimeError(
                    "RNNoise shared library not found. "
                    "Install librnnoise-dev or place librnnoise.so on the library path."
                )
            self._rnnoise = ctypes.CDLL(lib_path)

            # Set proper restype/argtypes so ctypes handles 64-bit
            # pointers correctly instead of truncating to c_int.
            # DenoiseState* rnnoise_create(RNNModel *model)
            self._rnnoise.rnnoise_create.restype = ctypes.c_void_p
            self._rnnoise.rnnoise_create.argtypes = [ctypes.c_void_p]
            # float rnnoise_process_frame(DenoiseState *st, float *out, const float *in)
            self._rnnoise.rnnoise_process_frame.restype = ctypes.c_float
            self._rnnoise.rnnoise_process_frame.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_float),
            ]
            # void rnnoise_destroy(DenoiseState *st)
            self._rnnoise.rnnoise_destroy.restype = None
            self._rnnoise.rnnoise_destroy.argtypes = [ctypes.c_void_p]

            self._state = self._rnnoise.rnnoise_create(None)
            if not self._state:
                raise RuntimeError("Failed to create RNNoise state.")
            logger.info("RNNoise loaded successfully from %s", lib_path)
        except OSError as exc:
            raise RuntimeError(f"Failed to load RNNoise library: {exc}") from exc

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process an audio chunk through RNNoise for noise reduction.

        Handles resampling to/from 48 kHz and float32 conversion internally.
        """
        import ctypes

        original_rate = chunk.format.sample_rate

        # Step 1: Resample to 48 kHz if needed
        chunk_48k = resample_chunk(chunk, 48000)

        # Step 2: Convert to float32
        float_samples = _pcm16_to_float32(chunk_48k.data)

        # Step 3: Process in 480-sample (10 ms) frames through RNNoise
        output_samples: list[float] = []
        i = 0
        while i < len(float_samples):
            frame = float_samples[i : i + _RNNOISE_FRAME_SAMPLES]
            if len(frame) < _RNNOISE_FRAME_SAMPLES:
                # Pad the last frame with zeros
                frame = frame + [0.0] * (_RNNOISE_FRAME_SAMPLES - len(frame))

            # RNNoise expects float* input/output scaled to [-32768, 32767]
            in_buf = (ctypes.c_float * _RNNOISE_FRAME_SAMPLES)(*(s * 32768.0 for s in frame))
            out_buf = (ctypes.c_float * _RNNOISE_FRAME_SAMPLES)()
            self._rnnoise.rnnoise_process_frame(self._state, out_buf, in_buf)

            processed = [out_buf[j] / 32768.0 for j in range(_RNNOISE_FRAME_SAMPLES)]
            output_samples.extend(processed[: min(_RNNOISE_FRAME_SAMPLES, len(float_samples) - i)])
            i += _RNNOISE_FRAME_SAMPLES

        # Step 4: Convert back to PCM16
        pcm_data = _float32_to_pcm16(output_samples)
        cleaned_48k = AudioChunk(data=pcm_data, format=PCM16_MONO_48K, timestamp=chunk.timestamp)

        # Step 5: Resample back to original rate
        return resample_chunk(cleaned_48k, original_rate)

    def close(self) -> None:
        """Release RNNoise state."""
        if self._state and self._rnnoise:
            try:
                self._rnnoise.rnnoise_destroy(self._state)
            except Exception:
                pass
            self._state = None

    def __del__(self) -> None:
        self.close()


# ── Krisp integration (commercial) ─────────────────────────────────


class KrispNoiseReducer:
    """Noise reducer using Krisp SDK (commercial voice isolation).

    Requires the Krisp SDK to be installed and a valid license.
    If Krisp is not configured or the license is missing, raises RuntimeError.
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._session: Any = None
        self._model_path = model_path
        self._krisp_audio: Any = None
        self._initialize()

    def _initialize(self) -> None:
        """Initialize the Krisp SDK and create a noise cancellation session."""
        try:
            krisp_audio = require_module(
                "krisp_audio", purpose="Krisp noise reduction"
            )
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        config = {}
        if self._model_path:
            config["model_path"] = self._model_path
        try:
            self._session = krisp_audio.create_noise_cancellation_session(**config)
        except Exception as exc:
            raise RuntimeError(
                f"Krisp SDK initialization failed (license or config issue): {exc}"
            ) from exc
        self._krisp_audio = krisp_audio
        logger.info("Krisp noise reduction initialized")

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process audio through Krisp noise cancellation."""
        if self._krisp_audio is None:
            self._krisp_audio = require_module(
                "krisp_audio", purpose="Krisp noise reduction"
            )
        cleaned_data = self._krisp_audio.process_frame(
            self._session, chunk.data, chunk.format.sample_rate
        )
        return AudioChunk(
            data=cleaned_data,
            format=chunk.format,
            timestamp=chunk.timestamp,
        )

    def close(self) -> None:
        """Release Krisp session resources."""
        if self._session is not None:
            try:
                if self._krisp_audio is None:
                    self._krisp_audio = require_module(
                        "krisp_audio", purpose="Krisp noise reduction"
                    )
                self._krisp_audio.destroy_session(self._session)
            except Exception:
                pass
            self._session = None

    def __del__(self) -> None:
        self.close()


# ── Factory ────────────────────────────────────────────────────────


@dataclass
class NoiseReducerConfig:
    """Configuration for noise reducer factory."""

    # "krisp", "rnnoise", or "auto" (try krisp first, then rnnoise)
    backend: str = "auto"
    # Krisp-specific
    krisp_model_path: str | None = None


class PassthroughNoiseReducer:
    """No-op reducer that passes audio through unchanged. Last-resort fallback."""

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


def create_noise_reducer(config: NoiseReducerConfig | None = None) -> Any:
    """Create the best available noise reducer.

    Selection order:
      1. If config.backend == "krisp": use Krisp (fail if unavailable)
      2. If config.backend == "rnnoise": use RNNoise (fail if unavailable)
      3. If config.backend == "auto" (default):
         - Try Krisp first
         - Fall back to RNNoise
         - Fall back to passthrough (no noise reduction)

    Returns an object satisfying the NoiseReducer protocol.
    """
    cfg = config or NoiseReducerConfig()

    if cfg.backend == "krisp":
        return KrispNoiseReducer(model_path=cfg.krisp_model_path)

    if cfg.backend == "rnnoise":
        return RNNoiseReducer()

    # Auto mode: try Krisp -> RNNoise -> passthrough
    try:
        return KrispNoiseReducer(model_path=cfg.krisp_model_path)
    except (RuntimeError, ImportError):
        logger.info("Krisp not available, trying RNNoise fallback")

    try:
        return RNNoiseReducer()
    except (RuntimeError, ImportError):
        logger.info("RNNoise not available, falling back to passthrough")

    return PassthroughNoiseReducer()
