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


async def test_first_payload_holds_trailing_numeric_comma_for_lookahead():
    """A thousands separator split across deltas ships as one payload."""
    built = await _run_streaming_payloads(
        ["The total is 1,", "234 items are ready."],
        strip_md=False,
    )
    streaming = [text for text, is_final in built if not is_final]
    assert streaming, "expected at least one mid-stream payload"
    assert streaming[0] == "The total is 1,234 items are ready."
    assert "The total is 1," not in streaming


async def test_markdown_first_payload_holds_trailing_numeric_comma_for_lookahead():
    """The markdown-stripping path also holds a trailing numeric comma."""
    built = await _run_streaming_payloads(
        ["The total is 1,", "234 items are ready."],
        strip_md=True,
    )
    streaming = [text for text, is_final in built if not is_final]
    assert streaming, "expected at least one mid-stream payload"
    assert streaming[0] == "The total is 1,234 items are ready."
    assert "The total is 1," not in streaming


def test_has_trailing_numeric_separator_recognizes_trailing_comma():
    """A trailing thousands-separator comma forces a pending-buffer recheck."""
    from easycat.session._streaming import _SentenceStreamBuffer

    assert _SentenceStreamBuffer._has_trailing_numeric_separator("The total is 1,") is True
    assert _SentenceStreamBuffer._has_trailing_numeric_separator("The total is 1，") is True


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


async def test_markdown_holds_double_backtick_span_split_across_deltas():
    """A ``double-backtick`` span split across deltas is held until it closes.

    Backtick parity treated ``Use ``obj.`` as closed, so the buffer stripped
    the window early and emitted the unmatched ```` `` ```` opener (and later
    the closer) for TTS to speak.  The single backtick inside the span is
    code content and survives; the delimiter runs never reach TTS.
    """
    from easycat.strip_markdown import strip_markdown

    deltas = ["Use ``obj.", "`method()`` now."]
    built = await _run_streaming_payloads(deltas, strip_md=True)

    texts = [text for text, _ in built]
    assert all("``" not in text for text in texts)
    assert "".join(texts) == strip_markdown("".join(deltas), normalize_code_spans=True)
    assert "".join(texts) == "Use obj dot `method open paren close paren now."


async def test_markdown_double_backtick_span_across_deltas_leaves_no_backticks():
    deltas = ["First one. Use ``obj.", "method()`` now. ", "Done."]
    built = await _run_streaming_payloads(deltas, strip_md=True)

    texts = [text for text, _ in built]
    assert all("`" not in text for text in texts)
    assert "".join(texts) == "First one. Use obj dot method open paren close paren now. Done."


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        (
            ["Sure thing. It`s simple. First call ```f()`", "`` and wait. Then check. Done."],
            [
                (
                    "Sure thing. It`s simple. First call f open paren close paren and wait. "
                    "Then check. Done."
                )
            ],
        ),
        (
            ["Start here. Wrap ``a. b Call ```f()``", "` here. Done now."],
            ["Start here. Wrap ``a. b Call f open paren close paren here. Done now."],
        ),
    ],
)
async def test_markdown_holds_span_whose_closing_run_may_still_grow(
    deltas: list[str], expected: list[str]
) -> None:
    """A span closed by the buffer's last backticks is not settled yet.

    The first delta reads as one inline span from the stray tick to the end,
    and the sentence inside it was spoken (``Its simple.``).  The next delta
    grows the closing run into a fence closer, the span no longer closes, and
    the spoken text was rewritten: its tick came back and the fence markers
    were spoken.
    """
    built = await _run_streaming_payloads(deltas, strip_md=True)

    assert [text for text, _ in built] == expected


async def test_markdown_trailing_span_closer_rechecks_on_any_next_delta():
    """The hold on a trailing closing run lifts on the next delta, whatever it carries.

    The delta has no markdown closer character, which alone never rechecks an
    open window, so emission would stall until the final flush.
    """
    from easycat.session._streaming import _SentenceStreamBuffer

    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    buffer = _SentenceStreamBuffer(
        tts_queue=tts_queue,
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=True,
    )

    assert not await buffer.add_delta("Hello there, use `x`")
    assert await buffer.add_delta(" now and then go on. ")
    assert await buffer.add_delta("Next sentence here. ")
    assert not await buffer.flush()

    spoken = []
    while not tts_queue.empty():
        payload = tts_queue.get_nowait()
        assert payload is not None
        spoken.append(payload.text)
    # The first clause ships as soon as the second delta arrives.
    assert spoken == ["Hello there, ", "use x now and then go on. Next sentence here. "]


async def test_markdown_link_destination_keeps_underscores_across_deltas():
    """The streamed (``trim=False``) path speaks a link URL verbatim (gh 1209)."""
    deltas = ["Assets live at [docs](https://example.com/_next_/", "static). And *more* here."]
    built = await _run_streaming_payloads(deltas, strip_md=True)

    assert "".join(text for text, _ in built) == (
        "Assets live at docs https://example.com/_next_/static. And more here."
    )


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        # A URL that ends up in the unspoken remainder keeps its underscores.
        (["Hello, [docs](https://x/_a_)", "."], ["Hello, docs https://x/_a_."]),
        (
            ["Hi. See [docs](https://x/_a_) and", " more text. Done."],
            ["Hi. ", "See docs https://x/_a_ and more text. ", "Done."],
        ),
        # ... including a remainder spoken by the final flush.
        (["Hello. A [d](https://x/_a_) b", " c"], ["Hello. ", "A d https://x/_a_ b c"]),
        # Escaped emphasis is unescaped once, never read as italic afterwards.
        (["Hello, a \\_b\\_ x", "."], ["Hello, a _b_ x."]),
        # A heading closer arriving after its opener was stripped is dropped.
        (["# Hello, world", " #\n"], ["Hello, world"]),
        (["# Hi. #", "\nMore."], ["Hi. ", "More."]),
    ],
)
async def test_markdown_streaming_strips_raw_text_once(
    deltas: list[str], expected: list[str]
) -> None:
    """The buffer strips the raw markdown, never an already-stripped remainder.

    It used to store the stripped remainder and strip it again on the next
    recheck, so a URL's ``_a_`` became italic, ``\\_b\\_`` lost its
    underscores and a lone heading closer ``#`` was spoken.
    """
    built = await _run_streaming_payloads(deltas, strip_md=True)

    assert [text for text, _ in built] == expected


_SPLIT_SWEEP_TEXTS = [
    "Hi there. See [docs](https://x/_next_/a.b) now. Done.",
    "Hello there, a \\_b\\_ x. And \\*c\\* too. Done.",
    "# Hello, world #\nThen more text. Done.",
    "First line here. Use `snake_case` and `a*b` please. Done.",
    (
        "Intro sentence here.\n\n## Part two ##\nRead [https://x/_a_](https://x/_a_) now. "
        "**Bold** end.\n\nLast _para_ here."
    ),
    # A triple-backtick run inside a double-backtick span is span content, and a
    # fenced block after a closed span is still a block (no placeholder leaks).
    "Use ``a ```b``` c`` here. Then `x` ```py\nprint(1)\n``` ran. Done.",
    "Say `x ```y` z``` now. Then ``k`` ok. Done.",
    "Not \\`code\\` here. Use ``a ```b``` c`` now. Done.",
    # A closing run split across deltas may grow into a fence closer.
    "Sure thing, it`s simple. First call ```f()``` and wait. Then check. Done.",
    (
        '"Stop now." \U0001f600 Smile here. (Aside one.) "Quote" - not a list. '
        "1. Not a list. [Docs.] (x) and **bold** text. “Curly end.” > Not quoted. Done."
    ),
]


@pytest.mark.parametrize("text", _SPLIT_SWEEP_TEXTS)
async def test_markdown_streaming_matches_one_shot_strip_at_every_split(text: str) -> None:
    """Wherever the stream splits the text, the spoken text is ``strip_markdown`` of it."""
    from easycat.strip_markdown import strip_markdown

    expected = strip_markdown(text, normalize_code_spans=True)
    mismatches = []
    for split in range(len(text) + 1):
        built = await _run_streaming_payloads([text[:split], text[split:]], strip_md=True)
        spoken = "".join(payload for payload, _ in built)
        if spoken != expected:
            mismatches.append((split, spoken))

    assert mismatches == []


async def test_markdown_streaming_reinterpreted_spoken_prefix_is_not_repeated():
    """Later text that rewrites an already-spoken prefix never re-speaks it.

    ``!`` is spoken as the end of the first clause; the next delta turns it
    into an image opener, so stripping the whole turn no longer starts with
    the spoken text.  The fallback speaks only what is new, never the prefix
    again and never dropping the link.
    """
    built = await _run_streaming_payloads(
        ["That works really well!", "[chart](https://x/_c_) shows it."], strip_md=True
    )

    assert [text for text, _ in built] == [
        "That works really well!",
        "chart https://x/_c_ shows it.",
    ]


async def test_markdown_buffer_compacts_spoken_paragraphs():
    """Fully spoken raw paragraphs are dropped so rechecks stay cheap."""
    from easycat.session._streaming import _SentenceStreamBuffer

    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    buffer = _SentenceStreamBuffer(
        tts_queue=tts_queue,
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=True,
    )

    await buffer.add_delta("First **paragraph** is here, and done.\n\n")
    await buffer.add_delta("Second [link](https://x/_a_) here. ")
    await buffer.add_delta("Third _part_ ends")
    assert "First" not in buffer._text
    await buffer.flush()

    spoken = []
    while not tts_queue.empty():
        payload = tts_queue.get_nowait()
        assert payload is not None
        spoken.append(payload.text)
    assert spoken == [
        "First paragraph is here, ",
        "and done.\n\nSecond link https://x/_a_ here. ",
        "Third part ends",
    ]


async def test_markdown_streaming_fallback_does_not_restrip_carried_text():
    """The fallback keeps the unspoken stripped text instead of stripping it again.

    The late ``**`` pairs with a ``**`` already spoken literally, so the
    stripped turn no longer starts with the spoken text.  The rebuilt
    remainder used to be stripped a second time, turning ``/_a_`` into
    ``/a``.
    """
    built = await _run_streaming_payloads(
        ["Rate it a ** b **c. More text [d](https://x/_a_) and", " d** end."], strip_md=True
    )

    assert [text for text, _ in built] == [
        "Rate it a ** b **c. ",
        "More text d https://x/_a_ and d** end.",
    ]


@pytest.mark.parametrize("separator", [" ", "\n", "\r\n", "\r\n\r\n", "\n\n"])
async def test_markdown_buffer_stays_bounded_on_long_turns(separator: str) -> None:
    """A long turn is compacted at paragraph, line or sentence ends as it is spoken.

    Without compaction every recheck strips the whole turn from the start,
    which made long single-paragraph turns quadratic.
    """
    from easycat.session._streaming import _SentenceStreamBuffer
    from easycat.strip_markdown import strip_markdown

    text = "".join(
        f"Sentence {i} has a [link](https://x/_a_/{i}) and **bold {i}** text.{separator}"
        for i in range(125)
    )
    assert len(text) > 8000
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    buffer = _SentenceStreamBuffer(
        tts_queue=tts_queue,
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=True,
    )

    longest_raw = 0
    for start in range(0, len(text), 5):
        await buffer.add_delta(text[start : start + 5])
        longest_raw = max(longest_raw, len(buffer._text))
    await buffer.flush()

    # A few sentences' worth, not the whole turn.
    assert longest_raw < 400
    spoken = []
    while not tts_queue.empty():
        payload = tts_queue.get_nowait()
        assert payload is not None
        spoken.append(payload.text)
    assert "".join(spoken).split() == strip_markdown(text, normalize_code_spans=True).split()


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


def test_compaction_cuts_ignore_paragraph_breaks_after_bound() -> None:
    """A paragraph break only in the unspoken tail must not yield a spurious cut.

    ``rfind`` misses with -1; adding the separator length to that used to
    offer a 1- or 3-character cut, which ``_compact`` took instead of the
    sentence cut, so long turns stayed unbounded.
    """
    from easycat.session._streaming import _compaction_cuts

    raw = "First one. Second two. tail\n\nnext"
    bound = raw.index("tail")
    cuts = _compaction_cuts(raw, bound)
    assert cuts == [len("First one. Second two. ")]
    assert 1 not in cuts and 3 not in cuts

    crlf = "First one. Second two. tail\r\n\r\nnext"
    assert _compaction_cuts(crlf, crlf.index("tail")) == [len("First one. Second two. ")]
    # A break before the bound is still the preferred cut.
    with_break = "Head para.\n\nFirst one. Second two. tail"
    assert _compaction_cuts(with_break, with_break.index("tail"))[0] == len("Head para.\n\n")


_NON_LETTER_SENTENCES = {
    # Each sentence starts with a quote, an emoji or ends with a closer, so
    # no sentence end is followed by a space and a letter.
    "quoted": '"Quoted sentence {i} ends." ',
    "curly": "“Curly sentence {i} ends.” ",
    "paren-closer": "Sentence {i} ends (see the note.) ",
    "emoji-start": "\U0001f600 Emoji sentence {i} ends here. ",
    "emoji-and-paren": "\U0001f600 Emoji sentence {i} ends (see the note.) ",
    # Closers outside the old list and blank runs other than one space.
    "brace-double-space": "Brace sentence {i} ends {{see the note.}}  ",
    "cjk-space": "第{i}句话结束了。 ",
    "hash-start": "Issue {i} is fixed. ",
}


@pytest.mark.parametrize("kind", list(_NON_LETTER_SENTENCES))
async def test_markdown_buffer_stays_bounded_on_non_letter_sentences(kind: str) -> None:
    """A single-line turn whose sentences start or end in non-letters is compacted.

    A sentence cut used to need ``. `` followed by a letter, so these turns
    were never compacted: ``_text`` held the whole turn and every recheck
    stripped it again, making streaming quadratic.
    """
    from easycat.session._streaming import _SentenceStreamBuffer
    from easycat.strip_markdown import strip_markdown

    sentence = _NON_LETTER_SENTENCES[kind]
    # A line that merely starts with ``#`` (not ``#`` then a blank) is no
    # ATX heading, so its sentence ends are still cuts.
    text, count = ("#123 " if kind == "hash-start" else ""), 0
    while len(text) <= 8000:
        text += sentence.format(i=count)
        count += 1
    tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
    buffer = _SentenceStreamBuffer(
        tts_queue=tts_queue,
        prepare_tts_payload=lambda text, **_: TTSInput(text=text),
        strip_md=True,
    )

    longest_raw = 0
    for start in range(0, len(text), 5):
        await buffer.add_delta(text[start : start + 5])
        longest_raw = max(longest_raw, len(buffer._text))
    await buffer.flush()

    # A few sentences' worth, not the whole turn.  The segmenter does not
    # split at ``.) \U0001f600``, so that turn is spoken (and can only be
    # compacted) in larger pieces.
    assert longest_raw < (1000 if kind == "emoji-and-paren" else 400)
    spoken = []
    while not tts_queue.empty():
        payload = tts_queue.get_nowait()
        assert payload is not None
        spoken.append(payload.text)
    assert "".join(spoken).split() == strip_markdown(text, normalize_code_spans=True).split()


def test_compaction_cuts_allow_quotes_closers_and_symbols() -> None:
    """Sentence ends may carry closing quotes or brackets; tails may start with a quote."""
    from easycat.session._streaming import _compaction_cuts

    def sentence_cuts(raw: str, bound: int | None = None) -> list[int]:
        return _compaction_cuts(raw, len(raw) if bound is None else bound)

    # After ``." `` and before an opening quote.
    raw = 'He said "stop." "Next one" tail'
    assert sentence_cuts(raw) == [raw.index('"Next')]
    raw = "He said “stop.” “Next one” tail"
    assert sentence_cuts(raw) == [raw.index("“Next")]
    # After ``.)`` / ``.")`` and before a letter, a bracket or an emoji.
    raw = "One (aside.) Two"
    assert sentence_cuts(raw) == [raw.index("Two")]
    raw = 'One ("aside.") (Two'
    assert sentence_cuts(raw) == [raw.index("(Two")]
    raw = "One. [link](https://x) two"
    assert sentence_cuts(raw) == [raw.index("[link")]
    raw = "One. \U0001f600 two"
    assert sentence_cuts(raw) == [raw.index("\U0001f600")]
    raw = "你好。“引号”"
    assert sentence_cuts(raw) == [raw.index("“")]
    # Every closer the segmenter keeps attached, not just quotes and brackets.
    raw = "One {aside.} Two"
    assert sentence_cuts(raw) == [raw.index("Two")]
    raw = "He said 「ok.」 Two"
    assert sentence_cuts(raw) == [raw.index("Two")]
    raw = "他说「好。」再见"
    assert sentence_cuts(raw) == [raw.index("再")]
    # Any run of spaces or tabs after the sentence end, ASCII or CJK.
    raw = "One.  Two"
    assert sentence_cuts(raw) == [raw.index("Two")]
    raw = "One.\tTwo"
    assert sentence_cuts(raw) == [raw.index("Two")]
    raw = "你好。 再见"
    assert sentence_cuts(raw) == [raw.index("再")]
    raw = "One (aside.)  Two"
    assert sentence_cuts(raw) == [raw.index("Two")]


@pytest.mark.parametrize(
    "tail",
    [
        "- item",
        "* item",
        "+ item",
        "1. item",
        "1) item",
        "> quote",
        "# head",
        "```code",
        "~~~code",
        "___",
        "=== x",
        "| cell",
        "<b>x",
        "![alt](u)",
        "\\*x",
        " - indented",
        "\u00a0- nbsp",
        "\uff11. fullwidth digit",
        "\ue002# sentinel",
    ],
)
def test_compaction_cuts_reject_tails_that_can_start_a_block(tail: str) -> None:
    """A tail is stripped as if it began a line, so it must not start a block marker."""
    from easycat.session._streaming import _compaction_cuts

    for end in (". ", '." ', ".) "):
        head = f"First one{end}"
        assert _compaction_cuts(head + tail, len(head)) == []


def test_compaction_cuts_reject_paren_after_closing_label() -> None:
    """``[label.] (url)`` may still pair, so no cut is offered between them."""
    from easycat.session._streaming import _compaction_cuts

    raw = "See [label.] (https://x) now"
    assert _compaction_cuts(raw, raw.index("(")) == []
    raw = "See [Docs.]  (x) now"
    assert _compaction_cuts(raw, raw.index("(")) == []
    raw = "See [Docs.]\t (x) now"
    assert _compaction_cuts(raw, raw.index("(")) == []
    raw = "See (label.) (more) now"
    assert _compaction_cuts(raw, raw.index("(more")) == [raw.index("(more")]


def test_compaction_cuts_skip_sentence_ends_in_headings() -> None:
    from easycat.session._streaming import _compaction_cuts

    raw = '# Title. "Quoted." (Aside.) more'
    assert _compaction_cuts(raw, len(raw)) == []
    for raw in ("# Title. More", "###### H6. More", "##\tTabbed. More"):
        assert _compaction_cuts(raw, len(raw)) == [], raw
    # The line cut, then a sentence cut on the line before the heading.
    raw = 'Intro. "Lead." Next\n# Title. "Quoted." more'
    assert _compaction_cuts(raw, len(raw)) == [raw.index("\n") + 1, raw.index("Next")]


def test_compaction_cuts_allow_sentence_ends_on_non_heading_hash_lines() -> None:
    """Only ``#`` to ``######`` then a blank opens an ATX heading."""
    from easycat.session._streaming import _compaction_cuts

    for raw in ("#123 is fixed. Next", "####### seven. Next", "#hashtag day. Next"):
        assert _compaction_cuts(raw, len(raw)) == [raw.index("Next")], raw
