"""Krisp VAD backend (commercial; requires krisp-audio SDK + license)."""

from __future__ import annotations

import logging
import time
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
