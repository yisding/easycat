"""Queue-backed Deepgram WebSocket fake shared by tests and benchmarks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable


class QueueDeepgramSocket:
    """Model sequential Speak/Flush/Clear cycles without network I/O."""

    def __init__(
        self,
        messages: Iterable[bytes | str] = (),
        *,
        audio: bytes = b"\x00\x00" * 120,
        auto_flush: bool = True,
        acknowledge_clear: bool = True,
        hold_first_flush: bool = False,
        fail_connect: bool = False,
        connect_delay_s: float = 0.0,
    ) -> None:
        self._queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        for message in messages:
            self._queue.put_nowait(message)
        self._audio = audio
        self._auto_flush = auto_flush
        self._acknowledge_clear = acknowledge_clear
        self._hold_first_flush = hold_first_flush
        self._fail_connect = fail_connect
        self._connect_delay_s = connect_delay_s
        self._pending_text: str | None = None
        self._connected = False
        self._closed = False
        self.connect_calls = 0
        self.sent: list[dict[str, str]] = []
        self._sent: list[str | bytes] = []

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_delay_s:
            await asyncio.sleep(self._connect_delay_s)
        if self._fail_connect:
            raise RuntimeError("connect boom")
        self._connected = True

    async def send(self, message: str | bytes) -> None:
        assert isinstance(message, str)
        frame = json.loads(message)
        self._sent.append(message)
        self.sent.append(frame)
        if frame["type"] == "Speak":
            self._pending_text = frame["text"]
        elif frame["type"] == "Flush":
            if self._auto_flush:
                assert self._pending_text is not None
                await self._queue.put(self._audio)
                if self._hold_first_flush:
                    self._hold_first_flush = False
                else:
                    await self._queue.put(json.dumps({"type": "Flushed"}))
            self._pending_text = None
        elif frame["type"] == "Clear":
            self._pending_text = None
            if self._acknowledge_clear:
                await self._queue.put(json.dumps({"type": "Cleared"}))

    async def recv_iter(self) -> AsyncIterator[bytes | str]:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self._closed = True
        self._connected = False
        await self._queue.put(None)
