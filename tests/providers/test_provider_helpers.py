"""Tests for the shared :class:`ProviderErrorEmitter` mixin.

The STT WebSocket base, the TTS WebSocket base, and the OpenAI HTTP TTS
provider all delegate their fire-and-forget ``Error``-emit path to this one
mixin. The per-base/per-provider tests assert each end-to-end; these tests pin
the shared mechanism directly so a regression in the single copy is caught even
if a downstream test drifts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from easycat._provider_helpers import ProviderErrorEmitter
from easycat.events import Error, ErrorStage


@dataclass
class _Config:
    event_bus: object | None = None


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        # Yield control so an un-referenced task could be GC'd before this
        # resumes — the strong reference must keep it alive.
        await asyncio.sleep(0)
        self.events.append(event)


class _ConfigProbe(ProviderErrorEmitter):
    """Resolves the bus from ``self._config`` (the TTS pattern)."""

    _error_stage = ErrorStage.TTS
    _provider_error_name = "probe-tts"

    def __init__(self, config: _Config) -> None:
        self._config = config
        self._init_emit_tasks()


class _PerConnectionProbe(ProviderErrorEmitter):
    """Resolves the bus from a per-connection reference (the STT pattern)."""

    _error_stage = ErrorStage.STT
    _provider_error_name = "probe-stt"

    def __init__(self) -> None:
        self._provider_event_bus: object | None = None
        self._init_emit_tasks()

    def _resolve_event_bus(self) -> Any | None:
        return self._provider_event_bus


@pytest.mark.asyncio
async def test_emit_uses_config_bus_and_tts_stage():
    bus = _RecordingBus()
    probe = _ConfigProbe(_Config(event_bus=bus))

    probe._emit_provider_error(RuntimeError("boom"), code=42)

    assert len(probe._emit_tasks) == 1  # strongly referenced while pending
    await asyncio.gather(*list(probe._emit_tasks))

    assert len(bus.events) == 1
    emitted = bus.events[0]
    assert isinstance(emitted, Error)
    assert emitted.stage is ErrorStage.TTS
    assert emitted.provider == "probe-tts"
    notes = getattr(emitted.exception, "__notes__", [])
    assert any("code=42" in n for n in notes)
    assert probe._emit_tasks == set()  # done-callback discards the task


@pytest.mark.asyncio
async def test_resolve_event_bus_override_and_stt_stage():
    bus = _RecordingBus()
    probe = _PerConnectionProbe()
    probe._provider_event_bus = bus

    probe._emit_provider_error(RuntimeError("boom"))
    await asyncio.gather(*list(probe._emit_tasks))

    assert len(bus.events) == 1
    assert bus.events[0].stage is ErrorStage.STT
    assert bus.events[0].provider == "probe-stt"


@pytest.mark.asyncio
async def test_emit_is_noop_without_bus():
    probe = _ConfigProbe(_Config(event_bus=None))
    probe._emit_provider_error(RuntimeError("boom"))
    assert probe._emit_tasks == set()

    per_conn = _PerConnectionProbe()  # _provider_event_bus left None
    per_conn._emit_provider_error(RuntimeError("boom"))
    assert per_conn._emit_tasks == set()


@pytest.mark.asyncio
async def test_none_context_values_are_skipped():
    bus = _RecordingBus()
    probe = _ConfigProbe(_Config(event_bus=bus))

    probe._emit_provider_error(RuntimeError("boom"), code=None, status_code=400)
    await asyncio.gather(*list(probe._emit_tasks))

    notes = getattr(bus.events[0].exception, "__notes__", [])
    assert any("status_code=400" in n for n in notes)
    assert not any(n.startswith("code=") for n in notes)


@pytest.mark.asyncio
async def test_drain_awaits_pending_tasks():
    bus = _RecordingBus()
    probe = _ConfigProbe(_Config(event_bus=bus))

    probe._emit_provider_error(RuntimeError("boom"), code=7)
    assert len(probe._emit_tasks) == 1  # scheduled, not yet awaited

    await probe._drain_emit_tasks()

    assert probe._emit_tasks == set()
    assert len(bus.events) == 1
