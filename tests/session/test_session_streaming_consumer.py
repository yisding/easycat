"""Agent stream consumer and bounded queue behavior tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.integrations.agents.base import AgentBridgeEvent
from easycat.tts.input import TTSInput


async def test_consume_agent_stream_captures_done_on_cancel_without_tool_calls():
    """A cancelled stream with no pending tool calls still surfaces the
    trailing ``done`` payload (text + structured_output) instead of
    silently discarding it."""
    from easycat.session._streaming import consume_agent_stream

    cancel_token = CancelToken()

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        # Cancel before yielding so the very first event hits the
        # pending_tool_calls == 0 cancellation branch.
        cancel_token.cancel()
        yield AgentBridgeEvent(kind="done", text="final text", structured_output={"answer": 42})

    turn = TurnContext(turn_id="t1", cancel_token=cancel_token)
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()

    result = await consume_agent_stream(
        _stream,
        cancel_token=cancel_token,
        tts_queue=tts_queue,
        emit=AsyncMock(),
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=False,
        turn=turn,
    )

    assert result.interrupted is True
    assert result.text == "final text"
    assert result.structured_output == {"answer": 42}


async def test_consume_agent_stream_applies_backpressure_on_bounded_queue():
    """A fast producer against a bounded queue blocks on put() rather than
    growing unbounded, then proceeds once the consumer drains."""
    from easycat.session._streaming import consume_agent_stream

    cancel_token = CancelToken()

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        for _ in range(5):
            yield AgentBridgeEvent(kind="text_delta", text="Hello world. ")
        yield AgentBridgeEvent(kind="done", text="")

    turn = TurnContext(turn_id="t1", cancel_token=cancel_token)
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue(maxsize=1)

    consumer_task = asyncio.create_task(
        consume_agent_stream(
            _stream,
            cancel_token=cancel_token,
            tts_queue=tts_queue,
            emit=AsyncMock(),
            prepare_tts_payload=lambda text, **_: TTSInput(text=text),
            strip_md=False,
            turn=turn,
        )
    )

    # With maxsize=1 and multiple sentences, the producer must block on put()
    # until the queue is drained; it cannot accumulate without bound.
    await asyncio.sleep(0)
    assert tts_queue.qsize() <= 1

    # Drain so the producer can finish.
    drained: list[TTSInput] = []
    while True:
        item = await tts_queue.get()
        if item is None:
            break
        drained.append(item)

    result = await consumer_task
    assert result.error is None
    assert len(drained) >= 1


async def test_consume_agent_stream_strip_markdown_defers_work_until_flush(monkeypatch):
    """Tiny deltas without sentence boundaries should not re-strip the full buffer."""
    from easycat.session import _streaming
    from easycat.session._streaming import consume_agent_stream

    strip_calls: list[str] = []
    delimiter_calls: list[str] = []

    def _counting_strip_markdown(
        text: str, *, trim: bool = True, normalize_code_spans: bool = False
    ) -> str:
        _ = normalize_code_spans
        strip_calls.append(text)
        return text.strip() if trim else text

    def _counting_markdown_open_state(text: str) -> tuple[bool, bool]:
        delimiter_calls.append(text)
        return False, False

    monkeypatch.setattr(_streaming, "strip_markdown", _counting_strip_markdown)
    monkeypatch.setattr(
        _streaming,
        "markdown_open_state",
        _counting_markdown_open_state,
    )

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        for _ in range(128):
            yield AgentBridgeEvent(kind="text_delta", text="a")
        yield AgentBridgeEvent(kind="done", text="")

    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()

    result = await consume_agent_stream(
        _stream,
        cancel_token=turn.cancel_token,
        tts_queue=tts_queue,
        emit=AsyncMock(),
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=True,
        turn=turn,
    )

    assert result.error is None
    assert len(delimiter_calls) == 0
    assert strip_calls == ["a" * 128]
    assert (await tts_queue.get()).text == "a" * 128
    assert await tts_queue.get() is None


async def test_consume_agent_stream_strip_markdown_disambiguates_open_link_mid_stream():
    """A bracket awaiting a destination must not stall streaming.

    After ``"First sentence. See [note]"`` the buffer is open only because
    ``[note]`` might be a markdown link; an ordinary-prose continuation
    (no markdown-closer char) disambiguates it, so the first sentence must
    be queued to TTS *during* streaming, not deferred to the final flush.
    """
    from easycat.session._streaming import consume_agent_stream

    deltas = [
        "First sentence. See [note]",
        " in the docs and more plain prose",
    ]

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        for delta in deltas:
            yield AgentBridgeEvent(kind="text_delta", text=delta)
        yield AgentBridgeEvent(kind="done", text="")

    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()

    # Record the (text, is_final) of every payload built so we can prove the
    # first sentence was queued as a mid-stream (is_final=False) payload
    # rather than only at the final flush (is_final=True).
    built: list[tuple[str, bool]] = []

    def _prepare(text: str, *, is_streaming: bool = True, is_final: bool = False) -> TTSInput:
        _ = is_streaming
        built.append((text, is_final))
        return TTSInput(text=text)

    result = await consume_agent_stream(
        _stream,
        cancel_token=turn.cancel_token,
        tts_queue=tts_queue,
        emit=AsyncMock(),
        prepare_tts_payload=_prepare,
        strip_md=True,
        turn=turn,
    )
    assert result.error is None

    # Drain the queue.
    payloads: list[TTSInput] = []
    while True:
        item = await tts_queue.get()
        if item is None:
            break
        payloads.append(item)

    # The disambiguated first sentence must be queued during streaming
    # (is_final=False); if the open link bracket had stalled streaming, the
    # only payload would be the single final-flush (is_final=True) chunk.
    streaming_texts = [text for text, is_final in built if not is_final]
    assert streaming_texts, "first sentence should be queued before the final flush"
    assert streaming_texts[0].strip().startswith("First sentence.")
    # The bracketed label survives stripping (no destination -> plain text).
    joined = " ".join(p.text for p in payloads)
    assert "note" in joined
    assert "more plain prose" in joined


async def test_consume_agent_stream_sentinel_skipped_when_consumer_stopped():
    """If the bounded queue is full and the consumer stopped draining, the
    stop sentinel is dropped instead of deadlocking the finally block."""
    from easycat.session._streaming import consume_agent_stream

    cancel_token = CancelToken()

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        # Cancel up front so the producer takes the cancellation break before
        # putting anything, exercising the finally-block sentinel put.
        cancel_token.cancel()
        yield AgentBridgeEvent(kind="done", text="final")

    turn = TurnContext(turn_id="t1", cancel_token=cancel_token)
    # Pre-fill the queue to capacity so the sentinel put_nowait would raise
    # QueueFull; the producer must swallow it rather than block forever.
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue(maxsize=1)
    tts_queue.put_nowait(TTSInput(text="already here"))

    result = await asyncio.wait_for(
        consume_agent_stream(
            _stream,
            cancel_token=cancel_token,
            tts_queue=tts_queue,
            emit=AsyncMock(),
            prepare_tts_payload=lambda text, **_: TTSInput(text=text),
            strip_md=False,
            turn=turn,
        ),
        timeout=2.0,
    )
    assert result.interrupted is True
