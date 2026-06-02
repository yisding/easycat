"""Regression tests for bounded TurnManager audio buffers."""

from __future__ import annotations

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import EventBus, VADStartSpeaking
from easycat.transports.websocket import WebSocketConnectionTransport, WebSocketTransport
from easycat.turn_manager import TurnManager, TurnManagerConfig


def _chunk(n_bytes: int = 640, value: int = 0) -> AudioChunk:
    return AudioChunk(data=bytes([value & 0xFF] * n_bytes), format=PCM16_MONO_16K)


class _FakeWebSocket:
    def __init__(self, messages: list[bytes | str]) -> None:
        self._messages = messages

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> bytes | str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def test_zero_duration_chunks_are_not_retained() -> None:
    tm = TurnManager(EventBus())

    for _ in range(100):
        tm.on_audio_frame(_chunk(0))

    assert len(tm._pre_roll_buffer) == 0
    assert tm._pre_roll_duration_ms == 0.0


@pytest.mark.asyncio
async def test_turn_audio_is_bounded_during_active_turn() -> None:
    config = TurnManagerConfig(max_turn_audio_ms=60, max_turn_audio_chunks=3)
    tm = TurnManager(EventBus(), config=config)

    await tm.on_vad_event(VADStartSpeaking())
    chunks = [_chunk(value=i) for i in range(10)]
    for chunk in chunks:
        tm.on_audio_frame(chunk)

    assert tm.turn_audio == chunks[-3:]
    assert tm._turn_audio_duration_ms <= config.max_turn_audio_ms


def test_pre_roll_is_bounded_by_chunk_count_for_tiny_frames() -> None:
    config = TurnManagerConfig(pre_roll_ms=300, max_pre_roll_chunks=4)
    tm = TurnManager(EventBus(), config=config)

    chunks = [_chunk(2, value=i) for i in range(20)]
    for chunk in chunks:
        tm.on_audio_frame(chunk)

    assert list(tm._pre_roll_buffer) == chunks[-4:]


@pytest.mark.asyncio
async def test_websocket_drops_empty_binary_frames() -> None:
    transport = WebSocketTransport()
    ws = _FakeWebSocket([b"", b"\0\0"])

    await transport._receive_loop(ws)  # type: ignore[arg-type]

    queued = transport._in_queue.get_nowait()
    assert queued is not None
    assert queued.data == b"\0\0"
    assert transport._in_queue.empty()


@pytest.mark.asyncio
async def test_websocket_connection_drops_empty_binary_frames() -> None:
    ws = _FakeWebSocket([b"", b"\0\0"])
    transport = WebSocketConnectionTransport(ws)  # type: ignore[arg-type]

    await transport._receive_loop()

    queued = transport._in_queue.get_nowait()
    assert queued is not None
    assert queued.data == b"\0\0"
    assert transport._in_queue.get_nowait() is None
    assert transport._in_queue.empty()
