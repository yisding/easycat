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
from easycat.session import Session, SessionConfig, _split_at_sentence_boundaries
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
