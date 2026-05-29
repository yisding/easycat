"""Bounded audio queues with configurable drop policies.

Provides BoundedAudioQueue for both inbound (mic -> processing) and
outbound (TTS -> playback) audio, preventing unbounded memory growth.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections import deque
from typing import Any

from easycat import _observability as observability
from easycat.audio_format import AudioChunk

logger = logging.getLogger(__name__)


class DropPolicy(enum.Enum):
    """Policy for handling a full queue."""

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    BLOCK = "block"


class BoundedAudioQueue:
    """Bounded queue for audio chunks with configurable overflow policy.

    Used for both inbound (mic -> processing) and outbound (TTS -> playback)
    audio. Prevents unbounded memory growth during slow consumers or fast
    producers.
    """

    def __init__(
        self,
        max_size: int = 100,
        policy: DropPolicy = DropPolicy.DROP_OLDEST,
        block_timeout: float = 5.0,
        name: str = "audio_queue",
        on_drop: Any = None,
    ) -> None:
        self._max_size = max_size
        self._policy = policy
        self._block_timeout = block_timeout
        self._name = name
        self._queue: deque[AudioChunk] = deque()
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()
        self._put_lock = asyncio.Lock()
        self._closed = False
        self._drops = 0
        self._turn_id: int = 0
        # ``on_drop(name, kind, queue_len, total_drops)`` is called when
        # a chunk is dropped so Session can journal backpressure events
        # without polling ``drops`` — useful for bundle readers that
        # need to correlate audio gaps to queue pressure.
        self._on_drop = on_drop

    def _note_drop(self, kind: str) -> None:
        """Increment the drop counter and notify the hook (if any)."""
        self._drops += 1
        observability.increment_counter(
            "easycat.queue.dropped.total",
            attributes={"easycat.stage": "audio_queue"},
        )
        self._observe_depth()
        hook = self._on_drop
        if hook is not None:
            try:
                hook(self._name, kind, len(self._queue), self._drops)
            except Exception:  # noqa: BLE001 - drop hook must never break the queue
                logger.debug("on_drop hook raised", exc_info=True)

    def _observe_depth(self, value: int | None = None) -> None:
        observability.observe_gauge(
            "easycat.queue.depth",
            len(self._queue) if value is None else value,
            attributes={"easycat.stage": "audio_queue"},
        )

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def policy(self) -> DropPolicy:
        return self._policy

    @property
    def drops(self) -> int:
        """Number of chunks dropped since last reset."""
        return self._drops

    @property
    def turn_id(self) -> int:
        """Current turn identifier for stale-flush detection."""
        return self._turn_id

    def qsize(self) -> int:
        return len(self._queue)

    def empty(self) -> bool:
        return len(self._queue) == 0

    def full(self) -> bool:
        return len(self._queue) >= self._max_size

    async def put(self, chunk: AudioChunk) -> bool:
        """Add a chunk to the queue. Returns False if dropped."""
        if self._closed:
            return False

        if not self.full():
            self._queue.append(chunk)
            self._not_empty.set()
            if self.full():
                self._not_full.clear()
            self._observe_depth()
            return True

        if self._policy == DropPolicy.DROP_OLDEST:
            self._queue.popleft()
            self._queue.append(chunk)
            self._note_drop("drop_oldest")
            logger.debug(
                "Queue '%s' dropped oldest chunk (total drops: %d)",
                self._name,
                self._drops,
            )
            self._not_empty.set()
            return True

        elif self._policy == DropPolicy.DROP_NEWEST:
            self._note_drop("drop_newest")
            logger.debug(
                "Queue '%s' dropped newest chunk (total drops: %d)", self._name, self._drops
            )
            return False

        elif self._policy == DropPolicy.BLOCK:
            try:
                await asyncio.wait_for(self._not_full.wait(), timeout=self._block_timeout)
            except TimeoutError:
                self._note_drop("block_timeout")
                logger.debug(
                    "Queue '%s' block timed out, dropping (total drops: %d)",
                    self._name,
                    self._drops,
                )
                return False
            # Serialize the append under a lock and re-check fullness:
            # multiple producers may have been woken by a single get(),
            # so only the first to acquire the lock should append.
            async with self._put_lock:
                if self._closed:
                    return False
                if self.full():
                    self._note_drop("block_lost_race")
                    logger.debug(
                        "Queue '%s' lost race after BLOCK wait, dropping (total drops: %d)",
                        self._name,
                        self._drops,
                    )
                    return False
                self._queue.append(chunk)
                self._not_empty.set()
                if self.full():
                    self._not_full.clear()
                self._observe_depth()
                return True

        return False  # pragma: no cover

    async def get(self) -> AudioChunk:
        """Remove and return a chunk from the queue. Blocks until available."""
        while self.empty():
            if self._closed:
                raise asyncio.QueueEmpty()
            self._not_empty.clear()
            await self._not_empty.wait()

        chunk = self._queue.popleft()
        self._not_full.set()
        if self.empty():
            self._not_empty.clear()
        self._observe_depth()
        return chunk

    def get_nowait(self) -> AudioChunk:
        """Remove and return a chunk without blocking. Raises if empty."""
        if self.empty():
            raise asyncio.QueueEmpty()
        chunk = self._queue.popleft()
        self._not_full.set()
        if self.empty():
            self._not_empty.clear()
        self._observe_depth()
        return chunk

    def flush(self) -> list[AudioChunk]:
        """Discard all queued chunks, returning them. Used for cancellation."""
        flushed = list(self._queue)
        self._queue.clear()
        self._not_full.set()
        self._not_empty.clear()
        self._observe_depth()
        return flushed

    def flush_for_new_turn(self) -> list[AudioChunk]:
        """Flush stale audio and advance the turn counter.

        Called on turn cancellation or barge-in to ensure the next turn
        starts with clean queues.
        """
        flushed = self.flush()
        self._turn_id += 1
        self._drops = 0
        return flushed

    def close(self) -> None:
        """Mark the queue as closed. Wakes up any waiters."""
        self._closed = True
        self._observe_depth(0)
        self._not_empty.set()
        self._not_full.set()

    def reset_drops(self) -> int:
        """Reset the drop counter. Returns the count before reset."""
        count = self._drops
        self._drops = 0
        return count
