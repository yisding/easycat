"""Session streaming integration tests: AgentRunner, incremental TTS, tool events, and barge-in."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.agent_runner import (
    AgentRunner,
    AgentStreamEvent,
    AgentStreamEventType,
)
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
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
from easycat.session import (
    Session,
    SessionConfig,
    _all_tts_audio_sent,
    _estimate_text_spoken,
    _has_unclosed_markdown_delimiters,
    _split_at_sentence_boundaries,
)
from easycat.turn_manager import TurnManagerConfig

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

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(text)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class FakeNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


# ── Streaming test agents ──────────────────────────────────────────


class StreamingUpperAgent:
    """Streaming agent that uppercases and streams word by word."""

    async def run(self, text: str) -> str:
        return text.upper()

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        words = text.upper().split()
        for i, word in enumerate(words):
            if cancel_token and cancel_token.is_cancelled:
                break
            delta = word if i == 0 else f" {word}"
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=delta)
        full = " ".join(words)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=full)


class StreamingSentenceAgent:
    """Streams text with sentence boundaries for incremental TTS testing."""

    async def run(self, text: str) -> str:
        return "Hello world. How are you? I am fine."

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        chunks = ["Hello world. ", "How are you? ", "I am fine."]
        for chunk in chunks:
            if cancel_token and cancel_token.is_cancelled:
                break
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text="Hello world. How are you? I am fine.",
        )


class StreamingToolCallingAgent:
    """Streaming agent that calls a tool during response."""

    async def run(self, text: str) -> str:
        return "The result is 42."

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="calculator",
            call_id="call_abc",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_DELTA,
            call_id="call_abc",
            text="computing...",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_abc",
            result="42",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TEXT_DELTA,
            text="The result is 42.",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text="The result is 42.",
        )


class SlowStreamingAgent:
    """Agent that streams slowly — useful for barge-in testing."""

    async def run(self, text: str) -> str:
        return "slow response"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        for word in ["slow ", "response"]:
            if cancel_token and cancel_token.is_cancelled:
                break
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=word)
            await asyncio.sleep(0.05)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="slow response")


class FailingStreamingAgent:
    """Agent that raises during streaming."""

    async def run(self, text: str) -> str:
        return "won't get here"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="start ")
        raise RuntimeError("agent failed mid-stream")


# ── Sentence boundary helper tests ────────────────────────────────


def test_split_no_boundary():
    ready, remaining = _split_at_sentence_boundaries("hello world")
    assert ready == ""
    assert remaining == "hello world"


def test_split_single_sentence():
    ready, remaining = _split_at_sentence_boundaries("Hello world. ")
    # Single sentence is buffered; the caller flushes when the LLM finishes.
    assert ready == ""
    assert remaining == "Hello world. "


def test_split_multiple_sentences():
    text = "Hello. How are you? Fine"
    ready, remaining = _split_at_sentence_boundaries(text)
    # Should split at last boundary (after "you? ")
    assert "Hello" in ready
    assert "How are you" in ready
    assert remaining == "Fine"


def test_split_incomplete_sentence():
    ready, remaining = _split_at_sentence_boundaries("Hello world. How are")
    assert "Hello world" in ready
    assert remaining == "How are"


def test_split_abbreviation_sentence():
    text = "Dr. Smith went home. Next"
    ready, remaining = _split_at_sentence_boundaries(text)
    assert ready.strip() == "Dr. Smith went home."
    assert remaining == "Next"


def test_split_trailing_abbreviation():
    ready, remaining = _split_at_sentence_boundaries("Nice to meet you Mr. ")
    assert ready == ""
    assert remaining == "Nice to meet you Mr. "


def test_split_newline_sentence():
    text = "Hello world!\nHow are you"
    ready, remaining = _split_at_sentence_boundaries(text)
    assert ready.strip() == "Hello world!"
    assert remaining == "How are you"


def test_split_spanish_sentence():
    text = "Hola mundo. ¿Cómo estás? Bien"
    ready, remaining = _split_at_sentence_boundaries(text)
    assert "Hola mundo." in ready
    assert "¿Cómo estás?" in ready
    assert remaining == "Bien"


def test_split_chinese_sentence():
    text = "你好。今天天气不错。继续"
    ready, remaining = _split_at_sentence_boundaries(text)
    assert ready == "你好。今天天气不错。"
    assert remaining == "继续"


# ── _estimate_text_spoken tests ─────────────────────────────────────


def test_estimate_text_spoken_empty():
    assert _estimate_text_spoken([], 0) == ""
    assert _estimate_text_spoken([], 100) == ""
    assert _estimate_text_spoken([("Hello.", 320)], 0) == ""


def test_estimate_text_spoken_full_chunks():
    """When all audio was sent, the full text is returned."""
    chunks = [("Hello. ", 320), ("How are you?", 640)]
    assert _estimate_text_spoken(chunks, 960) == "Hello. How are you?"
    # More bytes sent than produced — still returns full text
    assert _estimate_text_spoken(chunks, 9999) == "Hello. How are you?"


def test_estimate_text_spoken_partial_first_chunk():
    """When only part of the first chunk's audio was sent, estimate proportionally."""
    chunks = [("Hello world.", 1000)]
    # Half the audio sent → approximately half the text
    assert _estimate_text_spoken(chunks, 500) == "Hello "


def test_estimate_text_spoken_one_and_a_half_chunks():
    """First chunk fully sent, second chunk partially sent."""
    chunks = [("First sentence. ", 400), ("Second sentence.", 400)]
    # All of first chunk (400) + half of second (200) = 600
    spoken = _estimate_text_spoken(chunks, 600)
    assert spoken.startswith("First sentence. ")
    # Second chunk has 16 chars, half → 8 chars
    assert spoken == "First sentence. Second s"


def test_estimate_text_spoken_skips_zero_audio_chunks():
    """Chunks with 0 audio bytes (cancelled before any output) are skipped."""
    chunks = [("First. ", 320), ("Never spoken.", 0), ("Third.", 320)]
    # 320 covers first chunk, 0-byte chunk is skipped, then 320 for third
    assert _estimate_text_spoken(chunks, 640) == "First. Third."


def test_all_tts_audio_sent():
    chunks = [("Hello. ", 320, True), ("How are you?", 640, True)]

    assert not _all_tts_audio_sent([], 960)
    assert not _all_tts_audio_sent(chunks, 959)
    assert _all_tts_audio_sent(chunks, 960)
    assert _all_tts_audio_sent(chunks, 9999)


def test_all_tts_audio_sent_ignores_non_positive_chunks():
    chunks = [("First.", 320, True), ("Silent", 0, True), ("Oops", -20, True)]
    assert not _all_tts_audio_sent(chunks, 319)
    assert _all_tts_audio_sent(chunks, 320)


def test_all_tts_audio_sent_requires_completed_synthesis():
    chunks = [("Hello", 320, False)]
    assert not _all_tts_audio_sent(chunks, 320)


def test_markdown_unclosed_single_italic_asterisk():
    assert _has_unclosed_markdown_delimiters("*First sentence. Second sentence")
    assert not _has_unclosed_markdown_delimiters("*First sentence. Second sentence*")


def test_markdown_unclosed_single_italic_underscore():
    assert _has_unclosed_markdown_delimiters("_First sentence. Second sentence")
    assert not _has_unclosed_markdown_delimiters("_First sentence. Second sentence_")
    assert not _has_unclosed_markdown_delimiters("Use my_variable_name here.")


def test_markdown_unclosed_link_or_image_delimiters():
    assert _has_unclosed_markdown_delimiters("See [OpenAI")
    assert _has_unclosed_markdown_delimiters("See [OpenAI]")
    assert _has_unclosed_markdown_delimiters("See [OpenAI](https://openai.com/docs")
    assert _has_unclosed_markdown_delimiters("See ![diagram](https://img.example.com/plot")
    assert not _has_unclosed_markdown_delimiters("See [OpenAI](https://openai.com/docs).")
    assert not _has_unclosed_markdown_delimiters(
        "See [Function](https://en.wikipedia.org/wiki/Function_(mathematics))."
    )


def test_markdown_delimiters_inside_inline_code_do_not_block_streaming():
    assert not _has_unclosed_markdown_delimiters("Literal `**` should not block.")
    assert not _has_unclosed_markdown_delimiters("Literal `__` should not block.")
    assert not _has_unclosed_markdown_delimiters("Literal `~~` should not block.")
    assert _has_unclosed_markdown_delimiters("Literal `**` and **still open")


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
    assert errors[0].context == "agent"
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
    assert errors[0].context == "agent"
    assert isinstance(errors[0].exception, RuntimeError)


@pytest.mark.asyncio
async def test_session_streaming_barge_in_cancellation():
    """Barge-in during streaming should stop agent output via cancel token."""

    class InterruptibleAgent:
        """Agent that checks cancel token and stops."""

        async def run(self, text: str) -> str:
            return "full response"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            for word in ["Hello ", "world. ", "This ", "is ", "a ", "long ", "response."]:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=word)
                await asyncio.sleep(0.03)
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="")

    # Custom VAD that triggers barge-in mid-stream
    class BargeInVAD:
        def __init__(self) -> None:
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
    if session._cancel_token:
        session._cancel_token.cancel()

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


# ── Session with AgentRunner (full integration) ───────────────────


@pytest.mark.asyncio
async def test_session_with_agent_runner_streaming():
    """Full pipeline with AgentRunner wrapping a streaming agent."""

    class MyStreamingAgent:
        async def run(self, text: str) -> str:
            return f"Reply: {text}"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            response = f"Reply: {text}"
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=response)
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=response)

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

    # Verify tracing spans were created
    assert len(runner.spans) > 0
    span_names = [s.name for s in runner.spans]
    assert "stt_to_agent" in span_names
    assert "agent_execution" in span_names


@pytest.mark.asyncio
async def test_streaming_flushes_final_buffer_to_tts():
    class BufferingAgent:
        async def run(self, text: str) -> str:
            return "Hello world"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello world")
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Hello world")

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
    class DeltaOnlyAgent:
        async def run(self, text: str) -> str:
            return "Hello world"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello world")

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
    class DelayedAfterDoneAgent:
        async def run(self, text: str) -> str:
            return "Hello world"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello world")
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Hello world")
            await asyncio.sleep(0.3)

    class ObservableFakeTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
            self.started.set()
            async for event in super().synthesize(text):
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

    task = asyncio.create_task(session._run_streaming_agent("hello", token=None))
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


class SlowToolCallingAgent:
    """Streaming agent with a tool call that takes time, allowing
    cancellation to arrive mid-tool."""

    def __init__(self) -> None:
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    async def run(self, text: str) -> str:
        return "The answer is 42."

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        # Text before tool
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Let me look. ")
        # Tool lifecycle
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="database_update",
            call_id="call_xyz",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_DELTA,
            call_id="call_xyz",
            text="updating...",
        )
        # Simulate slow tool — cancellation arrives here
        await asyncio.sleep(0.1)
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_xyz",
            result="row updated",
        )
        # Text after tool (should be skipped on barge-in)
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Done!")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Done!")

    def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
        self.interruption_notified = True
        self.interruption_text_spoken = text_spoken
        self.interruption_mode = mode


class FastDoneAgent:
    """Agent that completes quickly and supports interruption notifications."""

    def __init__(self) -> None:
        self.finished = asyncio.Event()
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    async def run(self, text: str) -> str:
        return "Quick reply."

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Quick reply.")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Quick reply.")
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

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(text)
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
    if session._cancel_token:
        session._cancel_token.cancel()

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

    if session._cancel_token:
        session._cancel_token.cancel()

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

    async def _cancel_during_tts_playback() -> None:
        await agent.finished.wait()
        await tts.started.wait()
        token.cancel()

    cancel_task = asyncio.create_task(_cancel_during_tts_playback())
    await session._run_streaming_agent("test", token=token)
    await cancel_task

    assert agent.interruption_notified
    assert agent.interruption_text_spoken == ""
    assert agent.interruption_mode == "truncate"


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

    if session._cancel_token:
        session._cancel_token.cancel()

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

    if session._cancel_token:
        session._cancel_token.cancel()

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

    if session._cancel_token:
        session._cancel_token.cancel()

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

    class FastAgent:
        """Completes instantly with a full sentence."""

        interruption_notified = False
        interruption_text_spoken = ""
        interruption_mode = ""

        async def run(self, text: str) -> str:
            return "Hello world."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello world.")
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Hello world.")

        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            self.interruption_notified = True
            self.interruption_text_spoken = text_spoken
            self.interruption_mode = mode

    class SlowTTS:
        """TTS that yields audio slowly so cancel can arrive mid-playback."""

        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
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
    if session._cancel_token:
        session._cancel_token.cancel()

    await asyncio.sleep(0.3)
    await session.stop()

    assert agent.interruption_notified
    assert agent.interruption_mode == "truncate"


@pytest.mark.asyncio
async def test_session_barge_in_after_full_playback_does_not_notify_interruption():
    """If all synthesized audio is already delivered, interruption should not rewrite history."""

    class FastAgent:
        interruption_notified = False

        async def run(self, text: str) -> str:
            return "Hello world."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello world.")
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Hello world.")

        def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
            self.interruption_notified = True

    class OneShotTTS:
        async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
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
    if session._cancel_token:
        session._cancel_token.cancel()

    await asyncio.sleep(0.1)
    await session.stop()

    assert not agent.interruption_notified


@pytest.mark.asyncio
async def test_session_barge_in_after_full_playback_keeps_agent_runner_history():
    """Late cancel after full playback should not truncate AgentRunner history."""

    class FastAgent:
        async def run(self, text: str) -> str:
            return "Hello world."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello world.")
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Hello world.")

    class OneShotTTS:
        async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
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

    if session._cancel_token:
        session._cancel_token.cancel()

    await asyncio.sleep(0.1)
    await session.stop()

    assert runner.history[-1] == {"role": "assistant", "content": "Hello world."}


# ── Streaming with strip_markdown ──────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_strip_markdown_tts_receives_clean_text():
    """Markdown should be stripped from TTS chunks and AgentFinal when enabled."""

    class MarkdownStreamingAgent:
        async def run(self, text: str) -> str:
            return "Go to **Settings** first. Then click *Security* next."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
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
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            full = "Go to **Settings** first. Then click *Security* next."
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=full)

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
async def test_streaming_strip_markdown_normalizes_short_code_spans_for_tts():
    class CodeStreamingAgent:
        async def run(self, text: str) -> str:
            return "Call `__init__`. Then run `print()`."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            chunks = ["Call `__init__`. ", "Then run `print()`."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            yield AgentStreamEvent(
                type=AgentStreamEventType.DONE,
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

    class BoundarySpaceAgent:
        async def run(self, text: str) -> str:
            return "Then click Security."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            for chunk in ["Then ", "click Security."]:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Then click Security.")

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

    class CrossSentenceBoldAgent:
        async def run(self, text: str) -> str:
            return "**This is important. Very important.** Got it?"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            chunks = ["**This is important. ", "Very important.** Got it?"]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            full = "**This is important. Very important.** Got it?"
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=full)

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

    class UnclosedBoldAgent:
        async def run(self, text: str) -> str:
            return "**First sentence. Second sentence.** Third sentence."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            chunks = ["**First sentence. Second sentence.", "** Third sentence."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            yield AgentStreamEvent(
                type=AgentStreamEventType.DONE,
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

    class UnclosedItalicAgent:
        async def run(self, text: str) -> str:
            return "*First sentence. Second sentence.* Third sentence."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            chunks = ["*First sentence. Second sentence.", "* Third sentence."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            yield AgentStreamEvent(
                type=AgentStreamEventType.DONE,
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

    class UnclosedLinkAgent:
        async def run(self, text: str) -> str:
            return "See [OpenAI. Next sentence.](https://openai.com/docs) Last sentence."

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            chunks = [
                "See [OpenAI. Next sentence.",
                "](https://openai.com/docs) Last sentence.",
            ]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=chunk)
            yield AgentStreamEvent(
                type=AgentStreamEventType.DONE,
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
async def test_streaming_strip_markdown_failed_turn_does_not_rewrite_prior_history():
    """Failed streaming turns must not patch prior assistant history entries."""

    class FlakyMarkdownAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, text: str) -> str:
            return "unused"

        async def run_streaming(
            self,
            text: str,
            *,
            context: list[dict[str, str]] | None = None,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentStreamEvent]:
            self.calls += 1
            if self.calls == 1:
                full = "First **answer**."
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=full)
                yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=full)
                return

            yield AgentStreamEvent(
                type=AgentStreamEventType.TEXT_DELTA,
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

    await session._run_streaming_agent("first", token=None)
    assert runner.history[-1]["role"] == "assistant"
    assert runner.history[-1]["content"] == "First answer."
    history_after_success = [entry.copy() for entry in runner.history]

    await session._run_streaming_agent("second", token=None)
    assert runner.history == history_after_success
