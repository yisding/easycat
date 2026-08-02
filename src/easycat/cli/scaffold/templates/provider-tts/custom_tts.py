"""A deterministic, offline EasyCat TTS provider authoring skeleton."""

from __future__ import annotations

import math
from array import array
from collections.abc import AsyncIterator
from dataclasses import dataclass

from easycat import PCM16_MONO_24K, AudioChunk, register_tts_provider
from easycat.events import TTSEvent, TTSEventType
from easycat.tts.input import TTSInput


@dataclass(slots=True)
class ToneTTSConfig:
    """Config created by ``tts="tone/model"`` shortcut resolution."""

    model: str = "tone-v1"
    frequency_hz: float = 440.0
    duration_ms: int = 80
    # LIVE TODO: add ``api_key: str | None = None`` for a credentialed backend.


class ToneTTS:
    """Synthesize one short PCM16 tone as a deterministic placeholder."""

    def __init__(self, config: ToneTTSConfig | None = None) -> None:
        self.config = config or ToneTTSConfig()
        self._cancelled = False

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        self._cancelled = False
        text = payload if isinstance(payload, str) else payload.text
        if not text or self._cancelled:
            return
        sample_count = PCM16_MONO_24K.sample_rate * self.config.duration_ms // 1000
        samples = array(
            "h",
            (
                int(4000 * math.sin(2 * math.pi * self.config.frequency_hz * index / 24000))
                for index in range(sample_count)
            ),
        )
        # LIVE TODO: stream SDK audio chunks here and check cancellation promptly.
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=samples.tobytes(), format=PCM16_MONO_24K),
        )

    async def stop(self) -> None:
        self._cancelled = True

    async def cancel(self) -> None:
        self._cancelled = True

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "tone",
            "model": self.config.model,
            "api_version": "offline-v1",
            "sdk_version": "none",
        }


def register() -> None:
    """Make ``tts="tone"`` available to configs, manifests, and plans."""
    register_tts_provider(
        "tone",
        ToneTTS,
        ToneTTSConfig,
        capabilities=frozenset({"offline"}),
        # LIVE TODO: add env_var, probe_module, and api_domains for your SDK.
    )
