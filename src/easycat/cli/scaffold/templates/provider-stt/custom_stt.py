"""A deterministic, offline EasyCat STT provider authoring skeleton."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from easycat import AudioChunk, register_stt_provider
from easycat.events import STTEvent, STTEventType


@dataclass(slots=True)
class ScriptedSTTConfig:
    """Config created by ``stt="scripted/model"`` shortcut resolution."""

    model: str = "scripted-v1"
    transcript: str = "This is a scaffolded transcript."
    # LIVE TODO: add ``api_key: str | None = None`` for a credentialed backend.


class ScriptedSTT:
    """Emit one deterministic final transcript for each committed segment."""

    def __init__(self, config: ScriptedSTTConfig | None = None) -> None:
        self.config = config or ScriptedSTTConfig()
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._running = False
        self._has_audio = False

    async def start_stream(self) -> None:
        self._queue = asyncio.Queue()
        self._running = True
        self._has_audio = False

    async def send_audio(self, chunk: AudioChunk) -> None:
        if not self._running:
            raise RuntimeError("stream is not started")
        self._has_audio = self._has_audio or bool(chunk.data)
        # LIVE TODO: stream ``chunk`` to your SDK without blocking the event loop.

    async def commit_segment(self) -> bool:
        if not self._running or not self._has_audio:
            return False
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self.config.transcript))
        self._has_audio = False
        return True

    async def end_stream(self) -> None:
        if self._running:
            self._running = False
            await self._queue.put(None)

    def events(self) -> AsyncIterator[STTEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[STTEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "scripted",
            "model": self.config.model,
            "api_version": "offline-v1",
            "sdk_version": "none",
        }


def register() -> None:
    """Make ``stt="scripted"`` available to configs, manifests, and plans."""
    register_stt_provider(
        "scripted",
        ScriptedSTT,
        ScriptedSTTConfig,
        capabilities=frozenset({"offline"}),
        # LIVE TODO: add env_var, probe_module, and api_domains for your SDK.
    )
