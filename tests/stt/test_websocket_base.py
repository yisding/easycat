"""Tests for the shared WebSocket STT base (``WebSocketSTTBase``)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from easycat.stt.websocket_base import WebSocketSTTBase, _noop_reconnect


class _Probe(WebSocketSTTBase):
    """Minimal concrete subclass for exercising base behavior."""

    def __init__(self) -> None:
        super().__init__(provider_name="probe_stt", provider_error_name="probe")

    def _handle_json_message(self, msg: dict[str, Any]) -> None:  # pragma: no cover
        pass


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        # Yield control so an un-referenced task could be GC'd before this
        # resumes — the strong reference must keep it alive.
        await asyncio.sleep(0)
        self.events.append(event)


@pytest.mark.asyncio
async def test_connect_websocket_defaults_to_present_on_reconnect(monkeypatch):
    """Query-param providers get a present on_reconnect so recv_iter reconnects."""
    captured: dict[str, Any] = {}

    class _FakeWS:
        def __init__(self, *args, **kwargs):
            captured["on_reconnect"] = kwargs.get("on_reconnect")

        async def connect(self) -> None:
            pass

    monkeypatch.setattr("easycat.stt.websocket_base.ReconnectingWebSocket", _FakeWS)

    probe = _Probe()

    async def _noop_loop() -> None:
        pass

    monkeypatch.setattr(probe, "_receive_loop", _noop_loop)

    await probe._connect_websocket(url="wss://x", headers={})

    assert captured["on_reconnect"] is _noop_reconnect


@pytest.mark.asyncio
async def test_emit_provider_error_tracks_task_until_complete():
    """The fire-and-forget emit task is strongly referenced until it finishes."""
    probe = _Probe()
    bus = _RecordingBus()
    probe._provider_event_bus = bus

    probe._emit_provider_error(RuntimeError("boom"), code=42)

    # Task is tracked while pending.
    assert len(probe._emit_tasks) == 1
    # Let it run to completion; the event must have been emitted.
    await asyncio.gather(*list(probe._emit_tasks))
    assert len(bus.events) == 1
    # Done-callback discards the finished task.
    assert probe._emit_tasks == set()


@pytest.mark.asyncio
async def test_emit_provider_error_noop_without_bus():
    probe = _Probe()
    probe._provider_event_bus = None
    probe._emit_provider_error(RuntimeError("boom"))
    assert probe._emit_tasks == set()
