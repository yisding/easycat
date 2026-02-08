"""Voice Activity Detection implementations: Silero (open-source) and Krisp (commercial).

Both implement the VADProvider protocol from providers.py:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]
    def configure(self, ...) -> None

The factory function `create_vad` selects the best available backend
with automatic fallback from Krisp -> Silero.
"""

from __future__ import annotations

import logging
import struct
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
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


# ── VAD base class ────────────────────────────────────────────────


class _VADBase:
    """Internal base class holding the shared VAD state machine.

    Provides the threshold + timing state, ``configure()``, the synchronous
    ``_evaluate_speech()`` generator, and ``reset()`` for the common variables.
    """

    def __init__(self) -> None:
        self._threshold: float = 0.5
        self._min_speech_duration_ms: int = 250
        self._min_silence_duration_ms: int = 300
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
        min_silence_duration_ms: int = 300,
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


# ── Silero VAD (open-source) ────────────────────────────────────────


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

        # Accumulation buffer for sub-frame chunks
        self._buffer: bytes = b""

        self._load_model()

    def _load_model(self) -> None:
        """Load the Silero VAD model."""
        torch = require_module("torch", extra="all", purpose="Silero VAD")
        try:
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to load Silero VAD model: {exc}") from exc
        self._model = model
        self._torch = torch
        logger.info("Silero VAD model loaded successfully")

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process an audio chunk and yield VAD events."""
        if self._torch is None:
            self._torch = require_module("torch", extra="all", purpose="Silero VAD")
        torch = self._torch

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
            tensor = torch.FloatTensor(float_samples)

            # Run model inference
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
        krisp_audio = require_module("krisp_audio", extra="krisp", purpose="Krisp VAD")
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
            self._krisp_audio = require_module(
                "krisp_audio", extra="krisp", purpose="Krisp VAD"
            )
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

    def __del__(self) -> None:
        self.close()


# ── Factory ─────────────────────────────────────────────────────────


@dataclass
class VADConfig:
    """Configuration for VAD factory."""

    # "krisp", "silero", or "auto" (try krisp first, then silero)
    backend: str = "auto"
    # Krisp-specific
    krisp_model_path: str | None = None
    # Shared VAD settings
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 300
    sensitivity: float = 0.5
    pre_roll_ms: int = 100
    post_roll_ms: int = 100


def create_vad(config: VADConfig | None = None) -> Any:
    """Create the best available VAD provider.

    Selection order:
      1. If config.backend == "krisp": use Krisp (fail if unavailable)
      2. If config.backend == "silero": use Silero (fail if unavailable)
      3. If config.backend == "auto" (default):
         - Try Krisp first
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

    # Auto mode: try Krisp -> Silero
    try:
        return _configure(KrispVAD(model_path=cfg.krisp_model_path))
    except RuntimeError:
        logger.info("Krisp VAD not available, trying Silero fallback")

    try:
        return _configure(SileroVAD())
    except RuntimeError:
        logger.info("Silero VAD not available either")
        raise RuntimeError(
            "No VAD backend available. Install torch (for Silero) or krisp-audio (for Krisp)."
        )
