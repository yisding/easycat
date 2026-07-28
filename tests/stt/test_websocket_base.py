"""Tests for the shared WebSocket STT base (``WebSocketSTTBase``)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import websockets

from easycat.reconnecting_ws import ReconnectConfig
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


class _FakeAbnormalWS:
    """Fake wrapper whose recv_iter ends after a terminal mid-stream death."""

    def __init__(self, died_abnormally: bool, *, attempts: int | None = None) -> None:
        self.died_abnormally = died_abnormally
        self.reconnect_attempts_exhausted = attempts

    async def recv_iter(self):
        # Terminal death mid-utterance: yield nothing then end cleanly (the
        # reconnect budget was exhausted inside ReconnectingWebSocket).
        return
        yield  # pragma: no cover - makes this an async generator


class _DroppingConnection:
    close_code = None

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        close_frame = websockets.frames.Close(1006, "abnormal")
        raise websockets.exceptions.ConnectionClosed(close_frame, None)
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


class _CleanConnection:
    close_code = None

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        self.started.set()
        await self.release.wait()
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_receive_loop_emits_error_on_abnormal_death():
    """A terminal WS death surfaces an Error before the sentinel, not silence."""
    from easycat.events import Error, ErrorStage

    probe = _Probe()
    bus = _RecordingBus()
    probe._provider_event_bus = bus
    probe._ws = _FakeAbnormalWS(died_abnormally=True, attempts=3)  # type: ignore[assignment]

    await probe._receive_loop()
    await asyncio.gather(*list(probe._emit_tasks))

    errors = [e for e in bus.events if isinstance(e, Error)]
    assert errors, "expected an Error event on abnormal WS death"
    assert errors[0].stage is ErrorStage.STT
    assert errors[0].code == "EASYCAT_E305"
    assert "after 3 attempt" in str(errors[0].exception)
    # The None sentinel is still queued last so events() terminates cleanly.
    assert probe._event_queue.get_nowait() is None


@pytest.mark.asyncio
async def test_connect_websocket_emits_e304_when_real_drop_reconnects():
    """The production wrapper boundary reports a transient drop before recovery."""
    from easycat.events import Error, ErrorStage, ReconnectAttempt, ReconnectSuccess

    probe = _Probe()
    bus = _RecordingBus()
    resumed = _CleanConnection()
    connections = iter([_DroppingConnection(), resumed])
    connect_calls = 0

    async def connect_fn(*args: Any, **kwargs: Any) -> Any:
        nonlocal connect_calls
        connect_calls += 1
        return next(connections)

    ws = await probe._connect_websocket(
        url="wss://probe.invalid",
        headers={},
        event_bus=bus,
        connect_fn=connect_fn,
    )
    assert probe._receive_task is not None
    await resumed.started.wait()
    await ws.close()
    resumed.release.set()
    await probe._receive_task
    await probe._drain_emit_tasks()

    assert connect_calls == 2
    errors = [e for e in bus.events if isinstance(e, Error)]
    assert [error.code for error in errors] == ["EASYCAT_E304"]
    assert errors[0].stage is ErrorStage.STT
    assert "1006" in str(errors[0].exception)
    assert "abnormal" in str(errors[0].exception)
    assert [type(event) for event in bus.events] == [
        ReconnectAttempt,
        ReconnectSuccess,
        Error,
        ReconnectAttempt,
        ReconnectSuccess,
    ]
    assert ws.died_abnormally is False
    assert probe._event_queue.get_nowait() is None
    await ws.close()


@pytest.mark.asyncio
async def test_connect_websocket_emits_e304_then_e305_when_retries_exhausted(monkeypatch):
    """A real drop is recoverable E304 until reconnect attempts are exhausted."""
    from easycat.events import Error

    def reconnect_config(**kwargs: Any) -> ReconnectConfig:
        return ReconnectConfig(
            max_retries=1,
            base_delay=0.001,
            max_delay=0.001,
            jitter_factor=0.0,
            extra_headers=kwargs["extra_headers"],
        )

    monkeypatch.setattr("easycat.stt.websocket_base.ReconnectConfig", reconnect_config)

    probe = _Probe()
    bus = _RecordingBus()
    connect_calls = 0

    async def connect_fn(*args: Any, **kwargs: Any) -> Any:
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            return _DroppingConnection()
        raise ConnectionError("provider unavailable")

    ws = await probe._connect_websocket(
        url="wss://probe.invalid",
        headers={},
        event_bus=bus,
        connect_fn=connect_fn,
    )
    assert probe._receive_task is not None
    await probe._receive_task
    await probe._drain_emit_tasks()

    errors = [event for event in bus.events if isinstance(event, Error)]
    assert [error.code for error in errors] == ["EASYCAT_E304", "EASYCAT_E305"]
    assert "after 2 attempt" in str(errors[1].exception)
    assert ws.died_abnormally is True
    assert ws.reconnect_attempts_exhausted == 2
    assert ws.reconnect_exhaustion_reason == "failed reconnect attempts"
    assert connect_calls == 3
    assert probe._event_queue.get_nowait() is None
    await ws.close()


@pytest.mark.asyncio
async def test_receive_loop_no_error_on_clean_end():
    """A graceful end-of-stream must not emit a spurious Error."""
    probe = _Probe()
    bus = _RecordingBus()
    probe._provider_event_bus = bus
    probe._ws = _FakeAbnormalWS(died_abnormally=False)  # type: ignore[assignment]

    await probe._receive_loop()
    await asyncio.gather(*list(probe._emit_tasks))

    assert bus.events == []
    assert probe._event_queue.get_nowait() is None


@pytest.mark.asyncio
async def test_connect_websocket_defaults_to_present_on_reconnect(monkeypatch):
    """Query-param providers get a present on_reconnect so recv_iter reconnects."""
    captured: dict[str, Any] = {}

    class _FakeWS:
        def __init__(self, *args, **kwargs):
            captured["on_reconnect"] = kwargs.get("on_reconnect")
            captured["on_disconnect"] = kwargs.get("on_disconnect")

        async def connect(self) -> None:
            pass

    monkeypatch.setattr("easycat.stt.websocket_base.ReconnectingWebSocket", _FakeWS)

    probe = _Probe()

    async def _noop_loop() -> None:
        pass

    monkeypatch.setattr(probe, "_receive_loop", _noop_loop)

    await probe._connect_websocket(url="wss://x", headers={})

    assert captured["on_reconnect"] is _noop_reconnect
    assert captured["on_disconnect"] == probe._on_websocket_disconnect


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


@pytest.mark.asyncio
async def test_close_active_websocket_drains_pending_emit_tasks():
    """Teardown awaits in-flight emit tasks so none dangle past close."""

    class _FakeWS:
        async def close(self) -> None:
            return None

    probe = _Probe()
    bus = _RecordingBus()
    probe._provider_event_bus = bus
    probe._ws = _FakeWS()  # type: ignore[assignment]
    probe._receive_task = None

    probe._emit_provider_error(RuntimeError("boom"), code=7)
    assert len(probe._emit_tasks) == 1  # scheduled, not yet awaited

    await probe._close_active_websocket()

    # The emit task was awaited during teardown — nothing left pending.
    assert probe._emit_tasks == set()
    assert len(bus.events) == 1
