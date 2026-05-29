"""Silero VAD backend (open-source, supports PyTorch or ONNX runtime)."""

from __future__ import annotations

import logging
import os
import platform
import struct
import time
from collections.abc import AsyncIterator
from importlib.metadata import version
from pathlib import Path
from typing import Any

from easycat._audio_utils import resample_chunk, to_mono_chunk
from easycat._extras import require_module
from easycat.audio_format import AudioChunk
from easycat.events import Event
from easycat.vad._base import _VADBase

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
_SILERO_ONNX_MODEL = Path(__file__).parent.parent / "models" / "silero_vad.onnx"


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
                # A single backend being unavailable is an expected fallback
                # (e.g. torch missing -> ONNX), so log at debug; the aggregate
                # RuntimeError below surfaces if *every* backend fails.
                logger.debug("Silero VAD %s backend unavailable: %s", backend, exc)

        joined = "; ".join(errors) or "no backend candidates"
        raise RuntimeError(f"Failed to load Silero VAD model: {joined}")

    def _load_torch_model(self) -> None:
        # The torch path is an optional speed-up that falls back to the bundled
        # ONNX model when torch is missing, so we don't point users at the
        # heavyweight ``easycat[all]`` extra (the ``silero-vad`` extra ships the
        # working ONNX backend without torch). A missing torch here is expected
        # and surfaces as a debug-level fallback in ``_load_model``.
        try:
            torch = require_module("torch", purpose="Silero VAD (optional torch backend)")
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
        """Process an audio chunk and yield VAD events.

        The chunk must be mono PCM16; the byte stream is decoded as a flat
        int16 sequence. Interleaved multi-channel input is downmixed to mono
        first so frame boundaries and resampling stay correct.
        """
        if chunk.format.channels > 1:
            chunk = to_mono_chunk(chunk)
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
                    self._torch = require_module(
                        "torch", purpose="Silero VAD (optional torch backend)"
                    )
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
