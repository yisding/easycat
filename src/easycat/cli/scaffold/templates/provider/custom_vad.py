"""EnergyVAD — a custom EasyCat ``VADProvider``.

Implements the Protocol structurally (no base class): ``process`` yields
speech start/stop events, ``configure`` adjusts thresholds, and
``version_info`` feeds the session journal. Swap the RMS gate for your own
detector and keep the same surface.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import AsyncIterator
from dataclasses import dataclass

from easycat import AudioChunk, Event, VADStartSpeaking, VADStopSpeaking


@dataclass
class EnergyVADConfig:
    """Configuration for :class:`EnergyVAD`."""

    threshold: float = 500.0


class EnergyVAD:
    """Flags speech whenever PCM16 RMS energy crosses a threshold."""

    def __init__(self, config: EnergyVADConfig | None = None) -> None:
        self._config = config or EnergyVADConfig()
        self._speaking = False

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        samples = array("h", chunk.data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
        loud = rms >= self._config.threshold
        if loud and not self._speaking:
            self._speaking = True
            yield VADStartSpeaking()
        elif not loud and self._speaking:
            self._speaking = False
            yield VADStopSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        self._config = EnergyVADConfig(threshold=1000.0 * (1.0 - sensitivity))

    def version_info(self) -> dict[str, str]:
        return {"provider": "energy", "model": "rms", "api_version": "v1", "sdk_version": "none"}
