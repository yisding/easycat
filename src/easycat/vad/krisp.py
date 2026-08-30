"""Krisp VAD backend (commercial; requires krisp-audio SDK + license)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat._extras import require_module
from easycat.audio_format import AudioChunk
from easycat.events import Event
from easycat.vad._base import _VADBase

logger = logging.getLogger(__name__)


class KrispVAD(_VADBase):
    """Voice activity detection using Krisp VIVA VAD SDK.

    Requires the Krisp SDK with a valid license.
    Same event interface and configuration as Silero.
    """

    def __init__(self, model_path: str | None = None) -> None:
        super().__init__()
        from easycat._audio_utils import AudioFrameAligner

        self._session: Any = None
        self._model_path = model_path
        self._krisp_audio: Any = None
        self._source_frame_aligner = AudioFrameAligner()

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
        # Align frames and downmix to mono so stereo / split-frame input is handled correctly (gh 1029).
        from easycat.audio_format import to_mono_chunk

        chunk = self._source_frame_aligner.align(chunk)
        if chunk.format.channels > 1:
            chunk = to_mono_chunk(chunk)
        if self._krisp_audio is None:
            self._krisp_audio = require_module("krisp_audio", purpose="Krisp VAD")
        speech_prob = self._krisp_audio.vad_process(
            self._session, chunk.data, chunk.format.sample_rate
        )
        audio_time_s = self._advance_audio_time(chunk.duration_ms / 1000.0)

        for event in self._evaluate_speech(speech_prob, audio_time_s):
            yield event

    def reset(self) -> None:
        """Reset VAD internal state."""
        super().reset()
        try:
            self._source_frame_aligner.reset()
        except Exception:
            pass

    def close(self) -> None:
        """Release Krisp session resources."""
        if self._session is not None:
            try:
                if self._krisp_audio is None:
                    self._krisp_audio = require_module("krisp_audio", purpose="Krisp VAD")
                self._krisp_audio.destroy_session(self._session)
            except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                pass
            self._session = None

    def version_info(self) -> dict[str, str]:
        sdk_ver = "unknown"
        try:
            from importlib.metadata import version

            sdk_ver = version("krisp-audio")
        except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
            pass
        return {
            "provider": "krisp",
            "model": "krisp-vad",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }

    def __del__(self) -> None:
        self.close()
