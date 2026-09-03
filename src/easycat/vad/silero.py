"""Silero VAD backend using the bundled ONNX runtime model."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from easycat._audio_utils import AudioFrameAligner, PCM16StreamResampler, to_mono_chunk
from easycat._extras import require_module
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import Event
from easycat.vad._base import _VADBase

logger = logging.getLogger(__name__)

# Silero v6.2.1 VAD accepts 8 kHz or 16 kHz mono audio via the ``sr`` input.
# Telephony transports feed 8 kHz natively; WebRTC / local mic typically
# arrives at 16 kHz (or is resampled there from higher rates).  Chunk and
# context sizes are fixed 32 ms windows at each rate.
_SILERO_SUPPORTED_RATES: tuple[int, ...] = (8000, 16000)
_SILERO_DEFAULT_RATE = 16000
_SILERO_FRAME_SAMPLES_AT: dict[int, int] = {8000: 256, 16000: 512}
_SILERO_CONTEXT_SAMPLES_AT: dict[int, int] = {8000: 32, 16000: 64}
_SILERO_ONNX_MODEL = Path(__file__).parent.parent / "models" / "silero_vad.onnx"


@dataclass
class _OnnxSessionEntry:
    session: Any
    owners: int


_ONNX_SESSION_CACHE: dict[tuple[int, str], _OnnxSessionEntry] = {}
_ONNX_SESSION_CACHE_LOCK = threading.Lock()


def _reset_onnx_session_cache_after_fork() -> None:
    global _ONNX_SESSION_CACHE, _ONNX_SESSION_CACHE_LOCK
    _ONNX_SESSION_CACHE = {}
    _ONNX_SESSION_CACHE_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_onnx_session_cache_after_fork)


def _silero_backend_override() -> str | None:
    override = os.getenv("EASYCAT_SILERO_BACKEND", "").strip().lower()
    if override in {"torch", "onnx"}:
        return override
    return None


def _silero_backend_candidates() -> tuple[str, ...]:
    override = _silero_backend_override()
    if override is not None:
        return (override,)
    return ("onnx",)


def _silero_onnx_model_path() -> str:
    if not _SILERO_ONNX_MODEL.exists():
        raise RuntimeError(f"Bundled Silero VAD ONNX model file not found: {_SILERO_ONNX_MODEL}")
    return str(_SILERO_ONNX_MODEL)


def _acquire_onnx_session(model_path: str, onnxruntime: Any) -> tuple[tuple[int, str], Any]:
    """Load each immutable ONNX graph once while keeping VAD state per instance."""
    cache_key = (os.getpid(), model_path)
    with _ONNX_SESSION_CACHE_LOCK:
        cached = _ONNX_SESSION_CACHE.get(cache_key)
        if cached is not None:
            cached.owners += 1
            return cache_key, cached.session

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1

        providers = None
        available = onnxruntime.get_available_providers()
        if "CPUExecutionProvider" in available:
            providers = ["CPUExecutionProvider"]

        if providers is None:
            session = onnxruntime.InferenceSession(model_path, sess_options=opts)
        else:
            session = onnxruntime.InferenceSession(
                model_path,
                providers=providers,
                sess_options=opts,
            )
        _ONNX_SESSION_CACHE[cache_key] = _OnnxSessionEntry(session=session, owners=1)
        return cache_key, session


def _release_onnx_session(cache_key: tuple[int, str], session: Any) -> None:
    with _ONNX_SESSION_CACHE_LOCK:
        cached = _ONNX_SESSION_CACHE.get(cache_key)
        if cached is None or cached.session is not session:
            return
        cached.owners -= 1
        if cached.owners <= 0:
            del _ONNX_SESSION_CACHE[cache_key]


class _SileroOnnxModel:
    """Small ONNX-only Silero wrapper that mirrors the recurrent model contract."""

    def __init__(self, model_path: str) -> None:
        numpy = require_module("numpy", extra="silero-vad", purpose="Silero VAD ONNX")
        onnxruntime = require_module("onnxruntime", extra="silero-vad", purpose="Silero VAD ONNX")
        self._cache_key: tuple[int, str] | None = None
        self._session: Any = None
        self._numpy = numpy
        try:
            self._cache_key, self._session = _acquire_onnx_session(model_path, onnxruntime)
            self.reset_states()
        except Exception:
            self.close()
            raise

    def reset_states(self) -> None:
        np = self._numpy
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 0), dtype=np.float32)
        self._last_sr = 0

    def close(self) -> None:
        """Release the onnxruntime InferenceSession handle."""
        cache_key = getattr(self, "_cache_key", None)
        session = getattr(self, "_session", None)
        if cache_key is not None and session is not None:
            _release_onnx_session(cache_key, session)
        self._cache_key = None
        self._session = None

    def __del__(self) -> None:
        self.close()

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

    Loads the bundled Silero VAD ONNX model and processes audio
    chunks to detect speech start/stop. Emits VADStartSpeaking and
    VADStopSpeaking events.

    Configurable parameters:
      - min_speech_duration_ms: minimum duration of speech to trigger start
      - min_silence_duration_ms: minimum silence to trigger stop
      - sensitivity: detection threshold (0.0-1.0, lower = more sensitive)
    """

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._backend: str | None = None

        # Accumulation buffer for sub-frame chunks
        self._buffer: bytes = b""
        self._buffer_rate: int | None = None
        self._audio_resampler = PCM16StreamResampler(_SILERO_DEFAULT_RATE)
        self._source_frame_aligner = AudioFrameAligner()

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
                # A single backend being unavailable is an expected fallback,
                # so log at debug; the aggregate RuntimeError below surfaces if
                # every backend candidate fails.
                logger.debug("Silero VAD %s backend unavailable: %s", backend, exc)

        joined = "; ".join(errors) or "no backend candidates"
        raise RuntimeError(f"Failed to load Silero VAD model: {joined}")

    def _load_torch_model(self) -> None:
        raise RuntimeError(
            "Silero VAD torch backend is disabled because torch.hub loads remote "
            "Python code without repository pinning or hash verification. Use the "
            "bundled ONNX model instead: uv add 'easycat[silero-vad]'. From the "
            "EasyCat repo, use: uv sync --extra silero-vad --group dev."
        )

    def _load_onnx_model(self) -> None:
        try:
            model_path = _silero_onnx_model_path()
            self._model = _SileroOnnxModel(model_path)
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"onnx loader failed: {exc}") from exc
        self._backend = "onnx"

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:  # noqa: C901, PLR0912
        """Process an audio chunk and yield VAD events.

        The chunk must be mono PCM16; the byte stream is decoded as a flat
        int16 sequence. Interleaved multi-channel input is downmixed to mono
        first so frame boundaries and resampling stay correct.
        """
        np = require_module("numpy", extra="silero-vad", purpose="Silero VAD ONNX")
        chunk = self._source_frame_aligner.align(chunk)
        if chunk.format.channels > 1:
            chunk = to_mono_chunk(chunk)
        # Silero v6.2.1 handles 8 kHz and 16 kHz natively.  Anything else (24 k,
        # 48 k, …) resamples to 16 kHz to preserve fidelity.
        if chunk.format.sample_rate not in _SILERO_SUPPORTED_RATES:
            chunk = AudioChunk(
                data=self._audio_resampler.process(
                    chunk.data,
                    chunk.format.sample_rate,
                ),
                format=PCM16_MONO_16K,
                timestamp=chunk.timestamp,
            )
        elif self._audio_resampler.source_rate is not None:
            # A native-rate chunk starts a new format segment. Flush the old
            # converter tail and feed it through VAD before resetting, instead
            # of discarding ~32ms of audio (gh 1034).
            tail = self._audio_resampler.finish()
            if tail:
                if self._buffer_rate == _SILERO_DEFAULT_RATE and chunk.format.sample_rate == 8000:
                    # Tail is 16 kHz while next chunk is 8 kHz — flush 16 kHz
                    # frames before discarding partial remainder, otherwise
                    # appending tail then clearing for rate switch drops it.
                    self._buffer += tail
                    frame_samples_old = _SILERO_FRAME_SAMPLES_AT[_SILERO_DEFAULT_RATE]
                    frame_bytes_old = frame_samples_old * 2
                    while len(self._buffer) >= frame_bytes_old:
                        frame_data = self._buffer[:frame_bytes_old]
                        self._buffer = self._buffer[frame_bytes_old:]
                        float_samples = (
                            np.frombuffer(frame_data, dtype="<i2").astype(np.float32) / 32768.0
                        )
                        speech_prob = self._model.predict(float_samples, _SILERO_DEFAULT_RATE)
                        audio_time_s = self._advance_audio_time(
                            frame_samples_old / _SILERO_DEFAULT_RATE
                        )
                        for event in self._evaluate_speech(speech_prob, audio_time_s):
                            yield event
                        if len(self._buffer) >= frame_bytes_old:
                            await asyncio.sleep(0)
                    self._buffer = b""
                else:
                    self._buffer += tail
        target_rate = chunk.format.sample_rate

        # A mid-stream 8k<->16k switch would concatenate old-rate remainder
        # bytes with new-rate bytes and slice at the new frame size, garbling
        # one boundary frame. Drop the stale remainder instead of raising —
        # VAD is continuous.
        if self._buffer_rate is not None and self._buffer_rate != target_rate:
            self._buffer = b""
        self._buffer_rate = target_rate

        # Accumulate into buffer
        self._buffer += chunk.data

        # Process complete frames
        frame_samples = _SILERO_FRAME_SAMPLES_AT[target_rate]
        frame_bytes = frame_samples * 2  # 2 bytes per PCM16 sample
        while len(self._buffer) >= frame_bytes:
            frame_data = self._buffer[:frame_bytes]
            self._buffer = self._buffer[frame_bytes:]

            # Convert PCM16 to float32 tensor
            float_samples = np.frombuffer(frame_data, dtype="<i2").astype(np.float32) / 32768.0

            # Run predict inline rather than via asyncio.to_thread: the ONNX
            # call is ~100us at ~31 frames/s (<0.5% of the 32ms frame budget),
            # so the ~40us thread-hop dispatch adds latency and a context
            # switch per frame without meaningfully freeing the event loop.
            speech_prob = self._model.predict(float_samples, target_rate)
            audio_time_s = self._advance_audio_time(frame_samples / target_rate)

            for event in self._evaluate_speech(speech_prob, audio_time_s):
                yield event

            # A transport may deliver many frames in one chunk (e.g. a buffered
            # WebSocket binary message), so yield to the event loop between
            # frames to keep the pipeline task from monopolizing it on a large
            # backlog.  Single-frame chunks (local mic) skip this entirely.
            if len(self._buffer) >= frame_bytes:
                await asyncio.sleep(0)

    async def warmup(self) -> None:
        """Prime the ONNX session with one zeroed 16 kHz frame.

        The first ``predict`` call pays the onnxruntime graph-allocation and
        kernel-compilation cost; running it on a silent 512-sample frame at
        startup keeps that cost off the first real turn.  State is reset
        afterwards so the warm frame does not leak into live detection.  All
        failures are swallowed — ``WarmupRunner`` re-raises, so a warmup
        error here must not abort ``Session.start()``.
        """
        if self._model is None:
            return
        try:
            frame = [0.0] * _SILERO_FRAME_SAMPLES_AT[_SILERO_DEFAULT_RATE]
            self._model.predict(frame, _SILERO_DEFAULT_RATE)
            self._model.reset_states()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("Silero VAD warmup skipped: %s", exc)

    def reset(self) -> None:
        """Reset VAD internal state."""
        super().reset()
        self._audio_resampler.reset()
        self._source_frame_aligner.reset()
        self._buffer = b""
        self._buffer_rate = None
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                pass

    def close(self) -> None:
        """Release the loaded model (onnxruntime session)."""
        super().close()
        self._source_frame_aligner.reset()
        self._buffer = b""
        self._buffer_rate = None

    def __del__(self) -> None:
        self.close()

    def version_info(self) -> dict[str, str]:
        sdk_ver = "unknown"
        try:
            sdk_ver = version("onnxruntime")
        except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
            pass
        model_name = "silero-vad-v6.2.1-onnx" if self._backend == "onnx" else "silero-vad-unknown"
        return {
            "provider": "silero",
            "model": model_name,
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }
