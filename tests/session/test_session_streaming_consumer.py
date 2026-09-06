"""Agent stream consumer and bounded queue behavior tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import AgentDelta
from easycat.integrations.agents.base import AgentBridgeEvent
from easycat.tts.input import TTSInput


async def test_indexed_replacement_updates_buffer_before_tts_admission():
    from easycat.session._streaming import consume_agent_stream

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_replace", text="stale fragment", part_index=0)
        yield AgentBridgeEvent(kind="text_replace", text="Correct sentence.", part_index=0)
        yield AgentBridgeEvent(kind="done", text="Correct sentence.")

    turn = TurnContext(turn_id="replacement-buffered", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    emitted: list[object] = []

    result = await consume_agent_stream(
        _stream,
        cancel_token=turn.cancel_token,
        tts_queue=tts_queue,
        emit=lambda event: _append_event(emitted, event),
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=False,
        turn=turn,
    )

    assert result.text == "Correct sentence."
    assert (await tts_queue.get()).text == "Correct sentence."
    assert await tts_queue.get() is None
    deltas = [event for event in emitted if isinstance(event, AgentDelta)]
    assert [(event.text, event.part_index, event.replacement) for event in deltas] == [
        ("stale fragment", 0, True),
        ("Correct sentence.", 0, True),
    ]


async def test_indexed_replacement_cuts_off_tts_after_payload_admission():
    from easycat.session._streaming import consume_agent_stream

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_replace", text="Stale sentence.", part_index=0)
        yield AgentBridgeEvent(kind="text_replace", text="Correct sentence.", part_index=0)
        yield AgentBridgeEvent(kind="done", text="Correct sentence.")

    turn = TurnContext(turn_id="replacement-spoken", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    cutoff = AsyncMock()

    result = await consume_agent_stream(
        _stream,
        cancel_token=turn.cancel_token,
        tts_queue=tts_queue,
        emit=AsyncMock(),
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=False,
        turn=turn,
        on_tts_replacement_conflict=cutoff,
    )

    assert result.text == "Correct sentence."
    cutoff.assert_awaited_once_with()
    assert await tts_queue.get() is None
    assert tts_queue.empty()


async def _append_event(events: list[object], event: object) -> None:
    events.append(event)


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


async def test_first_payload_gate_tracks_clause_completing_delta_dispatch():
    from easycat.session._streaming import consume_agent_stream

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_delta", text="Hello world")
        yield AgentBridgeEvent(kind="text_delta", text=".")
        yield AgentBridgeEvent(kind="done", text="Hello world.")

    second_handler_started = asyncio.Event()
    release_second_handler = asyncio.Event()
    delta_count = 0

    async def _emit(event: object) -> None:
        nonlocal delta_count
        if not isinstance(event, AgentDelta):
            return
        delta_count += 1
        if delta_count == 2:
            second_handler_started.set()
            await release_second_handler.wait()

    turn = TurnContext(turn_id="t-first-payload", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    gate: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        consume_agent_stream(
            _stream,
            cancel_token=turn.cancel_token,
            tts_queue=tts_queue,
            emit=_emit,
            prepare_tts_payload=lambda text, **_: TTSInput(text=text),
            strip_md=False,
            turn=turn,
            first_tts_payload_ready=gate,
        )
    )

    await asyncio.wait_for(second_handler_started.wait(), timeout=0.5)
    assert tts_queue.qsize() == 1
    assert not gate.done()

    release_second_handler.set()
    result = await asyncio.wait_for(task, timeout=0.5)

    assert result.error is None
    assert gate.result() is True


async def test_first_payload_gate_rejects_prepare_failure():
    from easycat.session._streaming import consume_agent_stream

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_delta", text="Hello world.")

    def _fail_prepare(text: str, **_: object) -> TTSInput:
        raise RuntimeError(f"cannot prepare {text!r}")

    turn = TurnContext(turn_id="t-prepare-failure", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    gate: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    result = await asyncio.wait_for(
        consume_agent_stream(
            _stream,
            cancel_token=turn.cancel_token,
            tts_queue=tts_queue,
            emit=AsyncMock(),
            prepare_tts_payload=_fail_prepare,
            strip_md=False,
            turn=turn,
            first_tts_payload_ready=gate,
        ),
        timeout=0.5,
    )

    assert isinstance(result.error, RuntimeError)
    assert gate.result() is False
    assert tts_queue.get_nowait() is None


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


async def test_consume_agent_stream_strip_markdown_rechecks_digit_colon_after_plain_delta():
    """A digit-ending colon should emit once the next delta proves it is not a time."""
    from easycat.session._streaming import consume_agent_stream

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_delta", text="Next, step 1:")
        yield AgentBridgeEvent(kind="text_delta", text=" continue drafting")
        yield AgentBridgeEvent(kind="done", text="")

    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
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
    payloads: list[TTSInput] = []
    while True:
        item = await tts_queue.get()
        if item is None:
            break
        payloads.append(item)

    assert ("Next, step 1: ", False) in built
    assert payloads[0].text == "Next, step 1: "


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


async def test_consume_agent_stream_sentinel_waits_when_the_consumer_is_alive():
    """A live consumer must still get the stop sentinel off a full queue.

    The agent-failure/timeout path cancels only the producer, so the
    ``QueueFull`` fallback's premise ("the consumer already stopped") does not
    hold there: dropping the sentinel left the consumer blocked on
    ``queue.get()`` forever and the turn never finalized (gh 1063).
    """
    from easycat.session._streaming import consume_agent_stream

    cancel_token = CancelToken()
    turn = TurnContext(turn_id="t1", cancel_token=cancel_token)
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue(maxsize=1)
    tts_queue.put_nowait(TTSInput(text="already here"))

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        cancel_token.cancel()
        yield AgentBridgeEvent(kind="done", text="final")

    producer = asyncio.create_task(
        consume_agent_stream(
            _stream,
            cancel_token=cancel_token,
            tts_queue=tts_queue,
            emit=AsyncMock(),
            prepare_tts_payload=lambda text, **_: TTSInput(text=text),
            strip_md=False,
            turn=turn,
            consumer_gone=lambda: False,
        )
    )

    # The producer is parked on the blocking put rather than dropping the
    # sentinel; draining one slot lets it through.
    await asyncio.sleep(0.05)
    assert not producer.done()
    assert await tts_queue.get() is not None

    result = await asyncio.wait_for(producer, timeout=2.0)

    assert result.interrupted is True
    assert await asyncio.wait_for(tts_queue.get(), timeout=1.0) is None


async def _run_streaming_payloads(deltas: list[str], *, strip_md: bool) -> list[tuple[str, bool]]:
    """Drive *deltas* through the consumer and return (text, is_final) payloads."""
    from easycat.session._streaming import consume_agent_stream

    async def _stream() -> AsyncIterator[AgentBridgeEvent]:
        for delta in deltas:
            yield AgentBridgeEvent(kind="text_delta", text=delta)
        yield AgentBridgeEvent(kind="done", text="")

    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()

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
        strip_md=strip_md,
        turn=turn,
    )
    assert result.error is None
    # Drain so the queue is left empty for the caller.
    while True:
        if await tts_queue.get() is None:
            break
    return built


async def test_first_payload_emits_clause_before_full_sentence():
    """The first payload of a turn ships at a clause boundary (earlier TTFA).

    A long opener clause followed by a comma is queued as its own payload
    before the sentence terminator arrives, instead of waiting for the full
    sentence.  Later sentences keep full-sentence granularity.
    """
    built = await _run_streaming_payloads(
        [
            "Let me look into that for you, and I will report back. ",
            "Here is the second sentence. ",
        ],
        strip_md=False,
    )
    streaming = [text for text, is_final in built if not is_final]
    assert streaming, "expected at least one mid-stream payload"
    # First payload is the early clause, not the whole first sentence.
    assert streaming[0] == "Let me look into that for you, "
    # Later payloads use full-sentence granularity: the remainder of the
    # first sentence and the second sentence ship after the early clause.
    later = "".join(streaming[1:])
    assert "and I will report back." in later
    assert "Here is the second sentence." in later
    # The early clause text is not duplicated in the later payloads.
    assert "Let me look into that for you," not in later


async def test_first_payload_holds_trailing_decimal_period_for_lookahead():
    built = await _run_streaming_payloads(
        ["The estimate is 3.", "5 seconds, then continue."],
        strip_md=False,
    )
    streaming = [text for text, is_final in built if not is_final]
    assert streaming, "expected at least one mid-stream payload"
    assert streaming[0] == "The estimate is 3.5 seconds, "
    assert "The estimate is 3." not in streaming


async def test_first_payload_bounds_punctuation_free_opener():
    """A run-on opener reaches TTS without waiting for final stream flush."""
    built = await _run_streaming_payloads(
        [
            "This response keeps streaming words without ",
            "reaching punctuation for quite a while",
        ],
        strip_md=False,
    )

    assert built[0] == ("This response keeps streaming words without ", False)
    assert built[1] == ("reaching punctuation for quite a while", True)


async def test_markdown_first_payload_bounds_punctuation_free_opener():
    """The same bound applies after safely stripping closed markdown."""
    built = await _run_streaming_payloads(
        [
            "**This response keeps streaming** words without ",
            "reaching punctuation for quite a while",
        ],
        strip_md=True,
    )

    assert built[0] == ("This response keeps streaming words without ", False)
    assert "**" not in "".join(text for text, _ in built)


async def test_first_payload_does_not_ship_clipped_short_opener():
    """A short opener like "Sure," is never queued as a clipped fragment.

    The first emission falls through to the sentence terminator instead, so
    no payload is just the truncated opener clause.
    """
    built = await _run_streaming_payloads(
        ["Sure, let me check that for you. ", "All set. "],
        strip_md=False,
    )
    texts = [text for text, _ in built]
    assert "Sure, " not in texts
    assert "Sure," not in texts
    streaming = [text for text, is_final in built if not is_final]
    # The whole first sentence ships as the first payload (clause guard kept
    # the clipped "Sure," from going out on its own).
    assert streaming[0] == "Sure, let me check that for you. "


async def test_later_sentences_keep_full_sentence_granularity():
    """Only the *first* payload uses clause granularity; the rest do not.

    After the first clause is emitted, a later sentence that itself contains
    an internal comma is shipped whole rather than being split at the comma.
    """
    built = await _run_streaming_payloads(
        [
            "Let me look into that for you, please. ",
            "Then, once that finishes, we proceed. ",
        ],
        strip_md=False,
    )
    streaming = [text for text, is_final in built if not is_final]
    assert streaming[0] == "Let me look into that for you, "
    # The later sentence is NOT split at its internal commas: no later payload
    # is just the comma-truncated "Then," fragment, and the sentence survives
    # whole inside the later payloads.
    later = "".join(streaming[1:])
    assert "Then, once that finishes, we proceed." in later
    assert "Then, " not in streaming


async def test_first_clause_defers_inside_open_markdown_span():
    """First-clause emission still defers while a markdown span is open.

    A comma inside an unterminated ``**bold**`` run must not trigger an
    early clause emission; the payload is held until the span closes.
    """
    built = await _run_streaming_payloads(
        ["**Let me look into that for you, ", "please** and continue. "],
        strip_md=True,
    )
    streaming = [text for text, is_final in built if not is_final]
    # Nothing ships while the bold span is open; the comma inside it does not
    # leak a partial clause out to TTS.
    for text, _ in built:
        assert "**" not in text
    # The first payload only appears once the span has closed, and it carries
    # the full bolded clause (not a comma-truncated fragment).
    assert streaming, "expected emission once the markdown span closed"
    assert streaming[0].startswith("Let me look into that for you")


async def test_markdown_buffer_commits_remainder_before_first_payload_handoff():
    """Cancellation after queueing must not leave emitted text pending."""
    from easycat.session._streaming import _SentenceStreamBuffer

    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    buffer = _SentenceStreamBuffer(
        tts_queue=tts_queue,
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=True,
    )

    task = asyncio.create_task(
        buffer.add_delta("**Let me look into that for you, please** and continue.")
    )
    first = await tts_queue.get()
    assert first is not None
    assert first.text == "Let me look into that for you, "

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await buffer.flush()
    remainder = await tts_queue.get()
    assert remainder is not None
    assert remainder.text == "please and continue."


async def test_flush_commits_text_before_first_payload_handoff():
    """Cancellation after queueing a final payload must not queue it twice."""
    from easycat.session._streaming import _SentenceStreamBuffer

    class SelfCancellingQueue(asyncio.Queue[TTSInput | None]):
        cancel_next_put = True

        async def put(self, item: TTSInput | None) -> None:
            await super().put(item)
            if item is not None and self.cancel_next_put:
                self.cancel_next_put = False
                task = asyncio.current_task()
                assert task is not None
                task.cancel()

    tts_queue = SelfCancellingQueue()
    buffer = _SentenceStreamBuffer(
        tts_queue=tts_queue,
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=False,
    )
    buffer.replace("A short final reply.")

    with pytest.raises(asyncio.CancelledError):
        await buffer.flush()

    first = await tts_queue.get()
    assert first is not None
    assert first.text == "A short final reply."
    assert await buffer.flush() is False
    assert tts_queue.empty()
