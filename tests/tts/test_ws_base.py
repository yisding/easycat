"""Tests for the shared WebSocket TTS base (``_WSTTSBase``).

Cartesia/Deepgram/ElevenLabs share their error-emission, fire-and-forget task
tracking, and WebSocket teardown via this base. The per-provider tests already
assert each provider's ``err.provider`` string end-to-end; these tests pin the
shared mechanism directly so a regression in the base is caught even if a
provider-level test drifts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from easycat.events import Error, ErrorStage
from easycat.tts._ws_base import _WSTTSBase
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig


@dataclass
class _Config:
    event_bus: object | None = None


class _Probe(_WSTTSBase):
    """Minimal concrete subclass for exercising base behavior."""

    _provider_error_name = "probe"
    _provider_log_label = "Probe"

    def __init__(self, config: _Config) -> None:
        super().__init__()
        self._config = config


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        # Yield control so an un-referenced task could be GC'd before this
        # resumes — the strong reference must keep it alive.
        await asyncio.sleep(0)
        self.events.append(event)


@pytest.mark.asyncio
async def test_emit_provider_error_tracks_task_until_complete():
    """The fire-and-forget emit task is strongly referenced until it finishes."""
    bus = _RecordingBus()
    probe = _Probe(_Config(event_bus=bus))

    probe._emit_provider_error(RuntimeError("boom"), code=42)

    # Task is tracked while pending.
    assert len(probe._emit_tasks) == 1
    # Let it run to completion; the event must have been emitted.
    await asyncio.gather(*list(probe._emit_tasks))
    assert len(bus.events) == 1
    emitted = bus.events[0]
    assert isinstance(emitted, Error)
    # The provider string comes from the class attribute, not a literal.
    assert emitted.provider == "probe"
    assert emitted.stage == ErrorStage.TTS
    notes = getattr(emitted.exception, "__notes__", [])
    assert any("code=42" in n for n in notes)
    # Done-callback discards the finished task.
    assert probe._emit_tasks == set()


@pytest.mark.asyncio
async def test_emit_provider_error_noop_without_bus():
    probe = _Probe(_Config(event_bus=None))
    probe._emit_provider_error(RuntimeError("boom"))
    assert probe._emit_tasks == set()


@pytest.mark.asyncio
async def test_emit_provider_error_skips_none_context():
    """``None`` context values are dropped instead of attached as notes."""
    bus = _RecordingBus()
    probe = _Probe(_Config(event_bus=bus))

    probe._emit_provider_error(RuntimeError("boom"), code=None, status_code=400)

    await asyncio.gather(*list(probe._emit_tasks))
    notes = getattr(bus.events[0].exception, "__notes__", [])
    assert any("status_code=400" in n for n in notes)
    assert not any("code=" in n and "status_code" not in n for n in notes)


@pytest.mark.asyncio
async def test_close_ws_retains_failed_socket_for_idempotent_retry():
    """``_close_ws`` forgets the exact socket only after close succeeds."""

    class _FakeWS:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1
            if self.closed == 1:
                raise RuntimeError("socket close failed")

    probe = _Probe(_Config())
    fake = _FakeWS()
    probe._ws = fake  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="socket close failed"):
        await probe._close_ws()
    assert probe._ws is fake
    assert fake.closed == 1

    await probe._close_ws()
    assert probe._ws is None
    assert fake.closed == 2
    # Once the successful close clears ownership, further calls are no-ops.
    await probe._close_ws()
    assert fake.closed == 2


@pytest.mark.asyncio
async def test_replace_oneshot_ws_waits_for_retained_close_before_factory():
    class _FailOnceWS:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("socket close failed")

    probe = _Probe(_Config())
    retained = _FailOnceWS()
    replacement = _FailOnceWS()
    factory = MagicMock(return_value=replacement)
    probe._ws = retained  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="socket close failed"):
        await probe._replace_oneshot_ws(factory)

    assert probe._ws is retained
    factory.assert_not_called()

    assert await probe._replace_oneshot_ws(factory) is replacement
    assert retained.close_calls == 2
    factory.assert_called_once_with()
    assert probe._ws is replacement


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(
            CartesiaTTS(CartesiaTTSConfig(persistent_ws=False)),
            id="cartesia",
        ),
        pytest.param(
            DeepgramTTS(DeepgramTTSConfig(persistent_ws=False)),
            id="deepgram",
        ),
        pytest.param(
            ElevenLabsTTS(ElevenLabsTTSConfig(persistent_ws=False)),
            id="elevenlabs",
        ),
    ],
)
@pytest.mark.asyncio
async def test_one_shot_provider_stop_retries_exact_failed_socket(
    provider: _WSTTSBase,
) -> None:
    class _FailOnceWS:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("socket close failed")

    ws = _FailOnceWS()
    provider._ws = ws  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="socket close failed"):
        await provider.stop()

    assert provider._ws is ws
    await provider.stop()
    assert provider._ws is None
    assert ws.close_calls == 2


@pytest.mark.asyncio
async def test_drain_emit_tasks_awaits_pending():
    """Teardown awaits in-flight emit tasks so none dangle past close."""
    bus = _RecordingBus()
    probe = _Probe(_Config(event_bus=bus))

    probe._emit_provider_error(RuntimeError("boom"), code=7)
    assert len(probe._emit_tasks) == 1  # scheduled, not yet awaited

    await probe._drain_emit_tasks()

    assert probe._emit_tasks == set()
    assert len(bus.events) == 1
