"""Tests for the shared :class:`ProviderErrorEmitter` mixin.

The STT WebSocket base, the TTS WebSocket base, and the OpenAI HTTP TTS
provider all delegate their fire-and-forget ``Error``-emit path to this one
mixin. The per-base/per-provider tests assert each end-to-end; these tests pin
the shared mechanism directly so a regression in the single copy is caught even
if a downstream test drifts.
"""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass
from typing import Any

import pytest

from easycat._concurrency import RuntimeSupervisor
from easycat._provider_helpers import (
    ProviderErrorEmitter,
    get_package_version,
    word_timestamps_from_words,
)
from easycat.events import Error, ErrorStage
from easycat.runtime.scope import RuntimeScope, RuntimeScopeState


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


def test_emit_with_bus_outside_running_loop_does_not_leak_a_coroutine(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """The documented no-loop path is a quiet no-op, not an unawaited emit."""
    probe = _ConfigProbe(_Config(event_bus=_RecordingBus()))

    probe._emit_provider_error(RuntimeError("boom"))
    gc.collect()

    assert probe._emit_tasks == set()
    assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]


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


@pytest.mark.asyncio
async def test_standalone_drain_closes_and_releases_the_emitter_root():
    bus = _RecordingBus()
    probe = _ConfigProbe(_Config(event_bus=bus))
    probe._emit_provider_error(RuntimeError("boom"))
    scope = probe._emit_scope
    assert scope is not None
    assert scope.survivor_registry is None

    await probe._drain_emit_tasks()

    assert scope.state is RuntimeScopeState.CLOSED
    assert probe._emit_scope is None


@pytest.mark.asyncio
async def test_attached_emitter_uses_parent_tree_and_stays_reusable_after_drain():
    root = RuntimeScope.create_root(
        name="session",
        root_id="session:test",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    bus = _RecordingBus()
    probe = _ConfigProbe(_Config(event_bus=bus))
    probe._attach_provider_event_scope(root, name="tts-provider-events")

    probe._emit_provider_error(RuntimeError("first"))
    assert len(root.tasks("provider_error_emit")) == 1
    await probe._drain_emit_tasks()

    child = probe._emit_scope
    assert child is not None
    assert child.parent is root
    assert child.state is RuntimeScopeState.OPEN

    probe._emit_provider_error(RuntimeError("second"))
    await probe._drain_emit_tasks()
    assert len(bus.events) == 2
    await root.close()


@pytest.mark.asyncio
async def test_emit_is_a_quiet_noop_after_attached_scope_closes(
    recwarn: pytest.WarningsRecorder,
) -> None:
    root = RuntimeScope.create_root(
        name="session",
        root_id="session:test",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    bus = _RecordingBus()
    probe = _ConfigProbe(_Config(event_bus=bus))
    probe._attach_provider_event_scope(root, name="tts-provider-events")
    await root.close()

    probe._emit_provider_error(RuntimeError("late"))
    gc.collect()

    assert bus.events == []
    assert probe._emit_tasks == set()
    assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]


@pytest.mark.asyncio
async def test_drain_preserves_log_and_drop_policy_for_emitter_failure():
    class _FailingBus:
        async def emit(self, _event: Any) -> None:
            raise RuntimeError("subscriber failed")

    probe = _ConfigProbe(_Config(event_bus=_FailingBus()))
    probe._emit_provider_error(RuntimeError("provider failed"))

    await probe._drain_emit_tasks()

    assert probe._emit_tasks == set()


@pytest.mark.asyncio
async def test_drain_does_not_await_the_current_error_handler_task():
    probe = _ConfigProbe(_Config())
    current = asyncio.current_task()
    assert current is not None
    sibling = asyncio.create_task(asyncio.Event().wait())
    scope = probe._ensure_emit_scope()
    scope.add_task("provider_error_emit", current)
    scope.add_task("provider_error_emit", sibling)
    try:
        async with asyncio.timeout(0.1):
            await probe._drain_emit_tasks()
    finally:
        scope.discard(current)
        sibling.cancel()
        await asyncio.gather(sibling, return_exceptions=True)
        scope.discard(sibling)
        await probe._drain_emit_tasks()


def test_get_package_version_returns_unknown_for_missing_package():
    assert get_package_version("easycat-definitely-not-installed") == "unknown"


def test_word_timestamps_accept_word_or_text_keys():
    timestamps = word_timestamps_from_words(
        [
            {"word": "hello", "start": 0, "end": 0.3},
            {"text": "world", "start": "0.4", "end": "0.7"},
        ]
    )

    assert timestamps is not None
    assert [timestamp.word for timestamp in timestamps] == ["hello", "world"]
    assert timestamps[0].start == 0.0
    assert timestamps[1].end == 0.7


def test_word_timestamps_skip_missing_values():
    assert (
        word_timestamps_from_words(
            [
                {"word": "missing-start", "end": 0.2},
                {"word": "missing-end", "start": 0.3},
                {"start": 0.4, "end": 0.5},
            ]
        )
        is None
    )


def test_word_timestamps_skip_non_numeric_timestamps():
    timestamps = word_timestamps_from_words(
        [
            {"word": "bad-start", "start": "nope", "end": 0.3},
            {"word": "bad-end", "start": 0.4, "end": object()},
            {"word": "valid", "start": "0.5", "end": 0.8},
        ]
    )

    assert timestamps is not None
    assert len(timestamps) == 1
    assert timestamps[0].word == "valid"
    assert timestamps[0].start == 0.5
    assert timestamps[0].end == 0.8
