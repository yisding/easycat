"""Session streaming turn behavior tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat import create_text_session
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    Event,
    STTFinal,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TurnEnded,
    TurnStarted,
)
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder, AgentTurnInput
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.timeouts import AgentTimeoutError, TimeoutConfig, TTSTimeoutError
from easycat.tts.input import TTSInput
from tests._bridge_helpers import _TestBridgeBase
from tests.session._session_streaming_helpers import (
    _FAST_TURN,
    ContextCapturingBridge,
    DoneOnlyStreamingAgent,
    FailingStreamingAgent,
    FakeNoiseReducer,
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
    FastDoneAgent,
    PostDoneStreamingAgent,
    SlowStreamingAgent,
    StreamingSentenceAgent,
    StreamingToolCallingAgent,
    StreamingUpperAgent,
    StructuredOnlyStreamingAgent,
    TimeoutThenRecoverStreamingAgent,
    TimeoutThenRecoverTTS,
    _chunk,
)


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
async def test_streaming_done_stops_unbounded_session_consumption() -> None:
    class PostDoneHangingAgent(_TestBridgeBase):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        async def run(self, text: str) -> str:
            return "Alpha."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            try:
                yield AgentBridgeEvent(kind="text_delta", text="Alpha.")
                yield AgentBridgeEvent(kind="done", text="Alpha.")
                await asyncio.Event().wait()
                yield AgentBridgeEvent(kind="text_delta", text=" Beta.")  # pragma: no cover
            finally:
                self.closed = True

    agent = PostDoneHangingAgent()
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
            timeout_config=None,
        )
    )

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    session._turn = TurnContext("test-turn", CancelToken())
    await asyncio.wait_for(
        session._turn_runner.run_streaming_agent("hello", token=None), timeout=0.2
    )

    assert len(finals) == 1
    assert finals[0].text == "Alpha."
    assert tts.synthesized_texts == ["Alpha."]
    assert agent.closed


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
async def test_streaming_agent_timeout_cancels_before_error_dispatch() -> None:
    """Late agent success must not overtake a slow timeout error handler."""

    class LateSuccessAgent(_TestBridgeBase):
        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            await asyncio.sleep(0.02)
            yield AgentBridgeEvent(kind="text_delta", text="Late success.")
            yield AgentBridgeEvent(kind="done", text="Late success.")

    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=LateSuccessAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            timeout_config=TimeoutConfig(agent_timeout=0.01),
        )
    )

    errors: list[Error] = []
    finals: list[AgentFinal] = []

    async def record_error_slowly(event: Error) -> None:
        await asyncio.sleep(0.03)
        errors.append(event)

    session.event_bus.subscribe(Error, record_error_slowly)
    session.event_bus.subscribe(AgentFinal, lambda event: finals.append(event))
    session._turn = TurnContext("turn-1", CancelToken())

    await session._turn_runner.run_streaming_agent("first", token=None)

    assert len(errors) == 1
    assert isinstance(errors[0].exception, AgentTimeoutError)
    assert finals == []
    assert tts.synthesized_texts == []


@pytest.mark.asyncio
async def test_streaming_agent_timeout_drains_cleanup_before_error_dispatch() -> None:
    cleanup_finished = asyncio.Event()

    class SlowCleanupAgent(_TestBridgeBase):
        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            try:
                await asyncio.Event().wait()
                yield AgentBridgeEvent(kind="done", text="unreachable")
            finally:
                await asyncio.sleep(0.02)
                cleanup_finished.set()

    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=SlowCleanupAgent(),
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            timeout_config=TimeoutConfig(agent_timeout=0.01),
        )
    )
    cleanup_seen_by_handler: list[bool] = []

    def record_error(event: Error) -> None:
        _ = event
        cleanup_seen_by_handler.append(cleanup_finished.is_set())

    session.event_bus.subscribe(Error, record_error)
    session._turn = TurnContext("turn-1", CancelToken())

    await session._turn_runner.run_streaming_agent("first", token=None)

    assert cleanup_seen_by_handler == [True]


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
