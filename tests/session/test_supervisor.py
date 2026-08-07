from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    AudioIn,
    AudioOut,
    EventBus,
    SupervisorListenerAttached,
    SupervisorListenerDetached,
)
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.session._session import Session
from easycat.supervisor import (
    SessionAudioBroadcaster,
    serve_supervisor_websocket,
    supervisor_audio_frame_to_json,
    supervisor_auth_token_from_env,
    supervisor_message_authorized,
)
from tests.session._session_core_helpers import _full_config


class _DummySession:
    def __init__(self, session_id: str = "session-test") -> None:
        self.session_id = session_id
        self.event_bus = EventBus()

    def subscribe_event(self, event_type, handler) -> None:
        self.event_bus.subscribe(event_type, handler)

    def unsubscribe_event(self, event_type, handler) -> None:
        self.event_bus.unsubscribe(event_type, handler)


class _ScopedDummySession(_DummySession):
    def __init__(self, session_id: str = "session-test") -> None:
        super().__init__(session_id)
        self._runtime_scope = RuntimeScope.create_root(
            name="session",
            root_id=f"test-root:{session_id}",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )


def _chunk(byte: int) -> AudioChunk:
    return AudioChunk(data=bytes([byte]) * 640, format=PCM16_MONO_16K)


class _FakeSupervisorWebSocket:
    def __init__(self, incoming: list[object] | None = None) -> None:
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        for item in incoming or []:
            self._incoming.put_nowait(item)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> object:
        return await self._incoming.get()

    async def close(self, code: int, reason: str) -> None:
        self.close_code = code
        self.close_reason = reason
        self._incoming.put_nowait(None)

    def feed(self, message: object) -> None:
        self._incoming.put_nowait(message)

    def __aiter__(self):
        return self

    async def __anext__(self) -> object:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def _wait_for_sent_type(
    ws: _FakeSupervisorWebSocket,
    message_type: str,
) -> dict[str, object]:
    for _ in range(100):
        for raw in ws.sent:
            message = json.loads(raw)
            if message.get("type") == message_type:
                return message
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message_type!r}; sent={ws.sent!r}")


@pytest.mark.asyncio
async def test_session_audio_broadcaster_fans_out_caller_and_assistant_audio() -> None:
    session = _DummySession()
    broadcaster = SessionAudioBroadcaster(session)
    listener_a, queue_a = broadcaster.subscribe()
    listener_b, queue_b = broadcaster.subscribe()
    await broadcaster.drain_audit_events()

    caller = _chunk(1)
    assistant = _chunk(2)

    await session.event_bus.emit(
        AudioIn(chunk=caller, session_id=session.session_id, turn_id="turn-1")
    )
    await session.event_bus.emit(
        AudioOut(chunk=assistant, session_id=session.session_id, turn_id="turn-1")
    )

    frame_a1 = await asyncio.wait_for(queue_a.get(), timeout=1.0)
    frame_a2 = await asyncio.wait_for(queue_a.get(), timeout=1.0)
    frame_b1 = await asyncio.wait_for(queue_b.get(), timeout=1.0)
    frame_b2 = await asyncio.wait_for(queue_b.get(), timeout=1.0)

    assert frame_a1 is not None
    assert frame_a2 is not None
    assert frame_b1 is not None
    assert frame_b2 is not None

    assert frame_a1.track == "caller"
    assert frame_a2.track == "assistant"
    assert frame_b1.track == "caller"
    assert frame_b2.track == "assistant"
    assert frame_a1.session_id == session.session_id
    assert frame_a2.turn_id == "turn-1"
    assert frame_b1.chunk is caller
    assert frame_b2.chunk is assistant

    broadcaster.unsubscribe(listener_a)
    broadcaster.unsubscribe(listener_b)
    await broadcaster.drain_audit_events()


@pytest.mark.asyncio
async def test_session_broadcasters_share_attached_audit_event_scope() -> None:
    session = _ScopedDummySession()
    first = SessionAudioBroadcaster(session)
    second = SessionAudioBroadcaster(session)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _handler(_event: SupervisorListenerAttached) -> None:
        entered.set()
        await release.wait()

    session.event_bus.subscribe(SupervisorListenerAttached, _handler)
    first.subscribe()
    await entered.wait()

    assert first._event_tasks is second._event_tasks
    scope = first._event_tasks.scope
    assert scope is not None
    assert scope.parent is session._runtime_scope
    assert scope.name == "supervisor-events"

    closing = asyncio.create_task(session._runtime_scope.close(phases=("supervisor-events",)))
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing

    assert not first._event_tasks.tasks()
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_audit_subscriber_can_drain_its_own_event_task() -> None:
    session = _DummySession()
    broadcaster = SessionAudioBroadcaster(session)
    drained = asyncio.Event()

    async def _handler(_event: SupervisorListenerAttached) -> None:
        await broadcaster.drain_audit_events()
        drained.set()

    session.event_bus.subscribe(SupervisorListenerAttached, _handler)
    broadcaster.subscribe()

    await asyncio.wait_for(drained.wait(), timeout=0.2)
    await asyncio.sleep(0)
    assert not broadcaster._event_tasks.tasks()
    broadcaster.close()


@pytest.mark.asyncio
async def test_session_audio_broadcaster_drops_slow_listener_frames_and_closes_cleanly() -> None:
    session = _DummySession()
    broadcaster = SessionAudioBroadcaster(session, max_listener_queue=1)
    listener_id, queue = broadcaster.subscribe()
    fast_listener_id, fast_queue = broadcaster.subscribe(max_queue_size=2)
    await broadcaster.drain_audit_events()

    first = _chunk(3)
    second = _chunk(4)

    await session.event_bus.emit(AudioIn(chunk=first, session_id=session.session_id))
    await session.event_bus.emit(AudioIn(chunk=second, session_id=session.session_id))

    queued = queue.get_nowait()
    assert queued is not None
    assert queued.chunk is first
    assert fast_queue.qsize() == 2
    assert broadcaster.dropped_frames == 1
    assert broadcaster.dropped_frames_for(listener_id) == 1
    assert broadcaster.dropped_frames_for(fast_listener_id) == 0
    assert broadcaster.dropped_frames_by_listener == {
        listener_id: 1,
        fast_listener_id: 0,
    }

    broadcaster.close()
    await broadcaster.drain_audit_events()
    assert broadcaster.listener_count == 0
    assert broadcaster.dropped_frames == 1
    assert broadcaster.dropped_frames_by_listener == {}

    sentinel = queue.get_nowait()
    assert sentinel is None

    await session.event_bus.emit(AudioOut(chunk=_chunk(5), session_id=session.session_id))

    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_session_audio_broadcaster_applies_consent_and_redaction_hooks() -> None:
    session = _DummySession()
    redacted_chunk = _chunk(9)

    def consent(frame) -> bool:
        return frame.track == "assistant"

    def redact(frame):
        return replace(frame, chunk=redacted_chunk)

    broadcaster = SessionAudioBroadcaster(
        session,
        consent_hook=consent,
        redaction_hook=redact,
    )
    _listener_id, queue = broadcaster.subscribe()
    await broadcaster.drain_audit_events()

    await session.event_bus.emit(AudioIn(chunk=_chunk(1), session_id=session.session_id))
    await session.event_bus.emit(AudioOut(chunk=_chunk(2), session_id=session.session_id))

    frame = queue.get_nowait()
    assert frame is not None
    assert frame.track == "assistant"
    assert frame.chunk is redacted_chunk
    assert broadcaster.consent_blocked_frames == 1
    assert broadcaster.redacted_frames == 1

    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_session_audio_broadcaster_can_suppress_frames_from_redaction_hook() -> None:
    session = _DummySession()
    broadcaster = SessionAudioBroadcaster(session, redaction_hook=lambda _frame: None)
    _listener_id, queue = broadcaster.subscribe()
    await broadcaster.drain_audit_events()

    await session.event_bus.emit(AudioIn(chunk=_chunk(1), session_id=session.session_id))

    assert broadcaster.redacted_frames == 1
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_session_audio_broadcaster_suppresses_async_consent_hooks() -> None:
    session = _DummySession()
    hook_body_executed = False

    async def consent(_frame):
        nonlocal hook_body_executed
        hook_body_executed = True
        return False

    broadcaster = SessionAudioBroadcaster(session, consent_hook=consent)
    _listener_id, queue = broadcaster.subscribe()
    await broadcaster.drain_audit_events()

    await session.event_bus.emit(AudioIn(chunk=_chunk(1), session_id=session.session_id))

    assert broadcaster.consent_blocked_frames == 1
    assert not hook_body_executed
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_session_audio_broadcaster_suppresses_async_redaction_hooks() -> None:
    session = _DummySession()
    hook_body_executed = False

    async def redact(frame):
        nonlocal hook_body_executed
        hook_body_executed = True
        return replace(frame, chunk=_chunk(9))

    broadcaster = SessionAudioBroadcaster(session, redaction_hook=redact)
    _listener_id, queue = broadcaster.subscribe()
    await broadcaster.drain_audit_events()

    await session.event_bus.emit(AudioIn(chunk=_chunk(1), session_id=session.session_id))

    assert broadcaster.redacted_frames == 1
    assert not hook_body_executed
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_session_audio_broadcaster_emits_listener_audit_events() -> None:
    session = _DummySession()
    attached: list[SupervisorListenerAttached] = []
    detached: list[SupervisorListenerDetached] = []
    session.event_bus.subscribe(SupervisorListenerAttached, attached.append)
    session.event_bus.subscribe(SupervisorListenerDetached, detached.append)

    broadcaster = SessionAudioBroadcaster(session, max_listener_queue=1)
    listener_id, queue = broadcaster.subscribe()
    await broadcaster.drain_audit_events()

    await session.event_bus.emit(AudioIn(chunk=_chunk(1), session_id=session.session_id))
    await session.event_bus.emit(AudioIn(chunk=_chunk(2), session_id=session.session_id))
    broadcaster.close()
    await broadcaster.drain_audit_events()

    assert len(attached) == 1
    assert attached[0].listener_id == listener_id
    assert attached[0].queue_size == 1
    assert attached[0].session_id == session.session_id

    assert len(detached) == 1
    assert detached[0].listener_id == listener_id
    assert detached[0].dropped_frames == 1
    assert detached[0].reason == "close"
    assert detached[0].session_id == session.session_id

    assert queue.get_nowait() is None


def test_supervisor_auth_token_from_env_and_message_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EASYCAT_SUPERVISOR_TOKEN", raising=False)
    assert supervisor_auth_token_from_env() is None
    assert not supervisor_message_authorized({"type": "subscribe"}, None)
    assert supervisor_message_authorized(
        {"type": "subscribe"},
        None,
        allow_unauthenticated=True,
    )

    monkeypatch.setenv("EASYCAT_SUPERVISOR_TOKEN", " secret ")
    assert supervisor_auth_token_from_env() == "secret"
    assert supervisor_message_authorized({"token": "secret"}, "secret")
    assert not supervisor_message_authorized({"token": "wrong"}, "secret")
    assert not supervisor_message_authorized({"token": 123}, "secret")
    assert not supervisor_message_authorized({"token": "café"}, "secret")
    assert not supervisor_message_authorized({"token": "secret"}, "café")


@pytest.mark.asyncio
async def test_serve_supervisor_websocket_rejects_non_ascii_token_cleanly() -> None:
    ws = _FakeSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "session-a", "token": "café"})]
    )

    await serve_supervisor_websocket(ws, {}, expected_token="secret")

    error = await _wait_for_sent_type(ws, "error")
    assert error["message"] == "Supervisor token is missing or invalid."
    assert ws.close_code == 4401
    assert ws.close_reason == "Unauthorized"


@pytest.mark.asyncio
async def test_supervisor_audio_frame_to_json() -> None:
    session = _DummySession()
    broadcaster = SessionAudioBroadcaster(session)
    _listener_id, queue = broadcaster.subscribe()
    caller = _chunk(7)

    await session.event_bus.emit(
        AudioIn(chunk=caller, session_id=session.session_id, turn_id="t1")
    )
    frame = queue.get_nowait()
    assert frame is not None

    payload = json.loads(supervisor_audio_frame_to_json(frame))
    assert payload["type"] == "audio"
    assert payload["session_id"] == session.session_id
    assert payload["track"] == "caller"
    assert payload["turn_id"] == "t1"
    assert payload["sample_rate"] == PCM16_MONO_16K.sample_rate
    assert payload["channels"] == PCM16_MONO_16K.channels
    assert payload["sample_width"] == PCM16_MONO_16K.sample_width
    assert payload["encoding"] == PCM16_MONO_16K.encoding
    assert base64.b64decode(payload["data"]) == caller.data


@pytest.mark.asyncio
async def test_serve_supervisor_websocket_subscribes_and_streams_audio() -> None:
    session = _DummySession("session-a")
    broadcaster = SessionAudioBroadcaster(session)
    ws = _FakeSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "session-a", "token": "secret"})]
    )

    task = asyncio.create_task(
        serve_supervisor_websocket(
            ws,
            {"session-a": broadcaster},
            expected_token="secret",
        )
    )
    await _wait_for_sent_type(ws, "subscribed")
    assert broadcaster.listener_count == 1

    await session.event_bus.emit(AudioOut(chunk=_chunk(8), session_id=session.session_id))
    audio = await _wait_for_sent_type(ws, "audio")
    assert audio["session_id"] == "session-a"
    assert audio["track"] == "assistant"

    ws.feed(None)
    await asyncio.wait_for(task, timeout=1.0)
    assert broadcaster.listener_count == 0


@pytest.mark.asyncio
async def test_supervisor_stream_workers_are_cancelled_with_session_scope() -> None:
    session = _ScopedDummySession("session-a")
    broadcaster = SessionAudioBroadcaster(session)
    ws = _FakeSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "session-a", "token": "secret"})]
    )
    serving = asyncio.create_task(
        serve_supervisor_websocket(
            ws,
            {"session-a": broadcaster},
            expected_token="secret",
        )
    )
    await _wait_for_sent_type(ws, "subscribed")
    await asyncio.sleep(0)

    assert {task.get_name() for task in session._runtime_scope.tasks()} == {
        serving.get_name(),
        "supervisor-queue-0",
        "supervisor-recv-0",
    }

    await session._runtime_scope.close()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert broadcaster.listener_count == 0
    assert session._runtime_scope.tasks("supervisor_stream_0") == ()
    await broadcaster.drain_audit_events()
    assert session._runtime_scope.empty


@pytest.mark.asyncio
async def test_graceful_session_stop_joins_supervisor_handler_and_workers() -> None:
    session = Session(_full_config())
    broadcaster = SessionAudioBroadcaster(session)
    ws = _FakeSupervisorWebSocket(
        [
            json.dumps(
                {
                    "type": "subscribe",
                    "session_id": session.session_id,
                    "token": "secret",
                }
            )
        ]
    )
    serving = asyncio.create_task(
        serve_supervisor_websocket(
            ws,
            {session.session_id: broadcaster},
            expected_token="secret",
        )
    )
    await _wait_for_sent_type(ws, "subscribed")

    await session.stop()

    with pytest.raises(asyncio.CancelledError):
        await serving
    assert broadcaster.listener_count == 0
    assert not session._runtime_scope.tasks("supervisor_stream_0")


@pytest.mark.asyncio
async def test_cancelling_supervisor_handler_joins_both_stream_workers() -> None:
    session = _ScopedDummySession("session-a")
    broadcaster = SessionAudioBroadcaster(session)
    ws = _FakeSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "session-a", "token": "secret"})]
    )
    serving = asyncio.create_task(
        serve_supervisor_websocket(
            ws,
            {"session-a": broadcaster},
            expected_token="secret",
        )
    )
    await _wait_for_sent_type(ws, "subscribed")
    await asyncio.sleep(0)

    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert broadcaster.listener_count == 0
    assert session._runtime_scope.tasks("supervisor_stream_0") == ()
    await broadcaster.drain_audit_events()
    assert session._runtime_scope.empty


@pytest.mark.asyncio
async def test_supervisor_inbound_worker_failure_propagates_after_sibling_cleanup() -> None:
    class _FailingSupervisorWebSocket(_FakeSupervisorWebSocket):
        async def __anext__(self) -> object:
            raise RuntimeError("inbound failed")

    session = _ScopedDummySession("session-a")
    broadcaster = SessionAudioBroadcaster(session)
    ws = _FailingSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "session-a", "token": "secret"})]
    )

    with pytest.raises(RuntimeError, match="inbound failed"):
        await serve_supervisor_websocket(
            ws,
            {"session-a": broadcaster},
            expected_token="secret",
        )

    assert broadcaster.listener_count == 0
    assert session._runtime_scope.tasks("supervisor_stream_0") == ()
    await broadcaster.drain_audit_events()
    assert session._runtime_scope.empty


@pytest.mark.asyncio
async def test_serve_supervisor_websocket_rejects_unknown_session() -> None:
    ws = _FakeSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "missing", "token": "secret"})]
    )

    await serve_supervisor_websocket(ws, {}, expected_token="secret")

    error = await _wait_for_sent_type(ws, "error")
    assert error["message"] == "No active caller session found for missing."
    assert ws.close_code == 4404
    assert ws.close_reason == "Unknown session"


@pytest.mark.asyncio
async def test_serve_supervisor_websocket_fails_closed_without_token() -> None:
    ws = _FakeSupervisorWebSocket([json.dumps({"type": "subscribe", "session_id": "session-a"})])

    await serve_supervisor_websocket(ws, {}, expected_token=None)

    error = await _wait_for_sent_type(ws, "error")
    assert error["message"] == "Supervisor token is not configured; set EASYCAT_SUPERVISOR_TOKEN."
    assert ws.close_code == 4401
    assert ws.close_reason == "Unauthorized"


@pytest.mark.asyncio
async def test_serve_supervisor_websocket_rejects_bad_token() -> None:
    session = _DummySession("session-a")
    broadcaster = SessionAudioBroadcaster(session)
    ws = _FakeSupervisorWebSocket(
        [json.dumps({"type": "subscribe", "session_id": "session-a", "token": "wrong"})]
    )

    await serve_supervisor_websocket(
        ws,
        {"session-a": broadcaster},
        expected_token="secret",
    )

    error = await _wait_for_sent_type(ws, "error")
    assert error["message"] == "Supervisor token is missing or invalid."
    assert ws.close_code == 4401
    assert ws.close_reason == "Unauthorized"
    assert broadcaster.listener_count == 0
