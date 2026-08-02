"""Bounded audio queues with configurable drop policies.

Provides BoundedAudioQueue for both inbound (mic -> processing) and
outbound (TTS -> playback) audio, preventing unbounded memory growth.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections import deque
from collections.abc import Callable

from easycat import _observability as observability
from easycat.audio_format import AudioChunk

logger = logging.getLogger(__name__)
DropCallback = Callable[[str, str, int, int], None]


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
        on_drop: DropCallback | None = None,
    ) -> None:
        if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size <= 0:
            raise ValueError("max_size must be a positive integer")
        if not isinstance(policy, DropPolicy):
            raise ValueError("policy must be a DropPolicy")  # noqa: TRY004 domain-specific validation error
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
            except Exception:
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
            return self._append(chunk)

        if self._policy == DropPolicy.DROP_OLDEST:
            return self._replace_oldest(chunk)
        if self._policy == DropPolicy.DROP_NEWEST:
            return self._reject("drop_newest", "dropped newest chunk")
        if self._policy == DropPolicy.BLOCK:
            return await self._put_after_wait(chunk)

        raise AssertionError("unreachable DropPolicy")  # pragma: no cover

    def _append(self, chunk: AudioChunk) -> bool:
        self._queue.append(chunk)
        self._not_empty.set()
        if self.full():
            self._not_full.clear()
        self._observe_depth()
        return True

    def _replace_oldest(self, chunk: AudioChunk) -> bool:
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

    def _reject(self, kind: str, detail: str) -> bool:
        self._note_drop(kind)
        logger.debug("Queue '%s' %s (total drops: %d)", self._name, detail, self._drops)
        return False

    async def _put_after_wait(self, chunk: AudioChunk) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._block_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._reject("block_timeout", "block timed out, dropping")
            try:
                await asyncio.wait_for(self._not_full.wait(), timeout=remaining)
            except TimeoutError:
                return self._reject("block_timeout", "block timed out, dropping")

            # A get wakes every producer. Serialize the re-check so only one
            # claims each newly available slot; a producer that loses this
            # race waits again for the remainder of its original budget.
            async with self._put_lock:
                if self._closed:
                    return False
                if not self.full():
                    return self._append(chunk)

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
