"""Tests for the shared WebSocket STT base (``WebSocketSTTBase``)."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest
import websockets

from easycat._concurrency import RuntimeSupervisor
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import Error, ErrorStage, EventBus
from easycat.reconnecting_ws import ReconnectConfig
from easycat.runtime.scope import RuntimeScope
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
async def test_failed_partial_connect_closes_published_socket_before_retry(monkeypatch):
    class _FakeWrapper:
        died_abnormally = False
        reconnect_attempts_exhausted = None
        reconnect_exhaustion_reason = None

        def __init__(self, *, block_connect: bool) -> None:
            self.block_connect = block_connect
            self.connect_entered = asyncio.Event()
            self.closed = asyncio.Event()
            self.close_calls = 0

        async def connect(self) -> None:
            self.connect_entered.set()
            if self.block_connect:
                await asyncio.Event().wait()

        async def recv_iter(self):
            await self.closed.wait()
            return
            yield  # pragma: no cover

        async def close(self) -> None:
            self.close_calls += 1
            self.closed.set()

    first = _FakeWrapper(block_connect=True)
    second = _FakeWrapper(block_connect=False)
    wrappers = iter([first, second])
    monkeypatch.setattr(
        "easycat.stt.websocket_base.ReconnectingWebSocket",
        lambda *args, **kwargs: next(wrappers),
    )

    class _ConnectingProbe(_Probe):
        async def _on_start(self) -> None:
            await self._connect_websocket(url="wss://probe.invalid", headers={})

        async def _on_end(self) -> None:
            await self._close_active_websocket(close_before_drain=True)

    probe = _ConnectingProbe()
    start = asyncio.create_task(probe.start_stream())
    await first.connect_entered.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert first.close_calls >= 1
    assert probe._ws is None
    assert probe._receive_task is None

    await probe.start_stream()
    assert probe._ws is second
    await probe.end_stream()
    assert second.close_calls >= 1


@pytest.mark.asyncio
async def test_failed_start_retains_socket_when_close_fails_then_retries(monkeypatch):
    class _FakeWrapper:
        died_abnormally = False
        reconnect_attempts_exhausted = None
        reconnect_exhaustion_reason = None

        def __init__(self, *, fail_first_close: bool = False) -> None:
            self.fail_first_close = fail_first_close
            self.closed = asyncio.Event()
            self.close_calls = 0

        async def connect(self) -> None:
            return None

        async def recv_iter(self):
            await self.closed.wait()
            return
            yield  # pragma: no cover

        async def close(self) -> None:
            self.close_calls += 1
            if self.fail_first_close and self.close_calls == 1:
                raise RuntimeError("socket close failed")
            self.closed.set()

    first = _FakeWrapper(fail_first_close=True)
    second = _FakeWrapper()
    wrappers = iter([first, second])
    monkeypatch.setattr(
        "easycat.stt.websocket_base.ReconnectingWebSocket",
        lambda *args, **kwargs: next(wrappers),
    )

    class _FailOnceAfterConnect(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.start_calls = 0

        async def _on_start(self) -> None:
            self.start_calls += 1
            await self._connect_websocket(url="wss://probe.invalid", headers={})
            if self.start_calls == 1:
                raise RuntimeError("provider startup failed")

        async def _on_end(self) -> None:
            await self._close_active_websocket(close_before_drain=True)

    probe = _FailOnceAfterConnect()

    with pytest.raises(RuntimeError, match="provider startup failed") as exc_info:
        await probe.start_stream()

    assert str(exc_info.value.__cause__) == "socket close failed"
    assert probe._ws is first
    assert probe._receive_task is not None
    assert probe._failed_start_cleanup_pending is True

    await probe.start_stream()

    assert first.close_calls >= 2
    assert probe._ws is second
    assert probe._failed_start_cleanup_pending is False
    await probe.end_stream()


@pytest.mark.asyncio
async def test_failed_end_retains_exact_socket_and_retries_without_refinalizing(
    monkeypatch,
):
    class _FakeWrapper:
        died_abnormally = False
        reconnect_attempts_exhausted = None
        reconnect_exhaustion_reason = None

        def __init__(self, *, fail_first_close: bool = False) -> None:
            self.fail_first_close = fail_first_close
            self.closed = asyncio.Event()
            self.close_calls = 0

        async def connect(self) -> None:
            return None

        async def recv_iter(self):
            await self.closed.wait()
            return
            yield  # pragma: no cover

        async def close(self) -> None:
            self.close_calls += 1
            if self.fail_first_close and self.close_calls == 1:
                raise RuntimeError("socket close failed")
            self.closed.set()

    first = _FakeWrapper(fail_first_close=True)
    second = _FakeWrapper()
    wrappers = iter([first, second])
    monkeypatch.setattr(
        "easycat.stt.websocket_base.ReconnectingWebSocket",
        lambda *args, **kwargs: next(wrappers),
    )

    class _EndCleanupProbe(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_calls = 0

        async def _on_start(self) -> None:
            await self._connect_websocket(url="wss://probe.invalid", headers={})

        async def _on_end(self) -> None:
            self.finalize_calls += 1
            await self._close_active_websocket(close_before_drain=True)

    probe = _EndCleanupProbe()
    await probe.start_stream()

    with pytest.raises(RuntimeError, match="socket close failed"):
        await probe.end_stream()

    assert probe._ws is first
    assert probe._failed_end_cleanup_pending is True
    assert probe.finalize_calls == 1

    await probe.start_stream()

    # Retry closes once up front to wake the receiver and once again in the
    # drain helper's finally block.
    assert first.close_calls == 3
    assert probe._ws is second
    assert probe._failed_end_cleanup_pending is False
    assert probe.finalize_calls == 1
    await probe.end_stream()


@pytest.mark.asyncio
async def test_failed_start_error_observer_can_close_after_resource_cleanup() -> None:
    startup_error = RuntimeError("provider startup failed")
    observer_completed = asyncio.Event()
    emitted: list[Error] = []

    class _FakeWrapper:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    socket = _FakeWrapper()
    bus = EventBus()

    async def close_provider(event: Error) -> None:
        emitted.append(event)
        await probe.close()
        observer_completed.set()

    bus.subscribe(Error, close_provider)

    class _FailedStartProbe(_Probe):
        async def _on_start(self) -> None:
            self._provider_event_bus = bus
            self._ws = socket  # type: ignore[assignment]
            self._emit_provider_error(startup_error)
            raise startup_error

    probe = _FailedStartProbe()

    with pytest.raises(RuntimeError, match="provider startup failed") as exc_info:
        await asyncio.wait_for(probe.start_stream(), timeout=1)

    assert exc_info.value is startup_error
    assert observer_completed.is_set()
    assert len(emitted) == 1
    assert emitted[0].exception is startup_error
    assert socket.close_calls == 2
    assert probe._ws is None
    assert probe._failed_start_cleanup_pending is False
    assert probe._emit_tasks == set()
    assert probe._lifecycle_lock.locked() is False


@pytest.mark.asyncio
async def test_failed_end_error_observer_can_close_and_retry_exact_cleanup() -> None:
    end_error = RuntimeError("socket close failed")
    observer_completed = asyncio.Event()

    class _FakeWrapper:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise end_error

    socket = _FakeWrapper()
    bus = EventBus()

    async def close_provider(_event: Error) -> None:
        await probe.close()
        observer_completed.set()

    bus.subscribe(Error, close_provider)

    class _FailedEndProbe(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_calls = 0

        async def _on_start(self) -> None:
            self._provider_event_bus = bus
            self._ws = socket  # type: ignore[assignment]

        async def _on_end(self) -> None:
            self.finalize_calls += 1
            self._emit_provider_error(end_error)
            await self._close_active_websocket(close_before_drain=True)

    probe = _FailedEndProbe()
    await probe.start_stream()

    with pytest.raises(RuntimeError, match="socket close failed") as exc_info:
        await asyncio.wait_for(probe.end_stream(), timeout=1)

    assert exc_info.value is end_error
    assert observer_completed.is_set()
    assert socket.close_calls == 3
    assert probe.finalize_calls == 1
    assert probe._ws is None
    assert probe._failed_end_cleanup_pending is False
    assert probe._failed_end_cleanup_error is None
    assert probe._emit_tasks == set()
    assert probe._lifecycle_lock.locked() is False


@pytest.mark.asyncio
async def test_streaming_audio_downmixes_stereo_pcm16_once() -> None:
    class _AudioProbe(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.audio: list[AudioChunk] = []

        async def _on_audio(self, chunk: AudioChunk) -> None:
            self.audio.append(chunk)

    probe = _AudioProbe()
    await probe.start_stream()
    stereo = AudioChunk(
        data=struct.pack("<hhhh", 1200, -1200, -900, 900),
        format=AudioFormat(sample_rate=16000, channels=2, sample_width=2),
        timestamp=1.25,
    )

    await probe.send_audio(stereo)

    assert len(probe.audio) == 1
    normalized = probe.audio[0]
    assert normalized.data == struct.pack("<hh", 0, 0)
    assert normalized.format == AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    assert normalized.timestamp == 1.25
    await probe.end_stream()


@pytest.mark.asyncio
async def test_streaming_audio_carries_split_multichannel_frames_before_downmix() -> None:
    class _AudioProbe(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.audio: list[AudioChunk] = []

        async def _on_audio(self, chunk: AudioChunk) -> None:
            self.audio.append(chunk)

    probe = _AudioProbe()
    await probe.start_stream()
    stereo_format = AudioFormat(sample_rate=16000, channels=2, sample_width=2)

    await probe.send_audio(
        AudioChunk(
            data=struct.pack("<3h", 100, 300, 1000),
            format=stereo_format,
        )
    )
    await probe.send_audio(
        AudioChunk(
            data=struct.pack("<3h", 2000, 3000, 4000),
            format=stereo_format,
        )
    )

    assert [chunk.data for chunk in probe.audio] == [
        struct.pack("<h", 200),
        struct.pack("<2h", 1500, 3500),
    ]
    assert probe._source_frame_carry == b""
    await probe.end_stream()


@pytest.mark.asyncio
async def test_streaming_audio_discards_frame_carry_at_stream_boundary() -> None:
    class _AudioProbe(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.audio: list[AudioChunk] = []

        async def _on_audio(self, chunk: AudioChunk) -> None:
            self.audio.append(chunk)

    probe = _AudioProbe()
    stereo_format = AudioFormat(sample_rate=16000, channels=2, sample_width=2)
    await probe.start_stream()
    await probe.send_audio(AudioChunk(data=struct.pack("<h", 100), format=stereo_format))
    assert probe.audio == []
    await probe.end_stream()

    await probe.start_stream()
    await probe.send_audio(AudioChunk(data=struct.pack("<2h", 300, 500), format=stereo_format))

    assert [chunk.data for chunk in probe.audio] == [struct.pack("<h", 400)]
    await probe.end_stream()


@pytest.mark.asyncio
async def test_streaming_audio_rejects_non_pcm16_sample_width() -> None:
    probe = _Probe()
    await probe.start_stream()
    chunk = AudioChunk(
        data=b"\x80",
        format=AudioFormat(sample_rate=16000, channels=1, sample_width=1),
    )

    with pytest.raises(ValueError, match="PCM16"):
        await probe.send_audio(chunk)

    await probe.end_stream()


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
    root = RuntimeScope.create_root(
        name="session",
        root_id="session:test",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    probe.set_runtime_scope(root, name="stt-provider-runtime")
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
    assert probe._receive_task in root.tasks("stt_receive_loop")
    assert "stt-receive" in root.cohorts(force=False)
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
    await probe._close_active_websocket(close_before_drain=True)
    assert root.tasks("stt_receive_loop") == ()
    await root.close()


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
async def test_receive_loop_reports_bad_frame_and_continues_to_later_messages():
    """A provider schema error is visible without truncating the stream."""

    class _MessageWS:
        died_abnormally = False
        reconnect_attempts_exhausted = None
        reconnect_exhaustion_reason = None

        async def recv_iter(self):
            yield '{"bad": true}'
            yield '{"text": "still received"}'

    class _FailOneFrameProbe(_Probe):
        def __init__(self) -> None:
            super().__init__()
            self.received: list[str] = []

        def _handle_json_message(self, msg: dict[str, Any]) -> None:
            if msg.get("bad"):
                raise ValueError("invalid provider frame")
            self.received.append(msg["text"])

    probe = _FailOneFrameProbe()
    bus = _RecordingBus()
    probe._provider_event_bus = bus
    probe._ws = _MessageWS()  # type: ignore[assignment]

    await probe._receive_loop()
    await probe._drain_emit_tasks()

    assert probe.received == ["still received"]
    errors = [event for event in bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].stage is ErrorStage.STT
    assert errors[0].provider == "probe"
    assert str(errors[0].exception) == "invalid provider frame"
    notes = getattr(errors[0].exception, "__notes__", [])
    assert "phase=receive_frame" in notes
    assert "frame_type=json" in notes
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
async def test_close_drains_pending_emit_tasks_after_websocket_cleanup():
    """Public teardown awaits in-flight emits after releasing lifecycle ownership."""

    class _FakeWS:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class _ClosingProbe(_Probe):
        async def _on_end(self) -> None:
            await self._close_active_websocket()

    probe = _ClosingProbe()
    bus = _RecordingBus()
    socket = _FakeWS()
    await probe.start_stream()
    probe._provider_event_bus = bus
    probe._ws = socket  # type: ignore[assignment]
    probe._receive_task = None

    probe._emit_provider_error(RuntimeError("boom"), code=7)
    assert len(probe._emit_tasks) == 1  # scheduled, not yet awaited

    await probe.close()

    # The emit task was awaited during teardown — nothing left pending.
    assert probe._emit_tasks == set()
    assert len(bus.events) == 1
    assert socket.close_calls == 1
