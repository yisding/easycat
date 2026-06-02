"""Session streaming integration tests: AgentRunner, incremental TTS, tool events, and barge-in."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from easycat import create_text_session
from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    Event,
    STTEvent,
    STTEventType,
    STTFinal,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
)
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.session.interruption import (
    TtsChunk,
    _all_tts_audio_delivered,
    _audio_bytes_acknowledged,
    _audio_bytes_likely_heard,
    _audio_bytes_likely_heard_hybrid,
    _estimate_text_spoken,
)
from easycat.session.text import (
    _cleanup_estimation_text,
    _text_for_estimation_timeline,
    has_unclosed_markdown_delimiters,
    split_at_sentence_boundaries,
)
from easycat.timeouts import AgentTimeoutError, TimeoutConfig, TTSTimeoutError
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig
from tests._bridge_helpers import _TestBridgeBase

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)

# ── Test helpers ───────────────────────────────────────────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class FakeTransport:
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self.chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

    async def clear_audio(self) -> None:
        pass


class FakeVAD:
    """Emits start on chunk 1, stop on chunk 2."""

    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 2:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class FakeSTT:
    def __init__(self, transcript: str = "hello world") -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        if self._transcript:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class FakeTTS:
    def __init__(self) -> None:
        self.synthesized_texts: list[str] = []

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(payload.text)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class FakeNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


# ── Streaming test agents ──────────────────────────────────────────


class ContextCapturingBridge(_TestBridgeBase):
    def __init__(self, response_prefix: str = "reply") -> None:
        super().__init__()
        self.response_prefix = response_prefix
        self.contexts: list[list[dict[str, str]]] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = recorder, cancel_token
        self.contexts.append(list(turn_input.context))
        yield AgentBridgeEvent(kind="done", text=f"{self.response_prefix}:{turn_input.text}")


class StreamingUpperAgent(_TestBridgeBase):
    """Streaming agent that uppercases and streams word by word."""

    async def run(self, text: str) -> str:
        return text.upper()

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        words = text.upper().split()
        for i, word in enumerate(words):
            if cancel_token and cancel_token.is_cancelled:
                break
            delta = word if i == 0 else f" {word}"
            yield AgentBridgeEvent(kind="text_delta", text=delta)
        full = " ".join(words)
        yield AgentBridgeEvent(kind="done", text=full)


class StreamingSentenceAgent(_TestBridgeBase):
    """Streams text with sentence boundaries for incremental TTS testing."""

    async def run(self, text: str) -> str:
        return "Hello world. How are you? I am fine."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        chunks = ["Hello world. ", "How are you? ", "I am fine."]
        for chunk in chunks:
            if cancel_token and cancel_token.is_cancelled:
                break
            yield AgentBridgeEvent(kind="text_delta", text=chunk)
        yield AgentBridgeEvent(
            kind="done",
            text="Hello world. How are you? I am fine.",
        )


class StreamingToolCallingAgent(_TestBridgeBase):
    """Streaming agent that calls a tool during response."""

    async def run(self, text: str) -> str:
        return "The result is 42."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name="calculator",
            call_id="call_abc",
        )
        yield AgentBridgeEvent(
            kind="tool_delta",
            call_id="call_abc",
            text="computing...",
        )
        yield AgentBridgeEvent(
            kind="tool_result",
            call_id="call_abc",
            result="42",
        )
        yield AgentBridgeEvent(
            kind="text_delta",
            text="The result is 42.",
        )
        yield AgentBridgeEvent(
            kind="done",
            text="The result is 42.",
        )


class SlowStreamingAgent(_TestBridgeBase):
    """Agent that streams slowly — useful for barge-in testing."""

    async def run(self, text: str) -> str:
        return "slow response"

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        for word in ["slow ", "response"]:
            if cancel_token and cancel_token.is_cancelled:
                break
            yield AgentBridgeEvent(kind="text_delta", text=word)
            await asyncio.sleep(0.05)
        yield AgentBridgeEvent(kind="done", text="slow response")


class FailingStreamingAgent(_TestBridgeBase):
    """Agent that raises during streaming."""

    async def run(self, text: str) -> str:
        return "won't get here"

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="start ")
        raise RuntimeError("agent failed mid-stream")


class PostDoneStreamingAgent(_TestBridgeBase):
    """Agent that incorrectly keeps emitting events after DONE."""

    async def run(self, text: str) -> str:
        return "Alpha."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="Alpha.")
        yield AgentBridgeEvent(kind="done", text="Alpha.")
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name="late_tool",
            call_id="call_late",
        )
        yield AgentBridgeEvent(kind="text_delta", text=" Beta.")


class StructuredOnlyStreamingAgent(_TestBridgeBase):
    """Agent that completes with structured output and no text."""

    async def run(self, text: str) -> str:
        return ""

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(
            kind="done",
            text="",
            structured_output={"answer": 42},
        )


class DoneOnlyStreamingAgent(_TestBridgeBase):
    """Agent that emits only a DONE event with full text (no TEXT_DELTA events)."""

    async def run(self, text: str) -> str:
        return "Hello from done-only bridge."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(
            kind="done",
            text="Hello from done-only bridge.",
        )


class TimeoutThenRecoverStreamingAgent(_TestBridgeBase):
    """Agent that times out once, then succeeds on the next turn."""

    def __init__(self) -> None:

        super().__init__()
        self.calls = 0

    async def run(self, text: str) -> str:
        return "Recovered."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.05)
            return

        yield AgentBridgeEvent(kind="text_delta", text="Recovered.")
        yield AgentBridgeEvent(kind="done", text="Recovered.")


class TimeoutThenRecoverTTS(FakeTTS):
    """TTS that times out once before producing any audio, then recovers."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.calls += 1
        self.synthesized_texts.append(payload.text)
        if self.calls == 1:
            await asyncio.sleep(0.05)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())


# ── consume_agent_stream cancellation tests ───────────────────────


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


# ── Sentence boundary helper tests ────────────────────────────────


def test_split_no_boundary():
    ready, remaining = split_at_sentence_boundaries("hello world")
    assert ready == ""
    assert remaining == "hello world"


def test_split_single_sentence():
    # Lookahead sees the trailing period as a stable boundary, so the single
    # complete sentence is emitted rather than buffered until the LLM
    # finishes.
    ready, remaining = split_at_sentence_boundaries("Hello world. ")
    assert ready == "Hello world. "
    assert remaining == ""


def test_split_multiple_sentences():
    text = "Hello. How are you? Fine"
    ready, remaining = split_at_sentence_boundaries(text)
    # Should split at last boundary (after "you? ")
    assert "Hello" in ready
    assert "How are you" in ready
    assert remaining == "Fine"


def test_split_incomplete_sentence():
    ready, remaining = split_at_sentence_boundaries("Hello world. How are")
    assert "Hello world" in ready
    assert remaining == "How are"


def test_split_abbreviation_sentence():
    text = "Dr. Smith went home. Next"
    ready, remaining = split_at_sentence_boundaries(text)
    assert ready.strip() == "Dr. Smith went home."
    assert remaining == "Next"


def test_split_trailing_abbreviation():
    ready, remaining = split_at_sentence_boundaries("Nice to meet you Mr. ")
    assert ready == ""
    assert remaining == "Nice to meet you Mr. "


def test_split_newline_sentence():
    text = "Hello world!\nHow are you"
    ready, remaining = split_at_sentence_boundaries(text)
    assert ready.strip() == "Hello world!"
    assert remaining == "How are you"


def test_split_spanish_sentence():
    text = "Hola mundo. ¿Cómo estás? Bien"
    ready, remaining = split_at_sentence_boundaries(text)
    assert "Hola mundo." in ready
    assert "¿Cómo estás?" in ready
    assert remaining == "Bien"


def test_split_chinese_sentence():
    text = "你好。今天天气不错。继续"
    ready, remaining = split_at_sentence_boundaries(text)
    assert ready == "你好。今天天气不错。"
    assert remaining == "继续"


# ── _estimate_text_spoken tests ─────────────────────────────────────


def test_text_for_estimation_timeline_encodes_ssml_breaks() -> None:
    payload = TTSInput(
        text='<speak>Hello<break time="500ms"/>world</speak>',
        format="ssml",
    )
    timeline = _text_for_estimation_timeline(payload)
    assert "Hello" in timeline and "world" in timeline
    assert timeline != "Hello world"


def test_text_for_estimation_timeline_supports_single_quoted_breaks() -> None:
    payload = TTSInput(
        text="<speak>Hello<break time='500ms'/>world</speak>",
        format="ssml",
    )

    timeline = _text_for_estimation_timeline(payload)
    assert "Hello" in timeline and "world" in timeline
    assert timeline != "Hello world"
    assert timeline.count("\ue000") == 7


def test_cleanup_estimation_text_removes_pause_markers() -> None:
    payload = TTSInput(
        text='<speak>A<break time="500ms"/>B</speak>',
        format="ssml",
    )
    cleaned = _cleanup_estimation_text(_text_for_estimation_timeline(payload))
    assert cleaned == "AB"


def test_estimate_text_spoken_with_pause_markers_advances_less_text() -> None:
    # First chunk contains synthetic pause markers, so half-byte progress should
    # include less visible text than the same bytes on plain text.
    with_pause = _estimate_text_spoken([("AB" + "\ue000" * 10 + "CD", 1000, True)], 500)
    plain = _estimate_text_spoken([("ABCD", 1000, True)], 500)
    assert len(with_pause.replace("\ue000", "")) <= len(plain)


def test_estimate_text_spoken_empty():
    assert _estimate_text_spoken([], 0) == ""
    assert _estimate_text_spoken([], 100) == ""
    assert _estimate_text_spoken([("Hello.", 320, True)], 0) == ""


def test_estimate_text_spoken_full_chunks():
    """When all audio was sent, the full text is returned."""
    chunks = [("Hello. ", 320, True), ("How are you?", 640, True)]
    assert _estimate_text_spoken(chunks, 960) == "Hello. How are you?"
    # More bytes sent than produced — still returns full text
    assert _estimate_text_spoken(chunks, 9999) == "Hello. How are you?"


def test_estimate_text_spoken_partial_first_chunk():
    """When only part of the first chunk's audio was sent, estimate proportionally."""
    chunks = [("Hello world.", 1000, True)]
    # Half the audio sent → approximately half the text
    assert _estimate_text_spoken(chunks, 500) == "Hello "


def test_estimate_text_spoken_one_and_a_half_chunks():
    """First chunk fully sent, second chunk partially sent."""
    chunks = [("First sentence. ", 400, True), ("Second sentence.", 400, True)]
    # All of first chunk (400) + half of second (200) = 600
    spoken = _estimate_text_spoken(chunks, 600)
    assert spoken.startswith("First sentence. ")
    # Second chunk has 16 chars, half → 8 chars
    assert spoken == "First sentence. Second "


def test_estimate_text_spoken_partial_chunk_trims_mid_word_boundary():
    chunks = [("Alpha bravo charlie", 1000, True)]
    # 12 chars would land in the middle of "charlie"; should trim to word boundary.
    assert _estimate_text_spoken(chunks, 700) == "Alpha bravo "


def test_estimate_text_spoken_partial_single_token_keeps_prefix():
    chunks = [("supercalifragilistic", 1000, True)]
    # No internal boundary exists; keep proportional prefix instead of dropping content.
    assert _estimate_text_spoken(chunks, 300) == "superc"


def test_estimate_text_spoken_skips_zero_audio_chunks():
    """Chunks with 0 audio bytes (cancelled before any output) are skipped."""
    chunks = [("First. ", 320, True), ("Never spoken.", 0, True), ("Third.", 320, True)]
    # 320 covers first chunk, 0-byte chunk is skipped, then 320 for third
    assert _estimate_text_spoken(chunks, 640) == "First. Third."


def test_all_tts_audio_delivered():
    chunks = [("Hello. ", 320, True), ("How are you?", 640, True)]

    assert not _all_tts_audio_delivered([], 960)
    assert not _all_tts_audio_delivered(chunks, 959)
    assert _all_tts_audio_delivered(chunks, 960)
    assert _all_tts_audio_delivered(chunks, 9999)


def test_all_tts_audio_delivered_ignores_non_positive_chunks():
    chunks = [("First.", 320, True), ("Silent", 0, True), ("Oops", -20, True)]
    assert not _all_tts_audio_delivered(chunks, 319)
    assert _all_tts_audio_delivered(chunks, 320)


def test_all_tts_audio_delivered_requires_completed_synthesis():
    chunks = [("Hello", 320, False)]
    assert not _all_tts_audio_delivered(chunks, 320)


def test_all_tts_audio_delivered_zero_audio_is_still_fully_delivered():
    chunks = [("", 0, True)]
    assert _all_tts_audio_delivered(chunks, 0)


def test_tts_chunk_named_fields_flow_through_consumers():
    # The producer (TurnRunner._process_tts) builds TtsChunk instances, so the
    # consumers must accept them by named field as well as positionally.
    chunks = [
        TtsChunk(text="Hello. ", audio_bytes=320, completed=True),
        TtsChunk(text="How are you?", audio_bytes=640, completed=True),
    ]
    assert chunks[0].text == "Hello. "
    assert chunks[0].audio_bytes == 320
    assert chunks[0].completed is True

    assert _estimate_text_spoken(chunks, 960) == "Hello. How are you?"
    assert _all_tts_audio_delivered(chunks, 960)
    assert not _all_tts_audio_delivered(chunks, 959)


def test_audio_bytes_likely_heard_without_cutoff_uses_all_bytes():
    send_log = [(1.0, 100, 10.0), (1.2, 150, 10.0), (1.4, 50, 10.0)]
    assert _audio_bytes_likely_heard(send_log, None) == 300


def test_audio_bytes_likely_heard_with_cutoff_filters_future_bytes():
    send_log = [(1.0, 100, 10.0), (1.2, 150, 10.0), (1.4, 50, 10.0)]
    assert _audio_bytes_likely_heard(send_log, 1.2) == 100


def test_audio_bytes_likely_heard_ignores_negative_sizes():
    send_log = [(1.0, 100, 10.0), (1.2, -10, 10.0), (1.3, 20, 10.0)]
    assert _audio_bytes_likely_heard(send_log, 2.0) == 120


def test_audio_bytes_likely_heard_partial_chunk_by_duration():
    send_log = [(1.0, 100, 20.0)]
    assert _audio_bytes_likely_heard(send_log, 1.005) == 24


def test_audio_bytes_likely_heard_uses_cumulative_playout_clock():
    send_log = [
        (1.0, 100, 1000.0),  # plays from 1.0 to 2.0
        (1.01, 100, 1000.0),  # queued immediately but should start at 2.0
    ]
    assert _audio_bytes_likely_heard(send_log, 1.5) == 50


def test_audio_bytes_acknowledged_filters_by_cutoff():
    playback_ack_log = [(1.0, 120), (1.2, 220), (1.4, 320)]
    assert _audio_bytes_acknowledged(playback_ack_log, 1.2) == 220
    assert _audio_bytes_acknowledged(playback_ack_log, None) == 320


def test_audio_bytes_likely_heard_hybrid_fresh_ack_caps_heuristic():
    send_log = [(1.0, 100, 1000.0)]  # 100 B/s
    playback_ack_log = [(1.7, 70)]  # fresh and close to heuristic at cutoff=1.8
    heard = _audio_bytes_likely_heard_hybrid(
        send_log,
        playback_ack_log,
        1.8,
        ack_stale_ms=500,
        ack_tail_cap_ms=500,
    )
    assert heard == 70


def test_audio_bytes_likely_heard_hybrid_stale_ack_allows_bounded_tail():
    send_log = [(1.0, 100, 1000.0)]  # 100 B/s
    playback_ack_log = [(1.0, 20)]  # stale at cutoff=1.8 (800ms old)
    heard = _audio_bytes_likely_heard_hybrid(
        send_log,
        playback_ack_log,
        1.8,
        ack_stale_ms=500,
        ack_tail_cap_ms=500,  # allow +50 bytes
    )
    assert heard == 70


def test_audio_bytes_likely_heard_hybrid_tail_cap_zero_keeps_ack_cap():
    send_log = [(1.0, 100, 1000.0)]  # 100 B/s
    playback_ack_log = [(1.0, 20)]
    heard = _audio_bytes_likely_heard_hybrid(
        send_log,
        playback_ack_log,
        1.8,
        ack_stale_ms=500,
        ack_tail_cap_ms=0,
    )
    assert heard == 20


def test_markdown_unclosed_single_italic_asterisk():
    assert has_unclosed_markdown_delimiters("*First sentence. Second sentence")
    assert not has_unclosed_markdown_delimiters("*First sentence. Second sentence*")


def test_markdown_unclosed_single_italic_underscore():
    assert has_unclosed_markdown_delimiters("_First sentence. Second sentence")
    assert not has_unclosed_markdown_delimiters("_First sentence. Second sentence_")
    assert not has_unclosed_markdown_delimiters("Use my_variable_name here.")


def test_markdown_unclosed_link_or_image_delimiters():
    assert has_unclosed_markdown_delimiters("See [OpenAI")
    assert has_unclosed_markdown_delimiters("See [OpenAI]")
    assert has_unclosed_markdown_delimiters("See [OpenAI](https://openai.com/docs")
    assert has_unclosed_markdown_delimiters("See ![diagram](https://img.example.com/plot")
    assert not has_unclosed_markdown_delimiters("See [OpenAI](https://openai.com/docs).")
    assert not has_unclosed_markdown_delimiters(
        "See [Function](https://en.wikipedia.org/wiki/Function_(mathematics))."
    )


def test_markdown_delimiters_inside_inline_code_do_not_block_streaming():
    assert not has_unclosed_markdown_delimiters("Literal `**` should not block.")
    assert not has_unclosed_markdown_delimiters("Literal `__` should not block.")
    assert not has_unclosed_markdown_delimiters("Literal `~~` should not block.")
    assert has_unclosed_markdown_delimiters("Literal `**` and **still open")


# ── Session with basic agent (backward compatibility) ──────────────


@pytest.mark.asyncio
async def test_session_basic_agent_still_works():
    """Existing basic agent path should work unchanged."""

    class BasicAgent:
        async def run(self, text: str) -> str:
            return text.upper()

    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=BasicAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    events_received: list[Event] = []
    for et in [AgentDelta, AgentFinal, BotStartedSpeaking, BotStoppedSpeaking]:
        session.event_bus.subscribe(et, lambda e: events_received.append(e))

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    type_names = [type(e).__name__ for e in events_received]
    assert "AgentDelta" in type_names
    assert "AgentFinal" in type_names
    assert "BotStartedSpeaking" in type_names
    assert "BotStoppedSpeaking" in type_names

    agent_finals = [e for e in events_received if isinstance(e, AgentFinal)]
    assert agent_finals[0].text == "HELLO"


@pytest.mark.asyncio
async def test_session_basic_agent_error_emits_event():
    """Agent exceptions should emit Error events and not crash the session."""

    class BrokenAgent:
        async def run(self, text: str) -> str:
            raise ValueError("oops")

    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=BrokenAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    assert len(errors) >= 1
    assert errors[0].stage == ErrorStage.AGENT
    assert isinstance(errors[0].exception, ValueError)


# ── Session with streaming agent ───────────────────────────────────


@pytest.mark.asyncio
async def test_session_streaming_agent_emits_deltas():
    """Streaming agent should produce AgentDelta events per delta."""
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello world"),
        agent=StreamingUpperAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    deltas: list[AgentDelta] = []
    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentDelta, lambda e: deltas.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # Should have multiple deltas (one per word)
    assert len(deltas) >= 2
    # Deltas should be uppercase fragments
    combined = "".join(d.text for d in deltas)
    assert combined == "HELLO WORLD"
    # Should have exactly one final
    assert len(finals) == 1
    assert finals[0].text == "HELLO WORLD"


@pytest.mark.asyncio
async def test_session_streaming_agent_tts_receives_text():
    """TTS should receive text from the streaming agent."""
    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello world"),
        agent=StreamingUpperAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # TTS should have been called with the agent output text
    assert len(tts.synthesized_texts) > 0
    combined = " ".join(tts.synthesized_texts)
    assert "HELLO" in combined
    assert "WORLD" in combined


@pytest.mark.asyncio
async def test_session_streaming_incremental_tts():
    """Streaming agent with sentence boundaries should trigger incremental TTS."""
    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=StreamingSentenceAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # TTS should have been called multiple times (incremental sentences)
    assert len(tts.synthesized_texts) >= 2, (
        f"Expected at least 2 TTS calls for sentence-level synthesis, "
        f"got {len(tts.synthesized_texts)}: {tts.synthesized_texts}"
    )


@pytest.mark.asyncio
async def test_session_streaming_tool_events():
    """Tool events from streaming agent should be emitted on the event bus."""
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="calculate"),
        agent=StreamingToolCallingAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    tool_started: list[ToolCallStarted] = []
    tool_deltas: list[ToolCallDelta] = []
    tool_results: list[ToolCallResult] = []
    session.event_bus.subscribe(ToolCallStarted, lambda e: tool_started.append(e))
    session.event_bus.subscribe(ToolCallDelta, lambda e: tool_deltas.append(e))
    session.event_bus.subscribe(ToolCallResult, lambda e: tool_results.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert len(tool_started) == 1
    assert tool_started[0].tool_name == "calculator"
    assert tool_started[0].call_id == "call_abc"

    assert len(tool_deltas) == 1
    assert tool_deltas[0].delta == "computing..."

    assert len(tool_results) == 1
    assert tool_results[0].result == "42"


@pytest.mark.asyncio
async def test_session_streaming_agent_error_emits_event():
    """Streaming agent exceptions should emit Error events."""
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=FailingStreamingAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert len(errors) >= 1
    assert errors[0].stage == ErrorStage.AGENT
    assert isinstance(errors[0].exception, RuntimeError)


@pytest.mark.asyncio
async def test_streaming_done_terminates_session_consumption() -> None:
    """Late events after DONE should not leak into the session pipeline."""

    runner = AgentRunner(PostDoneStreamingAgent())
    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=runner,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    deltas: list[AgentDelta] = []
    finals: list[AgentFinal] = []
    tool_started: list[ToolCallStarted] = []
    session.event_bus.subscribe(AgentDelta, lambda e: deltas.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))
    session.event_bus.subscribe(ToolCallStarted, lambda e: tool_started.append(e))

    session._turn = TurnContext("test-turn", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert [event.text for event in deltas] == ["Alpha."]
    assert len(finals) == 1
    assert finals[0].text == "Alpha."
    assert tool_started == []
    assert tts.synthesized_texts == ["Alpha."]
    assert runner.history[-1]["role"] == "assistant"
    assert runner.history[-1]["content"] == "Alpha."


@pytest.mark.asyncio
async def test_streaming_structured_only_done_emits_final_without_tts() -> None:
    """Structured-only DONE events should still surface AgentFinal."""

    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=StructuredOnlyStreamingAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    session._turn = TurnContext("test-turn", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert len(finals) == 1
    assert finals[0].text == ""
    assert finals[0].structured_output == {"answer": 42}
    assert tts.synthesized_texts == []


@pytest.mark.asyncio
async def test_streaming_done_only_bridge_synthesizes_audio() -> None:
    """A bridge that emits only a DONE event with text should still produce TTS audio."""

    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=DoneOnlyStreamingAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    session._turn = TurnContext("test-turn", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert len(finals) == 1
    assert finals[0].text == "Hello from done-only bridge."
    assert tts.synthesized_texts == ["Hello from done-only bridge."]


@pytest.mark.asyncio
async def test_streaming_agent_timeout_does_not_poison_next_turn() -> None:
    """A timed-out streaming turn should not prevent the next turn from succeeding."""

    agent = TimeoutThenRecoverStreamingAgent()
    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=agent,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            timeout_config=TimeoutConfig(agent_timeout=0.01),
        )
    )

    errors: list[Error] = []
    finals: list[AgentFinal] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    session._turn = TurnContext("turn-1", CancelToken())
    await session._turn_runner.run_streaming_agent("first", token=None)
    session._turn = TurnContext("turn-2", CancelToken())
    await session._turn_runner.run_streaming_agent("second", token=None)

    assert len(errors) == 1
    assert errors[0].stage == ErrorStage.AGENT
    assert isinstance(errors[0].exception, AgentTimeoutError)
    assert len(finals) == 1
    assert finals[0].text == "Recovered."
    assert tts.synthesized_texts == ["Recovered."]


@pytest.mark.asyncio
async def test_streaming_tts_timeout_does_not_poison_next_turn() -> None:
    """A first-byte TTS timeout should still allow a later turn to synthesize."""

    tts = TimeoutThenRecoverTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=FastDoneAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            timeout_config=TimeoutConfig(tts_first_byte_timeout=0.01),
        )
    )

    errors: list[Error] = []
    finals: list[AgentFinal] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    session._turn = TurnContext("turn-1", CancelToken())
    await session._turn_runner.run_streaming_agent("first", token=None)
    session._turn = TurnContext("turn-2", CancelToken())
    await session._turn_runner.run_streaming_agent("second", token=None)

    assert len(errors) == 1
    assert errors[0].stage == ErrorStage.TTS
    assert isinstance(errors[0].exception, TTSTimeoutError)
    assert [event.text for event in finals] == ["Quick reply.", "Quick reply."]
    assert tts.synthesized_texts == ["Quick reply.", "Quick reply."]


@pytest.mark.asyncio
async def test_session_streaming_barge_in_cancellation():
    """Barge-in during streaming should stop agent output via cancel token."""

    class InterruptibleAgent(_TestBridgeBase):
        """Agent that checks cancel token and stops."""

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            for word in ["Hello ", "world. ", "This ", "is ", "a ", "long ", "response."]:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=word)
                await asyncio.sleep(0.03)
            yield AgentBridgeEvent(kind="done", text="")

    # Custom VAD that triggers barge-in mid-stream
    class BargeInVAD:
        def __init__(self) -> None:

            super().__init__()
            self._n = 0

        async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
            self._n += 1
            if self._n == 1:
                yield VADStartSpeaking()
            elif self._n == 2:
                yield VADStopSpeaking()
            # Chunk 3+: more audio arrives during agent streaming
            # (In a real scenario, barge-in would trigger cancel_turn)

        def configure(self, **kwargs: object) -> None:
            pass

    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    agent = InterruptibleAgent()
    config = SessionConfig(
        transport=transport,
        vad=BargeInVAD(),
        stt=FakeSTT(transcript="test"),
        agent=agent,
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    deltas: list[AgentDelta] = []
    session.event_bus.subscribe(AgentDelta, lambda e: deltas.append(e))

    await session.start()
    await asyncio.sleep(0.1)

    # Simulate barge-in by cancelling the token
    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.2)
    await session.stop()

    # Agent should not have produced all 7 deltas
    assert len(deltas) < 7


# ── Session reset_state clears agent history ───────────────────────


@pytest.mark.asyncio
async def test_session_reset_clears_agent_history():
    runner = AgentRunner(StreamingUpperAgent())
    await runner.run("hello")
    assert len(runner.history) == 2

    config = SessionConfig(
        agent=runner,
        transport=FakeTransport(),
        vad=FakeVAD(),
        stt=FakeSTT(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        enable_noise_reduction=False,
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)
    await session.reset_state()

    assert runner.history == []


@pytest.mark.asyncio
async def test_text_session_reset_clears_raw_bridge_shadow_history():
    bridge = ContextCapturingBridge()
    session = create_text_session(agent=bridge, wrap_agent=False)
    try:
        assert await session.send_text("SECRET-PII") == "reply:SECRET-PII"
        assert bridge.contexts[-1] == []

        await session.reset_state()

        assert await session.send_text("fresh") == "reply:fresh"
        assert bridge.contexts[-1] == []
    finally:
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_text_session_agent_swap_clears_raw_bridge_shadow_history():
    first = ContextCapturingBridge("first")
    session = create_text_session(agent=first, wrap_agent=False)
    try:
        assert await session.send_text("SECRET-PII") == "first:SECRET-PII"

        second = ContextCapturingBridge("second")
        session.agent = second

        assert await session.send_text("fresh") == "second:fresh"
        assert second.contexts[-1] == []
    finally:
        await session.stop(force=True)


# ── Session with AgentRunner (full integration) ───────────────────


@pytest.mark.asyncio
async def test_session_with_agent_runner_streaming():
    """Full pipeline with AgentRunner wrapping a streaming agent."""

    class MyStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return f"Reply: {text}"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            response = f"Reply: {text}"
            yield AgentBridgeEvent(kind="text_delta", text=response)
            yield AgentBridgeEvent(kind="done", text=response)

    runner = AgentRunner(MyStreamingAgent())
    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=runner,
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    timeline: list[Event] = []
    for et in [
        STTFinal,
        AgentDelta,
        AgentFinal,
        BotStartedSpeaking,
        TTSAudio,
        BotStoppedSpeaking,
    ]:
        session.event_bus.subscribe(et, lambda e: timeline.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    type_names = [type(e).__name__ for e in timeline]
    assert "STTFinal" in type_names
    assert "AgentDelta" in type_names
    assert "AgentFinal" in type_names
    assert "BotStartedSpeaking" in type_names
    assert "TTSAudio" in type_names
    assert "BotStoppedSpeaking" in type_names

    # Verify AgentRunner recorded history
    assert len(runner.history) == 2
    assert runner.history[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_streaming_flushes_final_buffer_to_tts():
    class BufferingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Hello world"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Hello world")
            yield AgentBridgeEvent(kind="done", text="Hello world")

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=BufferingAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    assert tts.synthesized_texts == ["Hello world"]


@pytest.mark.asyncio
async def test_streaming_delta_only_still_emits_final_and_flushes_tts():
    class DeltaOnlyAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Hello world"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Hello world")

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=DeltaOnlyAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    assert tts.synthesized_texts == ["Hello world"]
    assert len(finals) == 1
    assert finals[0].text == "Hello world"


@pytest.mark.asyncio
async def test_streaming_done_flushes_tts_before_stream_cleanup_finishes():
    class DelayedAfterDoneAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Hello world"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Hello world")
            yield AgentBridgeEvent(kind="done", text="Hello world")
            await asyncio.sleep(0.3)

    class ObservableFakeTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.started.set()
            async for event in super().synthesize(payload):
                yield event

    tts = ObservableFakeTTS()
    session = Session(
        SessionConfig(
            agent=DelayedAfterDoneAgent(),
            tts=tts,
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    session._turn = TurnContext("test-turn", CancelToken())
    task = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=None))
    await asyncio.wait_for(tts.started.wait(), timeout=0.2)
    await task

    assert tts.synthesized_texts == ["Hello world"]


@pytest.mark.asyncio
async def test_full_streaming_turn_event_order():
    """Verify the complete event ordering in a streaming turn."""
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hi"),
        agent=StreamingUpperAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    timeline: list[Event] = []
    for et in [
        TurnStarted,
        TurnEnded,
        STTFinal,
        AgentDelta,
        AgentFinal,
        BotStartedSpeaking,
        TTSAudio,
        BotStoppedSpeaking,
    ]:
        session.event_bus.subscribe(et, lambda e: timeline.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    type_names = [type(e).__name__ for e in timeline]

    # Verify ordering: TurnStarted < TurnEnded < STTFinal < AgentDelta < AgentFinal
    assert "TurnStarted" in type_names
    assert "TurnEnded" in type_names
    assert "STTFinal" in type_names
    assert "AgentFinal" in type_names
    assert "BotStartedSpeaking" in type_names
    assert "BotStoppedSpeaking" in type_names

    ts_idx = type_names.index("TurnStarted")
    te_idx = type_names.index("TurnEnded")
    sf_idx = type_names.index("STTFinal")
    af_idx = type_names.index("AgentFinal")
    bs_idx = type_names.index("BotStartedSpeaking")
    be_idx = type_names.index("BotStoppedSpeaking")

    assert ts_idx < te_idx
    assert te_idx < sf_idx
    assert sf_idx < af_idx
    assert af_idx < bs_idx or bs_idx < af_idx  # TTS may start before AgentFinal
    assert bs_idx < be_idx


# ── Barge-in with tool calls ──────────────────────────────────────


class SlowToolCallingAgent(_TestBridgeBase):
    """Streaming agent with a tool call that takes time, allowing
    cancellation to arrive mid-tool."""

    def __init__(self) -> None:

        super().__init__()
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    async def run(self, text: str) -> str:
        return "The answer is 42."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        # Text before tool — deliberately unterminated so lookahead keeps
        # it buffered instead of flushing it to TTS before the tool runs.
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="Let me look")
        # Tool lifecycle
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name="database_update",
            call_id="call_xyz",
        )
        yield AgentBridgeEvent(
            kind="tool_delta",
            call_id="call_xyz",
            text="updating...",
        )
        # Simulate slow tool — cancellation arrives here
        await asyncio.sleep(0.1)
        yield AgentBridgeEvent(
            kind="tool_result",
            call_id="call_xyz",
            result="row updated",
        )
        # Text after tool (should be skipped on barge-in)
        yield AgentBridgeEvent(kind="text_delta", text="Done!")
        yield AgentBridgeEvent(kind="done", text="Done!")

    def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
        self.interruption_notified = True
        self.interruption_text_spoken = text_spoken
        self.interruption_mode = mode


class FastDoneAgent(_TestBridgeBase):
    """Agent that completes quickly and supports interruption notifications."""

    def __init__(self) -> None:

        super().__init__()
        self.finished = asyncio.Event()
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    async def run(self, text: str) -> str:
        return "Quick reply."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="Quick reply.")
        yield AgentBridgeEvent(kind="done", text="Quick reply.")
        self.finished.set()

    def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
        self.interruption_notified = True
        self.interruption_text_spoken = text_spoken
        self.interruption_mode = mode


class SlowStartTTS(FakeTTS):
    """TTS that starts playback, then waits before yielding audio."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(payload.text)
        self.started.set()
        await asyncio.sleep(0.1)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())


@pytest.mark.asyncio
async def test_session_barge_in_completes_tool_calls():
    """Barge-in during a tool call should let the tool finish and emit its result."""
    agent = SlowToolCallingAgent()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="do the thing"),
        agent=agent,
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    tool_started: list[ToolCallStarted] = []
    tool_results: list[ToolCallResult] = []
    session.event_bus.subscribe(ToolCallStarted, lambda e: tool_started.append(e))
    session.event_bus.subscribe(ToolCallResult, lambda e: tool_results.append(e))

    await session.start()
    await asyncio.sleep(0.1)

    # Simulate barge-in while tool call is in-flight
    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.3)
    await session.stop()

    # Tool call started AND result should both have been emitted
    assert len(tool_started) == 1
    assert tool_started[0].tool_name == "database_update"
    assert len(tool_results) == 1
    assert tool_results[0].result == "row updated"


@pytest.mark.asyncio
async def test_session_barge_in_calls_notify_interruption():
    """After barge-in the session should call notify_interruption on the agent.

    The SlowToolCallingAgent emits a single sentence fragment before
    starting a tool call.  Because the fragment never crosses a sentence
    boundary it stays in the text buffer and is never sent to TTS, so the
    audio-based estimation correctly reports "" — the user never heard it.
    """
    agent = SlowToolCallingAgent()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="do the thing"),
        agent=agent,
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.1)

    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.3)
    await session.stop()

    assert agent.interruption_notified
    # Text never reached TTS (single sentence fragment still in buffer),
    # so audio-based estimation yields empty string.
    assert agent.interruption_text_spoken == ""
    # Default mode is "truncate"
    assert agent.interruption_mode == "truncate"


@pytest.mark.asyncio
async def test_session_barge_in_after_agent_done_calls_notify_interruption():
    """Cancellation during TTS playback should still notify interruption."""
    agent = FastDoneAgent()
    tts = SlowStartTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="test"),
            agent=agent,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )
    token = CancelToken()
    session._turn = TurnContext("test-turn", token)

    async def _cancel_during_tts_playback() -> None:
        await agent.finished.wait()
        await tts.started.wait()
        token.cancel()

    cancel_task = asyncio.create_task(_cancel_during_tts_playback())
    await session._turn_runner.run_streaming_agent("test", token=token)
    await cancel_task

    assert agent.interruption_notified
    assert agent.interruption_text_spoken == ""
    assert agent.interruption_mode == "truncate"


@pytest.mark.asyncio
async def test_session_barge_in_writes_interruption_journal_record():
    agent = FastDoneAgent()
    tts = SlowStartTTS()
    journal = InMemoryRingBuffer()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="test"),
            agent=agent,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            journal=journal,
        )
    )
    token = CancelToken()
    session._turn = TurnContext("turn-interruption-journal", token)

    async def _cancel_during_tts_playback() -> None:
        await agent.finished.wait()
        await tts.started.wait()
        token.cancel()

    cancel_task = asyncio.create_task(_cancel_during_tts_playback())
    await session._turn_runner.run_streaming_agent("test", token=token)
    await cancel_task

    records = [
        record for record in journal.read() if record.name == "assistant_interruption_notified"
    ]
    assert len(records) == 1
    assert records[0].turn_id == "turn-interruption-journal"
    assert records[0].data == {
        "source": "streaming_turn",
        "mode": "truncate",
        "text_spoken": "",
        "notified": True,
    }


@pytest.mark.asyncio
async def test_session_barge_in_message_mode():
    """When interruption_mode='message', notify_interruption receives that mode."""
    agent = SlowToolCallingAgent()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="do the thing"),
        agent=agent,
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        interruption_mode="message",
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.1)

    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.3)
    await session.stop()

    assert agent.interruption_notified
    assert agent.interruption_mode == "message"


@pytest.mark.asyncio
async def test_session_barge_in_with_agent_runner_adds_single_interruption_note():
    """Wrapped AgentRunner should record exactly one interruption note."""
    runner = AgentRunner(SlowToolCallingAgent())
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="do the thing"),
        agent=runner,
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        interruption_mode="message",
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.1)

    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.3)
    await session.stop()

    interruption_notes = [
        entry
        for entry in runner.history
        if entry["role"] == "system" and "interrupted" in entry["content"].lower()
    ]
    assert len(interruption_notes) == 1


@pytest.mark.asyncio
async def test_session_barge_in_without_tool_calls_stops_immediately():
    """Barge-in with no tool calls in flight should stop the stream quickly."""
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=SlowStreamingAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    deltas: list[AgentDelta] = []
    session.event_bus.subscribe(AgentDelta, lambda e: deltas.append(e))

    await session.start()
    await asyncio.sleep(0.1)

    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.2)
    await session.stop()

    # Stream should not have produced extra text beyond the agent's
    # natural output.  The SlowStreamingAgent only yields 2 words, so
    # it may finish before cancel arrives — what matters is it did not
    # hang waiting for more output.
    assert len(deltas) <= 2


@pytest.mark.asyncio
async def test_session_barge_in_during_tts_playback():
    """Cancellation after agent stream completes but during TTS playback
    should still trigger notify_interruption (cancelled_during_playback path)."""

    class FastAgent(_TestBridgeBase):
        """Completes instantly with a full sentence."""

        interruption_notified = False
        interruption_text_spoken = ""
        interruption_mode = ""

        async def run(self, text: str) -> str:
            return "Hello world."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Hello world.")
            yield AgentBridgeEvent(kind="done", text="Hello world.")

        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            self.interruption_notified = True
            self.interruption_text_spoken = text_spoken
            self.interruption_mode = mode

    class SlowTTS:
        """TTS that yields audio slowly so cancel can arrive mid-playback."""

        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.started.set()
            for _ in range(5):
                await asyncio.sleep(0.1)
                yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

        async def stop(self) -> None:
            pass

        async def cancel(self) -> None:
            pass

    agent = FastAgent()
    tts = SlowTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=agent,
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    # Wait for TTS playback to start, then cancel mid-playback.
    await tts.started.wait()
    await asyncio.sleep(0.15)

    # Cancel during TTS playback (agent stream already done)
    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.3)
    await session.stop()

    assert agent.interruption_notified
    assert agent.interruption_mode == "truncate"


@pytest.mark.asyncio
async def test_session_barge_in_records_dequeued_unsynthesized_text_as_incomplete():
    """Cancellation after dequeue should still count unsynthesized text as incomplete."""

    token = CancelToken()

    class TwoSentenceAgent(_TestBridgeBase):
        interruption_notified = False

        async def run(self, text: str) -> str:
            return "First sentence. Second sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="First sentence. ")
            yield AgentBridgeEvent(kind="text_delta", text="Second sentence.")
            yield AgentBridgeEvent(
                kind="done",
                text="First sentence. Second sentence.",
            )

        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            self.interruption_notified = True

    class CancelAfterFirstChunkTTS:
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.calls += 1
            if self.calls == 1:
                yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
                await asyncio.sleep(0.2)
                token.cancel()
                return
            pytest.fail("Second TTS chunk should not be synthesized after cancellation")

        async def stop(self) -> None:
            pass

        async def cancel(self) -> None:
            pass

    agent = TwoSentenceAgent()
    tts = CancelAfterFirstChunkTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=agent,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    await session.start()
    try:
        session._turn = TurnContext("test-turn", token)
        await session._turn_runner.run_streaming_agent("hello", token=token)
    finally:
        await session.stop()

    assert agent.interruption_notified


@pytest.mark.asyncio
async def test_session_barge_in_records_queued_unsynthesized_text_as_incomplete():
    """Cancellation should also account for chunks still left in the TTS queue."""

    token = CancelToken()

    class ThreeSentenceAgent(_TestBridgeBase):
        interruption_notified = False

        async def run(self, text: str) -> str:
            return "First sentence. Second sentence. Third sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(
                kind="text_delta",
                text="First sentence. Second sentence. ",
            )
            yield AgentBridgeEvent(kind="text_delta", text="Third sentence.")
            yield AgentBridgeEvent(
                kind="done",
                text="First sentence. Second sentence. Third sentence.",
            )

        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            self.interruption_notified = True

    class CancelAfterFirstChunkTTS:
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.calls += 1
            if self.calls == 1:
                yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
                token.cancel()
                return
            pytest.fail("No additional chunks should be synthesized after cancellation")

        async def stop(self) -> None:
            pass

        async def cancel(self) -> None:
            pass

    agent = ThreeSentenceAgent()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=agent,
            tts=CancelAfterFirstChunkTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    await session.start()
    try:
        session._turn = TurnContext("test-turn", token)
        await session._turn_runner.run_streaming_agent("hello", token=token)
    finally:
        await session.stop()

    assert agent.interruption_notified


@pytest.mark.asyncio
async def test_session_barge_in_drain_records_timeline_strings(monkeypatch: pytest.MonkeyPatch):
    """Queued leftovers should be normalized to timeline strings before interruption math."""

    token = CancelToken()
    captured = {"called": False}

    class TwoSentenceAgent(_TestBridgeBase):
        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            pass

        async def run(self, text: str) -> str:
            return "First sentence. Second sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="First sentence. ")
            yield AgentBridgeEvent(kind="text_delta", text="Second sentence.")
            yield AgentBridgeEvent(
                kind="done",
                text="First sentence. Second sentence.",
            )

    class ExplodingTTS:
        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            token.cancel()
            raise RuntimeError("boom")
            yield  # pragma: no cover

        async def stop(self) -> None:
            pass

        async def cancel(self) -> None:
            pass

    def _fake_heard_bytes(*args: object, **kwargs: object) -> int:
        return 1

    def _assert_string_chunks(
        chunks: list[tuple[str, int, bool]],
        audio_bytes_sent: int,
    ) -> str:
        captured["called"] = True
        assert audio_bytes_sent == 1
        assert all(isinstance(text, str) for text, _, _ in chunks)
        return ""

    monkeypatch.setattr(
        "easycat.session.interruption._audio_bytes_likely_heard_hybrid", _fake_heard_bytes
    )
    monkeypatch.setattr(
        "easycat.session.interruption._estimate_text_spoken", _assert_string_chunks
    )

    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=TwoSentenceAgent(),
            tts=ExplodingTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    await session.start()
    try:
        session._turn = TurnContext("test-turn", token)
        await session._turn_runner.run_streaming_agent("hello", token=token)
    finally:
        await session.stop()

    assert captured["called"]


@pytest.mark.asyncio
async def test_session_barge_in_after_full_playback_does_not_notify_interruption():
    """If all synthesized audio is already delivered, interruption should not rewrite history."""

    class FastAgent(_TestBridgeBase):
        interruption_notified = False

        async def run(self, text: str) -> str:
            return "Hello world."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Hello world.")
            yield AgentBridgeEvent(kind="done", text="Hello world.")

        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            self.interruption_notified = True

    class OneShotTTS:
        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

        async def stop(self) -> None:
            pass

        async def cancel(self) -> None:
            pass

    agent = FastAgent()
    session = Session(
        SessionConfig(
            transport=FakeTransport(chunks=[_chunk(), _chunk()]),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=agent,
            tts=OneShotTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    await session.start()
    await asyncio.sleep(0.25)

    # Cancel after playback has already completed; this should not be treated
    # as an interruption that mutates agent history.
    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.1)
    await session.stop()

    assert not agent.interruption_notified


@pytest.mark.asyncio
async def test_session_barge_in_after_full_playback_keeps_agent_runner_history():
    """Late cancel after full playback should not truncate AgentRunner history."""

    class FastAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Hello world."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Hello world.")
            yield AgentBridgeEvent(kind="done", text="Hello world.")

    class OneShotTTS:
        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

        async def stop(self) -> None:
            pass

        async def cancel(self) -> None:
            pass

    runner = AgentRunner(FastAgent())
    session = Session(
        SessionConfig(
            transport=FakeTransport(chunks=[_chunk(), _chunk()]),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=runner,
            tts=OneShotTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    await session.start()
    await asyncio.sleep(0.25)

    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.1)
    await session.stop()

    assert runner.history[-1] == {"role": "assistant", "content": "Hello world."}


@pytest.mark.asyncio
async def test_session_barge_in_after_full_multichunk_playback_keeps_history():
    """Late cancel after full multi-chunk playback should not truncate history."""

    class MultiSentenceAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "First sentence. Second sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="First sentence. ")
            yield AgentBridgeEvent(kind="text_delta", text="Second sentence.")
            yield AgentBridgeEvent(
                kind="done",
                text="First sentence. Second sentence.",
            )

    runner = AgentRunner(MultiSentenceAgent())
    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(chunks=[_chunk(), _chunk()]),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=runner,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    await session.start()
    await asyncio.sleep(0.3)

    if session.cancel_token:
        session.cancel_token.cancel()

    await asyncio.sleep(0.1)
    await session.stop()

    assert tts.synthesized_texts == ["First sentence. ", "Second sentence."]
    assert runner.history[-1] == {
        "role": "assistant",
        "content": "First sentence. Second sentence.",
    }


# ── Streaming with strip_markdown ──────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_strip_markdown_tts_receives_clean_text():
    """Markdown should be stripped from TTS chunks and AgentFinal when enabled."""

    class MarkdownStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Go to **Settings** first. Then click *Security* next."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            # Stream the markdown response in realistic token-size deltas
            chunks = [
                "Go to **Settings",
                "** first. ",
                "Then click *Security",
                "* next.",
            ]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            full = "Go to **Settings** first. Then click *Security* next."
            yield AgentBridgeEvent(kind="done", text=full)

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="help"),
        agent=MarkdownStreamingAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # TTS should have received text with no markdown artefacts
    joined_tts = " ".join(tts.synthesized_texts)
    assert "**" not in joined_tts
    assert "*Security*" not in joined_tts
    assert "Settings" in joined_tts
    assert "Security" in joined_tts

    # AgentFinal event should also carry stripped text
    assert len(finals) == 1
    assert "**" not in finals[0].text
    assert "Settings" in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_writes_journal_record():
    class MarkdownStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Go to **Settings** first."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Go to **Settings")
            yield AgentBridgeEvent(kind="text_delta", text="** first.")
            yield AgentBridgeEvent(
                kind="done",
                text="Go to **Settings** first.",
            )

    journal = InMemoryRingBuffer()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="help"),
            agent=MarkdownStreamingAgent(),
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
            enable_noise_reduction=False,
            turn_manager_config=_FAST_TURN,
            strip_markdown=True,
            journal=journal,
        )
    )
    session._turn = TurnContext("turn-stream-markdown", CancelToken())
    session._drain_session_actions = AsyncMock(return_value=False)

    await session._turn_runner.run_streaming_agent("help", token=None)

    records = [record for record in journal.read() if record.name == "markdown_stripped"]
    assert len(records) == 1
    assert records[0].turn_id == "turn-stream-markdown"
    assert records[0].data == {
        "phase": "streaming_final",
        "changed": True,
        "original_text": "Go to **Settings** first.",
        "stripped_text": "Go to Settings first.",
    }
    # The last-assistant rewrite is routed through AgentStage so the
    # framework-state mutation lands on the journal recording boundary
    # alongside the streamed text (xc-architecture consistency fix).
    rewrites = [
        record for record in journal.read() if record.name == "replace_last_assistant_text"
    ]
    assert len(rewrites) == 1
    assert rewrites[0].turn_id == "turn-stream-markdown"
    assert rewrites[0].data["stage"] == "agent"
    assert rewrites[0].data["text"] == "Go to Settings first."


@pytest.mark.asyncio
async def test_streaming_strip_markdown_normalizes_short_code_spans_for_tts():
    class CodeStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Call `__init__`. Then run `print()`."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["Call `__init__`. ", "Then run `print()`."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="Call `__init__`. Then run `print()`.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="help"),
        agent=CodeStreamingAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "dunder init" in joined_tts
    assert "print open paren close paren" in joined_tts
    assert "`" not in joined_tts

    assert len(finals) == 1
    assert "dunder init" in finals[0].text
    assert "print open paren close paren" in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_preserves_chunk_boundary_spaces():
    """Chunk-boundary spaces should not be removed by incremental stripping."""

    class BoundarySpaceAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Then click Security."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            for chunk in ["Then ", "click Security."]:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(kind="done", text="Then click Security.")

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=BoundarySpaceAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert tts.synthesized_texts == ["Then click Security."]


@pytest.mark.asyncio
async def test_streaming_strip_markdown_cross_sentence_bold():
    """Bold spanning two sentences should still be stripped correctly."""

    class CrossSentenceBoldAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "**This is important. Very important.** Got it?"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["**This is important. ", "Very important.** Got it?"]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            full = "**This is important. Very important.** Got it?"
            yield AgentBridgeEvent(kind="done", text=full)

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=CrossSentenceBoldAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # TTS must not contain any stray ** markers
    joined_tts = " ".join(tts.synthesized_texts)
    assert "**" not in joined_tts
    assert "important" in joined_tts.lower()

    # AgentFinal should be fully stripped
    assert len(finals) == 1
    assert "**" not in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_unclosed_bold_multiple_sentences():
    """Unclosed markdown across chunks should not drop earlier stripped text."""

    class UnclosedBoldAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "**First sentence. Second sentence.** Third sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["**First sentence. Second sentence.", "** Third sentence."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="**First sentence. Second sentence.** Third sentence.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=UnclosedBoldAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "**" not in joined_tts
    assert "First sentence." in joined_tts
    assert "Second sentence." in joined_tts
    assert "Third sentence." in joined_tts


@pytest.mark.asyncio
async def test_streaming_strip_markdown_unclosed_italic_multiple_sentences():
    """Unclosed single-italic markdown should defer flushing until closed."""

    class UnclosedItalicAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "*First sentence. Second sentence.* Third sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["*First sentence. Second sentence.", "* Third sentence."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="*First sentence. Second sentence.* Third sentence.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=UnclosedItalicAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "*" not in joined_tts
    assert "First sentence." in joined_tts
    assert "Second sentence." in joined_tts
    assert "Third sentence." in joined_tts


@pytest.mark.asyncio
async def test_streaming_strip_markdown_unclosed_link_multiple_sentences():
    """Unclosed markdown links across chunks should not leak link/url artefacts."""

    class UnclosedLinkAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "See [OpenAI. Next sentence.](https://openai.com/docs) Last sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = [
                "See [OpenAI. Next sentence.",
                "](https://openai.com/docs) Last sentence.",
            ]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="See [OpenAI. Next sentence.](https://openai.com/docs) Last sentence.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=UnclosedLinkAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "https://openai.com/docs" in joined_tts
    assert "](" not in joined_tts
    assert "OpenAI." in joined_tts
    assert "Next sentence." in joined_tts
    assert "Last sentence." in joined_tts

    assert len(finals) == 1
    assert "https://openai.com/docs" in finals[0].text
    assert "](" not in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_flushes_tail_without_sentence_boundary():
    class TailOnlyMarkdownAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "**Final sentence without punctuation**"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            full = "**Final sentence without punctuation**"
            yield AgentBridgeEvent(kind="text_delta", text=full)
            yield AgentBridgeEvent(kind="done", text=full)

    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(chunks=[_chunk(), _chunk()]),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="test"),
            agent=TailOnlyMarkdownAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            strip_markdown=True,
        )
    )

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert tts.synthesized_texts == ["Final sentence without punctuation"]


@pytest.mark.asyncio
async def test_streaming_strip_markdown_failed_turn_does_not_rewrite_prior_history():
    """Failed streaming turns must not patch prior assistant history entries."""

    class FlakyMarkdownAgent(_TestBridgeBase):
        def __init__(self) -> None:

            super().__init__()
            self.calls = 0

        async def run(self, text: str) -> str:
            return "unused"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            self.calls += 1
            if self.calls == 1:
                full = "First **answer**."
                yield AgentBridgeEvent(kind="text_delta", text=full)
                yield AgentBridgeEvent(kind="done", text=full)
                return

            yield AgentBridgeEvent(
                kind="text_delta",
                text="[overwritten](https://example.com)",
            )
            raise RuntimeError("agent failed mid-stream")

    runner = AgentRunner(FlakyMarkdownAgent())
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=runner,
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            strip_markdown=True,
        )
    )

    session._turn = TurnContext("turn-1", CancelToken())
    await session._turn_runner.run_streaming_agent("first", token=None)
    assert runner.history[-1]["role"] == "assistant"
    assert runner.history[-1]["content"] == "First answer."
    history_after_success = [entry.copy() for entry in runner.history]

    session._turn = TurnContext("turn-2", CancelToken())
    await session._turn_runner.run_streaming_agent("second", token=None)
    assert runner.history == history_after_success


@pytest.mark.asyncio
async def test_streaming_interruption_prefers_cancel_token_timestamp(
    monkeypatch: pytest.MonkeyPatch,
):
    """Interruption cutoff should prefer token.cancelled_at over last barge-in time."""

    class CapturingAgent(FastDoneAgent):
        pass

    captured: dict[str, float | None] = {"cutoff": None}

    def _capture_cutoff(
        send_log: list[tuple[float, int, float]],
        playback_ack_log: list[tuple[float, int]],
        cutoff_time: float | None,
        *,
        ack_stale_ms: int,
        ack_tail_cap_ms: int,
    ) -> int:
        captured["cutoff"] = cutoff_time
        return 0

    monkeypatch.setattr(
        "easycat.session.interruption._audio_bytes_likely_heard_hybrid", _capture_cutoff
    )

    agent = CapturingAgent()
    tts = SlowStartTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="test"),
            agent=agent,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    token = CancelToken()
    session._turn = TurnContext("test-turn", token)
    # A stale barge-in timestamp that must NOT be chosen as the cutoff once the
    # token carries its own cancellation time. ``cancelled_at`` is a real
    # monotonic timestamp (orders of magnitude larger than this sentinel), so a
    # mismatch is unambiguous.
    session._turn.last_barge_in_time = 20.0

    async def _cancel_during_tts_playback() -> None:
        await agent.finished.wait()
        await tts.started.wait()
        token.cancel()

    cancel_task = asyncio.create_task(_cancel_during_tts_playback())
    await session._turn_runner.run_streaming_agent("test", token=token)
    await cancel_task

    # The cutoff must come from the token's own cancellation time (set by
    # cancel() during playback), not the stale barge-in time.
    assert token.cancelled_at is not None
    assert captured["cutoff"] == pytest.approx(token.cancelled_at)
    assert captured["cutoff"] != pytest.approx(20.0)


@pytest.mark.asyncio
async def test_streaming_turn_does_not_clear_newer_turn_id() -> None:
    """run_streaming_agent must not clear _turn after a newer turn starts.

    Drives the live streaming path (the only production TTS path) and
    swaps the active turn mid-flight; the post-loop guard keyed on turn
    identity *and* generation must leave the newer turn intact.
    """

    class DelayedTTS(FakeTTS):
        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.synthesized_texts.append(payload.text)
            await asyncio.sleep(0.05)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="test"),
            agent=SlowStreamingAgent(),
            tts=DelayedTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
        )
    )

    old_turn = TurnContext("turn-old", CancelToken())
    session._turn = old_turn
    task = asyncio.create_task(
        session._turn_runner.run_streaming_agent("hello", token=None, turn=old_turn)
    )
    await asyncio.sleep(0.01)
    # A newer turn supersedes the old one while the stream is still running.
    session._turn = TurnContext("turn-new", CancelToken())

    await task

    assert session._turn.id == "turn-new"
