"""TEN VAD backend (via the ``ten-vad`` PyPI package, optional extra)."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from importlib.metadata import version
from typing import Any

from easycat._audio_utils import resample_chunk
from easycat._extras import require_module
from easycat.audio_format import AudioChunk
from easycat.events import Event
from easycat.vad._base import _VADBase

logger = logging.getLogger(__name__)

# TEN VAD expects 16 kHz audio and defaults to a hop size of 256 samples.
_TEN_SAMPLE_RATE = 16000
_TEN_HOP_SAMPLES = 256
_TEN_DEFAULT_THRESHOLD = 0.6
_TEN_DEFAULT_SENSITIVITY = 1.0 - _TEN_DEFAULT_THRESHOLD


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
