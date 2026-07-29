"""Session streaming barge-in and interruption tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Event,
    ToolCallResult,
    ToolCallStarted,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder, AgentTurnInput
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.tts.input import TTSInput
from tests._bridge_helpers import _TestBridgeBase
from tests.session._session_streaming_helpers import (
    _FAST_TURN,
    FakeNoiseReducer,
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
    FastDoneAgent,
    SlowStartTTS,
    SlowStreamingAgent,
    SlowToolCallingAgent,
    _chunk,
)


def _arm_first_agent_delta(session: Session) -> asyncio.Event:
    """Return an event set when the active turn emits its first text delta."""
    first_delta = asyncio.Event()
    session.event_bus.subscribe(AgentDelta, lambda _event: first_delta.set())
    return first_delta


async def _wait_for_active_cancel_token(
    session: Session,
    first_delta: asyncio.Event,
) -> CancelToken:
    """Wait for real agent output and return the turn token that produced it."""
    await asyncio.wait_for(first_delta.wait(), timeout=1.0)
    token = session.cancel_token
    assert token is not None
    return token


@pytest.mark.asyncio
async def test_session_streaming_barge_in_cancellation():
    """Barge-in during streaming should stop agent output via cancel token."""

    class InterruptibleAgent(_TestBridgeBase):
        """Agent that checks cancel token and stops."""

        def __init__(self) -> None:
            super().__init__()
            self.cancel_observed = asyncio.Event()

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
                    self.cancel_observed.set()
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
    first_delta = _arm_first_agent_delta(session)
    session.event_bus.subscribe(AgentDelta, deltas.append)

    await session.start()
    try:
        token = await _wait_for_active_cancel_token(session, first_delta)
        token.cancel()
        await asyncio.wait_for(agent.cancel_observed.wait(), timeout=1.0)
    finally:
        await session.stop()

    assert 1 <= len(deltas) <= 2


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
    tool_started_event = asyncio.Event()
    tool_result_event = asyncio.Event()

    def record_tool_started(event: ToolCallStarted) -> None:
        tool_started.append(event)
        tool_started_event.set()

    def record_tool_result(event: ToolCallResult) -> None:
        tool_results.append(event)
        tool_result_event.set()

    session.event_bus.subscribe(ToolCallStarted, record_tool_started)
    session.event_bus.subscribe(ToolCallResult, record_tool_result)

    await session.start()
    try:
        await asyncio.wait_for(tool_started_event.wait(), timeout=1.0)

        # Simulate barge-in while tool call is in-flight.
        assert session.cancel_token is not None
        session.cancel_token.cancel()

        await asyncio.wait_for(tool_result_event.wait(), timeout=1.0)
    finally:
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
    first_delta = _arm_first_agent_delta(session)

    await session.start()
    try:
        token = await _wait_for_active_cancel_token(session, first_delta)
        token.cancel()
        await asyncio.wait_for(agent.interruption_observed.wait(), timeout=1.0)
    finally:
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
async def test_session_barge_in_streaming_task_cancel_records_interruption():
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
    turn = TurnContext("turn-cancelled-streaming", token)
    session._turn = turn

    run_task = asyncio.create_task(session._turn_runner.run_streaming_agent("test", token=token))
    try:
        await agent.finished.wait()
        await tts.started.wait()
        turn.record_barge_in()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
    finally:
        await session.stop()

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
    first_delta = _arm_first_agent_delta(session)

    await session.start()
    try:
        token = await _wait_for_active_cancel_token(session, first_delta)
        token.cancel()
        await asyncio.wait_for(agent.interruption_observed.wait(), timeout=1.0)
    finally:
        await session.stop()

    assert agent.interruption_notified
    assert agent.interruption_mode == "message"


@pytest.mark.asyncio
async def test_session_barge_in_with_agent_runner_adds_single_interruption_note():
    """Wrapped AgentRunner should record exactly one interruption note."""
    agent = SlowToolCallingAgent()
    runner = AgentRunner(agent)
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
    first_delta = _arm_first_agent_delta(session)

    await session.start()
    try:
        token = await _wait_for_active_cancel_token(session, first_delta)
        token.cancel()
        await asyncio.wait_for(agent.interruption_observed.wait(), timeout=1.0)
    finally:
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
    agent = SlowStreamingAgent()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=agent,
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    deltas: list[AgentDelta] = []
    first_delta = _arm_first_agent_delta(session)
    session.event_bus.subscribe(AgentDelta, deltas.append)

    await session.start()
    try:
        token = await _wait_for_active_cancel_token(session, first_delta)
        token.cancel()
        await asyncio.wait_for(agent.cancel_observed.wait(), timeout=1.0)
    finally:
        await session.stop()

    assert 1 <= len(deltas) <= 2


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
    first_delta = _arm_first_agent_delta(session)
    playback_started = asyncio.Event()
    session.event_bus.subscribe(BotStartedSpeaking, lambda _event: playback_started.set())

    await session.start()
    try:
        token = await _wait_for_active_cancel_token(session, first_delta)
        await asyncio.wait_for(tts.started.wait(), timeout=1.0)
        await asyncio.wait_for(playback_started.wait(), timeout=1.0)
        token.cancel()
        await asyncio.wait_for(agent.interruption_observed.wait(), timeout=1.0)
    finally:
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


def _arm_playback_finished(session: Session) -> asyncio.Event:
    """Arm a ``BotStoppedSpeaking`` waiter; call before ``session.start()``.

    The late-cancel tests below must cancel only after playback has fully
    completed. A fixed sleep can elapse while playback is still in flight on a
    slow runner, turning the intended *late* cancel into a mid-playback
    interruption. ``BotStoppedSpeaking`` is emitted after the turn's
    ``playback_cut_short`` flag is decided, so a cancel issued after this
    event can never be attributed to playback.
    """
    done = asyncio.Event()
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _e: done.set())
    return done


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

    first_delta = _arm_first_agent_delta(session)
    playback_finished = _arm_playback_finished(session)
    await session.start()
    token = await _wait_for_active_cancel_token(session, first_delta)
    await asyncio.wait_for(playback_finished.wait(), timeout=5.0)

    # Cancel after playback has already completed; this should not be treated
    # as an interruption that mutates agent history.
    token.cancel()

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

    first_delta = _arm_first_agent_delta(session)
    playback_finished = _arm_playback_finished(session)
    await session.start()
    token = await _wait_for_active_cancel_token(session, first_delta)
    await asyncio.wait_for(playback_finished.wait(), timeout=5.0)

    token.cancel()

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

    first_delta = _arm_first_agent_delta(session)
    playback_finished = _arm_playback_finished(session)
    await session.start()
    token = await _wait_for_active_cancel_token(session, first_delta)
    await asyncio.wait_for(playback_finished.wait(), timeout=5.0)

    token.cancel()

    await asyncio.sleep(0.1)
    await session.stop()

    assert tts.synthesized_texts == ["First sentence. ", "Second sentence."]
    assert runner.history[-1] == {
        "role": "assistant",
        "content": "First sentence. Second sentence.",
    }


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
        **_: object,
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
