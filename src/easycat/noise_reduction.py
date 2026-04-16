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


class RNNoiseReducer:
    """Noise reducer using pyrnnoise (open-source RNNoise bindings).

    RNNoise expects 48 kHz float32 input in frames of 480 samples (10 ms).
    Internal pipeline: PCM16 at pipeline rate -> resample to 48 kHz ->
    convert to float32 -> RNNoise process -> convert back to PCM16 ->
    resample to pipeline rate.

    Requires the ``pyrnnoise`` package.
    """

    def __init__(self) -> None:
        self._rnnoise: Any = None
        self._state: Any = None
        self._frame_samples: int = _RNNOISE_FRAME_SAMPLES
        self._load_rnnoise()

    def _load_rnnoise(self) -> None:
        """Attempt to load RNNoise via pyrnnoise."""
        try:
            self._rnnoise = require_module(
                "pyrnnoise.rnnoise",
                extra="rnnoise",
                purpose="RNNoise",
            )
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc

        self._frame_samples = int(getattr(self._rnnoise, "FRAME_SIZE", _RNNOISE_FRAME_SAMPLES))
        self._state = self._rnnoise.create()
        if not self._state:
            raise RuntimeError("Failed to create RNNoise state.")
        logger.info("RNNoise loaded successfully via pyrnnoise")

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process an audio chunk through RNNoise for noise reduction.

        Handles resampling to/from 48 kHz and float32 conversion internally.
        """
        import numpy as np

        original_rate = chunk.format.sample_rate

        # Step 1: Resample to 48 kHz if needed
        chunk_48k = resample_chunk(chunk, 48000)

        # Step 2: Convert to int16 samples for pyrnnoise
        samples = np.frombuffer(chunk_48k.data, dtype=np.int16)

        # Step 3: Process in frame chunks through RNNoise
        output_samples: list[int] = []
        i = 0
        while i < len(samples):
            frame = samples[i : i + self._frame_samples]
            if len(frame) < self._frame_samples:
                # Pad the last frame with zeros
                frame = np.pad(frame, (0, self._frame_samples - len(frame)), mode="constant")

            processed, _ = self._rnnoise.process_mono_frame(self._state, frame.copy())
            remaining = min(self._frame_samples, len(samples) - i)
            output_samples.extend(
                max(-32768, min(32767, int(round(v)))) for v in processed[:remaining]
            )
            i += self._frame_samples

        # Step 4: Convert back to PCM16
        pcm_data = struct.pack(f"<{len(output_samples)}h", *output_samples)
        cleaned_48k = AudioChunk(data=pcm_data, format=PCM16_MONO_48K, timestamp=chunk.timestamp)

        # Step 5: Resample back to original rate
        return resample_chunk(cleaned_48k, original_rate)

    def close(self) -> None:
        """Release RNNoise state."""
        if self._state and self._rnnoise:
            try:
                self._rnnoise.destroy(self._state)
            except Exception:
                pass
            self._state = None

    def __del__(self) -> None:
        self.close()

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "rnnoise",
            "model": "rnnoise",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }


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
            krisp_audio = require_module("krisp_audio", purpose="Krisp noise reduction")
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
            self._krisp_audio = require_module("krisp_audio", purpose="Krisp noise reduction")
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

    def version_info(self) -> dict[str, str]:
        sdk_ver = "unknown"
        try:
            from importlib.metadata import version

            sdk_ver = version("krisp-audio")
        except Exception:
            pass
        return {
            "provider": "krisp",
            "model": "krisp-nc",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


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

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "passthrough",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }


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
