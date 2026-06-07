from __future__ import annotations

import asyncio
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
from easycat.supervisor import SessionAudioBroadcaster


class _DummySession:
    def __init__(self, session_id: str = "session-test") -> None:
        self.session_id = session_id
        self.event_bus = EventBus()

    def subscribe_event(self, event_type, handler) -> None:  # noqa: ANN001,ANN201
        self.event_bus.subscribe(event_type, handler)

    def unsubscribe_event(self, event_type, handler) -> None:  # noqa: ANN001,ANN201
        self.event_bus.unsubscribe(event_type, handler)


def _chunk(byte: int) -> AudioChunk:
    return AudioChunk(data=bytes([byte]) * 640, format=PCM16_MONO_16K)


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

    def consent(frame) -> bool:  # noqa: ANN001
        return frame.track == "assistant"

    def redact(frame):  # noqa: ANN001,ANN201
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
