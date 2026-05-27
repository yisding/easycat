"""Tests for bounded audio queues."""

from __future__ import annotations

import asyncio

import pytest

from easycat._bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.audio_format import AudioChunk, AudioFormat

# Helper to create test audio chunks
_fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)


def _chunk(data: bytes = b"\x00\x01") -> AudioChunk:
    return AudioChunk(data=data, format=_fmt)


# ── Basic queue operations ─────────────────────────────────────────


class TestBoundedAudioQueueBasic:
    async def test_put_and_get(self):
        q = BoundedAudioQueue(max_size=10)
        c = _chunk(b"\x01")
        await q.put(c)
        result = await q.get()
        assert result.data == b"\x01"

    async def test_qsize(self):
        q = BoundedAudioQueue(max_size=10)
        assert q.qsize() == 0
        await q.put(_chunk())
        assert q.qsize() == 1
        await q.get()
        assert q.qsize() == 0

    async def test_empty_and_full(self):
        q = BoundedAudioQueue(max_size=2)
        assert q.empty()
        assert not q.full()

        await q.put(_chunk())
        await q.put(_chunk())
        assert q.full()
        assert not q.empty()

    def test_get_nowait(self):
        q = BoundedAudioQueue(max_size=10)
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()

    async def test_get_nowait_success(self):
        q = BoundedAudioQueue(max_size=10)
        c = _chunk(b"\x42")
        await q.put(c)
        result = q.get_nowait()
        assert result.data == b"\x42"

    async def test_properties(self):
        q = BoundedAudioQueue(max_size=5, policy=DropPolicy.DROP_NEWEST)
        assert q.max_size == 5
        assert q.policy == DropPolicy.DROP_NEWEST


# ── Drop policies (Task 8.6) ──────────────────────────────────────


class TestDropOldest:
    async def test_drops_oldest_when_full(self):
        q = BoundedAudioQueue(max_size=3, policy=DropPolicy.DROP_OLDEST)

        for i in range(3):
            await q.put(_chunk(bytes([i])))

        assert q.full()
        assert q.drops == 0

        # Put one more — should drop oldest (0x00)
        await q.put(_chunk(bytes([3])))
        assert q.drops == 1
        assert q.qsize() == 3

        # The oldest item should now be 0x01
        result = await q.get()
        assert result.data == bytes([1])

    async def test_multiple_drops(self):
        q = BoundedAudioQueue(max_size=2, policy=DropPolicy.DROP_OLDEST)

        await q.put(_chunk(b"\x01"))
        await q.put(_chunk(b"\x02"))
        await q.put(_chunk(b"\x03"))
        await q.put(_chunk(b"\x04"))

        assert q.drops == 2
        # Queue should contain [0x03, 0x04]
        r1 = await q.get()
        r2 = await q.get()
        assert r1.data == b"\x03"
        assert r2.data == b"\x04"


class TestDropNewest:
    async def test_drops_newest_when_full(self):
        q = BoundedAudioQueue(max_size=2, policy=DropPolicy.DROP_NEWEST)

        await q.put(_chunk(b"\x01"))
        await q.put(_chunk(b"\x02"))
        result = await q.put(_chunk(b"\x03"))

        assert result is False
        assert q.drops == 1
        assert q.qsize() == 2

        # Queue still has original items
        r1 = await q.get()
        r2 = await q.get()
        assert r1.data == b"\x01"
        assert r2.data == b"\x02"


class TestBlock:
    async def test_block_times_out(self):
        q = BoundedAudioQueue(max_size=1, policy=DropPolicy.BLOCK, block_timeout=0.05)
        await q.put(_chunk(b"\x01"))
        result = await q.put(_chunk(b"\x02"))  # should block then timeout
        assert result is False
        assert q.drops == 1

    async def test_block_succeeds_when_space_freed(self):
        q = BoundedAudioQueue(max_size=1, policy=DropPolicy.BLOCK, block_timeout=1.0)
        await q.put(_chunk(b"\x01"))

        async def free_space():
            await asyncio.sleep(0.05)
            await q.get()

        task = asyncio.create_task(free_space())
        result = await q.put(_chunk(b"\x02"))
        await task

        assert result is True
        assert q.drops == 0


# ── Flush / stale audio (Task 8.7) ────────────────────────────────


class TestFlush:
    async def test_flush_clears_queue(self):
        q = BoundedAudioQueue(max_size=10)
        for i in range(5):
            await q.put(_chunk(bytes([i])))

        flushed = q.flush()
        assert len(flushed) == 5
        assert q.empty()
        assert q.qsize() == 0

    async def test_flush_for_new_turn(self):
        q = BoundedAudioQueue(max_size=10)
        for i in range(3):
            await q.put(_chunk(bytes([i])))

        initial_turn = q.turn_id
        flushed = q.flush_for_new_turn()

        assert len(flushed) == 3
        assert q.empty()
        assert q.turn_id == initial_turn + 1

    async def test_stale_audio_not_in_new_turn(self):
        """Queue has audio from turn 1 -> cancel -> verify queue is empty for turn 2."""
        q = BoundedAudioQueue(max_size=10)

        # Turn 1: add some audio
        for i in range(3):
            await q.put(_chunk(bytes([i])))
        assert q.turn_id == 0

        # Cancel turn: flush for new turn
        q.flush_for_new_turn()
        assert q.empty()
        assert q.turn_id == 1

        # Turn 2: new audio
        await q.put(_chunk(b"\xff"))
        result = await q.get()
        assert result.data == b"\xff"

    async def test_flush_resets_drop_counter(self):
        q = BoundedAudioQueue(max_size=1, policy=DropPolicy.DROP_OLDEST)
        await q.put(_chunk())
        await q.put(_chunk())
        assert q.drops == 1

        q.flush_for_new_turn()
        assert q.drops == 0


class TestClose:
    async def test_put_after_close_returns_false(self):
        q = BoundedAudioQueue(max_size=10)
        q.close()
        result = await q.put(_chunk())
        assert result is False

    async def test_reset_drops(self):
        q = BoundedAudioQueue(max_size=1, policy=DropPolicy.DROP_OLDEST)
        await q.put(_chunk())
        await q.put(_chunk())
        count = q.reset_drops()
        assert count == 1
        assert q.drops == 0
