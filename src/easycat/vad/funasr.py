"""FunASR FSMN-VAD backend via the bundled ONNX model and in-tree runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any

from easycat._audio_utils import AudioFrameAligner, PCM16StreamResampler, to_mono_chunk
from easycat._extras import require_module
from easycat.audio_format import AudioChunk
from easycat.events import Event
from easycat.vad._base import _VADBase, _validate_positive_int

logger = logging.getLogger(__name__)

# FunASR FSMN-VAD is published as a 16 kHz model.  We resample any
# non-16 kHz inputs before streaming chunks through the online runtime.
_FUNASR_SAMPLE_RATE = 16000
_FUNASR_DEFAULT_CHUNK_MS = 50
_FUNASR_BUNDLED_MODEL_DIR = Path(__file__).parent.parent / "models" / "funasr_fsmn_vad"
_FUNASR_DEFAULT_MODEL = str(_FUNASR_BUNDLED_MODEL_DIR)


def _iter_funasr_segment_pairs(value: Any) -> Iterator[tuple[int, int]]:
    """Flatten FunASR segment outputs into ``(beg_ms, end_ms)`` pairs."""
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(item, (int, float)) for item in value):
            yield int(value[0]), int(value[1])
            return
        for item in value:
            yield from _iter_funasr_segment_pairs(item)


class FunASROnnxVAD(_VADBase):
    """Voice activity detection via the bundled FunASR FSMN-VAD ONNX model.

    FunASR's streaming VAD emits absolute segment boundaries rather than
    frame-level speech probabilities.  This adapter translates each
    boundary into a per-frame active/inactive flag and then routes it
    through ``_VADBase._evaluate_speech`` so ``min_speech_duration_ms``
    is applied like the other backends.  The published FunASR FSMN-VAD
    models are 16 kHz; 8 kHz telephony audio is upsampled before
    inference.

    ``sensitivity`` has no effect on this backend because FunASR does
    not expose a probability threshold; ``min_silence_duration_ms`` is
    applied inside the model via ``max_end_sil`` so the shared state
    machine's silence gate is disabled to avoid double-waiting.
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
        if cache_dir is not None:
            raise ValueError(
                "cache_dir is not supported by EasyCat's in-tree FunASR runtime; "
                "pass model_dir/funasr_model_dir to an existing local model directory "
                "or omit cache_dir to use the bundled model assets."
            )
        _validate_positive_int("chunk_size_ms", chunk_size_ms)
        _validate_positive_int("intra_op_num_threads", intra_op_num_threads)

        self._model_dir = model_dir
        self._chunk_size_ms = chunk_size_ms
        self._device_id = device_id
        self._quantize = quantize
        self._intra_op_num_threads = intra_op_num_threads
        self._chunk_samples = int(_FUNASR_SAMPLE_RATE * chunk_size_ms / 1000)
        if self._chunk_samples <= 0:
            raise ValueError("chunk_size_ms produced an empty chunk")

        self._buffer: bytes = b""
        self._buffer_rate: int | None = None
        self._funasr_active: bool = False
        self._numpy: Any = None
        self._model: Any = None
        self._param_dict: dict[str, Any] = {"in_cache": []}
        self._audio_resampler = PCM16StreamResampler(_FUNASR_SAMPLE_RATE)
        self._source_frame_aligner = AudioFrameAligner()
        self._initialize()

    def _initialize(self) -> None:
        try:
            numpy = require_module("numpy", extra="funasr-vad", purpose="FunASR VAD")
            from easycat.vad._funasr_runtime import FunASROnlineRuntime

            model_dir = _resolve_funasr_model_dir(self._model_dir)
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc

        try:
            self._model = FunASROnlineRuntime(
                model_dir=model_dir,
                device_id=self._device_id,
                quantize=self._quantize,
                intra_op_num_threads=self._intra_op_num_threads,
                max_end_sil=self._min_silence_duration_ms,
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
    ) -> None:
        super().configure(
            min_speech_duration_ms=min_speech_duration_ms,
            min_silence_duration_ms=min_silence_duration_ms,
            sensitivity=sensitivity,
        )
        if self._model is not None and hasattr(self._model, "max_end_sil"):
            try:
                self._model.max_end_sil = min_silence_duration_ms
            except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                pass
        # FunASR already applies the silence gate internally via
        # max_end_sil, so disable the shared state machine's silence
        # gate to avoid waiting for the same duration twice.  Speech is
        # still gated by min_speech_duration_ms below.
        self._min_silence_duration_ms = 0
        # FunASR emits binary boundaries rather than probabilities, so
        # any sensitivity value would behave identically.  Pin the
        # threshold to 0.5 so the 1.0 / 0.0 flags we feed _evaluate_speech
        # cross it cleanly.
        self._threshold = 0.5

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process a chunk and yield translated FunASR boundary events.

        The chunk must be mono PCM16; the byte stream is decoded as a flat
        int16 sequence. Interleaved multi-channel input is downmixed to mono
        first so frame boundaries and resampling stay correct.
        """
        if self._numpy is None or self._model is None:
            self._initialize()

        chunk = self._source_frame_aligner.align(chunk)
        if chunk.format.channels > 1:
            chunk = to_mono_chunk(chunk)
        if self._audio_resampler.source_rate is not None and chunk.format.sample_rate == _FUNASR_SAMPLE_RATE:
            self._audio_resampler.reset()
        target_rate = chunk.format.sample_rate
        if self._buffer_rate is not None and self._buffer_rate != target_rate:
            self._buffer = b""
        self._buffer_rate = target_rate

        self._buffer += self._audio_resampler.process(
            chunk.data,
            chunk.format.sample_rate,
        )
        frame_bytes = self._chunk_samples * 2

        while len(self._buffer) >= frame_bytes:
            frame_data = self._buffer[:frame_bytes]
            self._buffer = self._buffer[frame_bytes:]

            waveform = self._numpy.frombuffer(frame_data, dtype=self._numpy.int16)
            waveform = waveform.astype(self._numpy.float32) / 32768.0
            try:
                segments = self._model(audio_in=waveform, param_dict=self._param_dict)
            except Exception as exc:
                raise RuntimeError(f"FunASR ONNX VAD inference failed: {exc}") from exc

            audio_time_s = self._advance_audio_time(self._chunk_size_ms / 1000.0)
            yield_events = self._evaluate_funasr_segments(segments, audio_time_s)
            for event in yield_events:
                yield event

            # Buffered transports may deliver many model frames in one chunk.
            # Yield between frames so cancellation and other pipeline work do
            # not wait for a silent backlog to drain.
            if len(self._buffer) >= frame_bytes:
                await asyncio.sleep(0)

    def _evaluate_funasr_segments(self, segments: Any, now: float) -> Iterator[Event]:
        """Route FunASR boundary pairs through the shared VAD state machine."""
        saw_boundary = False
        for beg_ms, end_ms in _iter_funasr_segment_pairs(segments):
            saw_boundary = True
            boundary_now = now
            if beg_ms >= 0:
                self._funasr_active = True
                yield from self._evaluate_speech(1.0, boundary_now)
            if end_ms >= 0:
                if beg_ms >= 0 and end_ms >= beg_ms:
                    boundary_now = now + (end_ms - beg_ms) / 1000
                if self._funasr_active:
                    yield from self._evaluate_speech(1.0, boundary_now)
                self._funasr_active = False
                yield from self._evaluate_speech(0.0, boundary_now)

        if not saw_boundary:
            speech_prob = 1.0 if self._funasr_active else 0.0
            yield from self._evaluate_speech(speech_prob, now)

    def reset(self) -> None:
        """Reset adapter state and FunASR streaming caches."""
        super().reset()
        self._audio_resampler.reset()
        self._source_frame_aligner.reset()
        self._buffer = b""
        self._buffer_rate = None
        self._funasr_active = False
        self._param_dict = {"in_cache": []}
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()

    def close(self) -> None:
        """Release the FunASR ONNX model handle and streaming caches."""
        super().close()
        audio_resampler = getattr(self, "_audio_resampler", None)
        if audio_resampler is not None:
            audio_resampler.reset()
        source_frame_aligner = getattr(self, "_source_frame_aligner", None)
        if source_frame_aligner is not None:
            source_frame_aligner.reset()
        self._buffer = b""
        self._buffer_rate = None
        self._funasr_active = False
        self._param_dict = {"in_cache": []}

    def __del__(self) -> None:
        self.close()

    def version_info(self) -> dict[str, str]:
        try:
            sdk_ver = version("onnxruntime")
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            sdk_ver = "unknown"
        model_name = self._model_dir
        if self._model_dir == _FUNASR_DEFAULT_MODEL:
            model_name = "funasr-fsmn-vad-bundled"
        return {
            "provider": "funasr",
            "model": model_name,
            "api_version": "unknown",
            "sdk_version": f"in-tree-runtime/onnxruntime-{sdk_ver}",
        }


def _resolve_funasr_model_dir(model_dir: str) -> str:
    if model_dir != _FUNASR_DEFAULT_MODEL:
        return model_dir

    required = ("model.onnx", "config.yaml", "am.mvn")
    missing = [name for name in required if not (_FUNASR_BUNDLED_MODEL_DIR / name).exists()]
    if missing:
        raise ImportError("Bundled FunASR FSMN-VAD assets are missing: " + ", ".join(missing))
    return str(_FUNASR_BUNDLED_MODEL_DIR)
