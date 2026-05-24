from __future__ import annotations

import asyncio

import pytest

from easycat._supervisor import SessionAudioBroadcaster
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import AudioIn, AudioOut, EventBus


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


@pytest.mark.asyncio
async def test_session_audio_broadcaster_drops_slow_listener_frames_and_closes_cleanly() -> None:
    session = _DummySession()
    broadcaster = SessionAudioBroadcaster(session, max_listener_queue=1)
    _listener_id, queue = broadcaster.subscribe()

    first = _chunk(3)
    second = _chunk(4)

    await session.event_bus.emit(AudioIn(chunk=first, session_id=session.session_id))
    await session.event_bus.emit(AudioIn(chunk=second, session_id=session.session_id))

    queued = queue.get_nowait()
    assert queued is not None
    assert queued.chunk is first
    assert broadcaster.dropped_frames == 1

    broadcaster.close()
    assert broadcaster.listener_count == 0

    sentinel = queue.get_nowait()
    assert sentinel is None

    await session.event_bus.emit(AudioOut(chunk=_chunk(5), session_id=session.session_id))

    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()
