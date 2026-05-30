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
from typing import Any, Literal, TypeAlias, cast

from easycat._audio_utils import resample_chunk
from easycat._extras import require_module
from easycat.audio_format import PCM16_MONO_48K, AudioChunk

logger = logging.getLogger(__name__)

# Frame size expected by RNNoise: 480 samples at 48 kHz (10 ms)
_RNNOISE_FRAME_SAMPLES = 480
NoiseReducerBackend: TypeAlias = Literal["auto", "krisp", "rnnoise"]
_VALID_NOISE_REDUCER_BACKENDS: tuple[NoiseReducerBackend, ...] = (
    "auto",
    "krisp",
    "rnnoise",
)
NoiseReducerFallbackPolicy: TypeAlias = Literal["passthrough", "error"]
_VALID_NOISE_REDUCER_FALLBACK_POLICIES: tuple[NoiseReducerFallbackPolicy, ...] = (
    "passthrough",
    "error",
)
# Actionable hint surfaced when auto-mode finds no real backend installed.
_NOISE_REDUCER_INSTALL_HINT = (
    "No noise-reduction backend is installed. Install easycat[rnnoise] "
    "for the open-source RNNoise backend, or configure Krisp (krisp-audio). "
    "Pass NoiseReducerConfig(backend='rnnoise') / backend='krisp' to require a "
    "specific backend, or fallback_policy='error' to fail loudly instead of "
    "passing audio through unchanged."
)


def _validate_noise_reducer_backend(backend: str) -> NoiseReducerBackend:
    if backend not in _VALID_NOISE_REDUCER_BACKENDS:
        allowed = ", ".join(_VALID_NOISE_REDUCER_BACKENDS)
        raise ValueError(f"Unknown noise reducer backend '{backend}'. Expected one of: {allowed}.")
    return cast(NoiseReducerBackend, backend)


def _validate_noise_reducer_fallback_policy(policy: str) -> NoiseReducerFallbackPolicy:
    if policy not in _VALID_NOISE_REDUCER_FALLBACK_POLICIES:
        allowed = ", ".join(_VALID_NOISE_REDUCER_FALLBACK_POLICIES)
        raise ValueError(
            f"Unknown noise reducer fallback_policy '{policy}'. Expected one of: {allowed}."
        )
    return cast(NoiseReducerFallbackPolicy, policy)


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
        # Remainder buffer of resampled 48 kHz PCM16 bytes carried across
        # calls.  RNNoise is a stateful recurrent (GRU) denoiser that advances
        # its filter a whole 480-sample frame at a time; zero-padding the tail
        # of every chunk would inject silence into the middle of the stream and
        # pollute the recurrent state at each chunk boundary.  Mirroring the VAD
        # backends, we accumulate whole frames here and defer the sub-frame
        # remainder to the next call.
        self._buffer_48k: bytes = b""
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

        Only whole 480-sample (10 ms) 48 kHz frames are submitted to the
        recurrent filter; the sub-frame remainder is buffered for the next
        call rather than zero-padded mid-stream.  The returned chunk therefore
        covers exactly the whole frames flushed this call and may differ in
        length from the input by less than one 10 ms frame.  Call ``flush()``
        to drain a trailing partial frame at end-of-stream.
        """
        original_rate = chunk.format.sample_rate

        # Step 1: Resample to 48 kHz if needed, then accumulate.
        chunk_48k = resample_chunk(chunk, 48000)
        self._buffer_48k += chunk_48k.data

        cleaned_48k = self._process_buffered_frames(flush=False)

        # Step 2: Resample the cleaned 48 kHz audio back to the input rate.
        cleaned = AudioChunk(data=cleaned_48k, format=PCM16_MONO_48K, timestamp=chunk.timestamp)
        return resample_chunk(cleaned, original_rate)

    def _process_buffered_frames(self, *, flush: bool) -> bytes:
        """Run buffered 48 kHz PCM16 through RNNoise, whole frames only.

        When ``flush`` is True the trailing sub-frame remainder is zero-padded
        and processed as a final frame (end-of-stream); otherwise it is
        retained in ``self._buffer_48k`` for the next call.
        """
        import numpy as np

        frame_bytes = self._frame_samples * 2  # 2 bytes per PCM16 sample
        output_samples: list[int] = []

        while len(self._buffer_48k) >= frame_bytes:
            frame_data = self._buffer_48k[:frame_bytes]
            self._buffer_48k = self._buffer_48k[frame_bytes:]
            frame = np.frombuffer(frame_data, dtype=np.int16)
            processed, _ = self._rnnoise.process_mono_frame(self._state, frame.copy())
            output_samples.extend(
                max(-32768, min(32767, int(round(v)))) for v in processed[: self._frame_samples]
            )

        if flush and self._buffer_48k:
            tail = np.frombuffer(self._buffer_48k, dtype=np.int16)
            valid = len(tail)
            padded = np.pad(tail, (0, self._frame_samples - valid), mode="constant")
            self._buffer_48k = b""
            processed, _ = self._rnnoise.process_mono_frame(self._state, padded.copy())
            output_samples.extend(
                max(-32768, min(32767, int(round(v)))) for v in processed[:valid]
            )

        return struct.pack(f"<{len(output_samples)}h", *output_samples)

    def flush(self) -> AudioChunk:
        """Drain any buffered trailing audio, zero-padding the final frame.

        Returns the cleaned tail at 48 kHz (empty data when nothing is
        buffered).  Call at end-of-stream so a final partial frame is not
        silently dropped.
        """
        cleaned_48k = self._process_buffered_frames(flush=True)
        return AudioChunk(data=cleaned_48k, format=PCM16_MONO_48K)

    def close(self) -> None:
        """Release RNNoise state."""
        self._buffer_48k = b""
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
    backend: NoiseReducerBackend = "auto"
    # What ``auto`` mode does when no real backend is installed:
    #   "passthrough" (default) — log a warning and pass audio through unchanged
    #     (graceful degradation, mirrors EchoCancellationConfig.fallback_policy).
    #   "error" — raise RuntimeError with an actionable install hint, the
    #     safest-in-prod behavior. Forcing ``backend="rnnoise"``/"krisp" also
    #     fails loudly when that specific backend is missing.
    fallback_policy: NoiseReducerFallbackPolicy = "passthrough"
    # Krisp-specific
    krisp_model_path: str | None = None

    def __post_init__(self) -> None:
        self.backend = _validate_noise_reducer_backend(self.backend)
        self.fallback_policy = _validate_noise_reducer_fallback_policy(self.fallback_policy)


class PassthroughNoiseReducer:
    """No-op reducer that passes audio through unchanged. Last-resort fallback."""

    is_passthrough_provider = True

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
         - When neither is installed, honor ``fallback_policy``:
           "passthrough" (default) logs a warning and returns a no-op
           passthrough; "error" raises RuntimeError with an install hint.

    Returns an object satisfying the NoiseReducer protocol.
    """
    cfg = config or NoiseReducerConfig()
    cfg.backend = _validate_noise_reducer_backend(cfg.backend)
    cfg.fallback_policy = _validate_noise_reducer_fallback_policy(cfg.fallback_policy)

    if cfg.backend == "krisp":
        return KrispNoiseReducer(model_path=cfg.krisp_model_path)

    if cfg.backend == "rnnoise":
        return RNNoiseReducer()

    # Auto mode: try Krisp -> RNNoise -> fallback_policy
    try:
        return KrispNoiseReducer(model_path=cfg.krisp_model_path)
    except (RuntimeError, ImportError):
        logger.info("Krisp not available, trying RNNoise fallback")

    try:
        return RNNoiseReducer()
    except (RuntimeError, ImportError):
        logger.info("RNNoise not available")

    if cfg.fallback_policy == "error":
        raise RuntimeError(_NOISE_REDUCER_INSTALL_HINT)

    logger.warning(
        "%s Falling back to passthrough (no noise reduction).", _NOISE_REDUCER_INSTALL_HINT
    )
    return PassthroughNoiseReducer()
