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
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.timeouts import AgentTimeoutError, TimeoutConfig, TTSTimeoutError
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerState
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
async def test_prompt_agent_runs_journaled_spoken_turn():
    bridge = ContextCapturingBridge(response_prefix="app")
    tts = FakeTTS()
    transport = FakeTransport()
    journal = InMemoryRingBuffer(capacity=1000)
    session = Session(
        SessionConfig(
            transport=transport,
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=bridge,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            journal=journal,
        )
    )
    lifecycle: list[Event] = []
    for event_type in (TurnStarted, AgentFinal, BotStartedSpeaking, BotStoppedSpeaking):
        session.event_bus.subscribe(event_type, lifecycle.append)

    await session.start()
    try:
        response = await session.prompt_agent("Switch to message capture.")
    finally:
        await session.stop(force=True)

    assert response == "app:Follow the application instruction above."
    assert tts.synthesized_texts == ["app:Follow the application instruction above."]
    assert transport.sent
    assert any(
        item["role"] == "system" and "Switch to message capture." in item["content"]
        for item in bridge.contexts[0]
    )
    assert [type(event) for event in lifecycle] == [
        TurnStarted,
        BotStartedSpeaking,
        AgentFinal,
        BotStoppedSpeaking,
    ]
    stage_records = [
        record
        for record in journal.read()
        if record.name in ("stage_start", "stage_complete") and record.data.get("stage") == "agent"
    ]
    assert [record.name for record in stage_records] == ["stage_start", "stage_complete"]
    assert stage_records[0].turn_id is not None
    assert stage_records[0].turn_id == stage_records[1].turn_id


@pytest.mark.asyncio
async def test_system_prompt_stays_out_of_user_history_on_follow_up():
    bridge = ContextCapturingBridge()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=bridge,
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
        )
    )

    await session.prompt_agent("Keep answers concise.", speak=False)
    await session.prompt_agent("What is the status?", role="user", speak=False)

    assert bridge.inputs[0].role == "system"
    assert bridge.inputs[0].text == "Follow the application instruction above."
    assert any(
        item["role"] == "system" and "Keep answers concise." in item["content"]
        for item in bridge.contexts[0]
    )
    assert all(
        not (item["role"] == "user" and "Keep answers concise." in item["content"])
        for context in bridge.contexts
        for item in context
    )
    assert bridge.contexts[1] == []


@pytest.mark.asyncio
async def test_system_prompt_rejects_plain_async_run_agent_explicitly():
    class PlainAgent:
        async def run(self, text: str) -> str:
            return text.upper()

    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=PlainAgent(),
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
        )
    )

    with pytest.raises(ValueError, match="plain async run"):
        await session.prompt_agent("System instruction.", speak=False)

    assert await session.prompt_agent("user message", role="user", speak=False) == "USER MESSAGE"


class _BlockingPromptBridge(_TestBridgeBase):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.active = 0

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder
        self.active += 1
        self.started.set()
        try:
            while not self.release.is_set():
                if cancel_token is not None and cancel_token.is_cancelled:
                    self.cancelled.set()
                    return
                await asyncio.sleep(0)
            yield AgentBridgeEvent(kind="done", text="finished")
        finally:
            if cancel_token is not None and cancel_token.is_cancelled:
                self.cancelled.set()
            self.active -= 1
            self.finished.set()


class _CancellationResistantPromptBridge(_TestBridgeBase):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.finished = asyncio.Event()

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            while not self.release_cleanup.is_set():
                try:
                    await self.release_cleanup.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
            raise
        finally:
            self.finished.set()
        if False:
            yield AgentBridgeEvent(kind="done")


class _CancellationIgnoringPromptBridge(_TestBridgeBase):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        self.started.set()
        await self.release.wait()
        yield AgentBridgeEvent(kind="text_delta", text="stale delta")
        yield AgentBridgeEvent(kind="done", text="stale final")


class _CancellationIgnoringToolPromptBridge(_TestBridgeBase):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        yield AgentBridgeEvent(kind="tool_started", tool_name="write", call_id="call-1")
        await self.release.wait()
        yield AgentBridgeEvent(kind="tool_delta", text="working", call_id="call-1")
        yield AgentBridgeEvent(kind="tool_result", result="written", call_id="call-1")
        yield AgentBridgeEvent(kind="text_delta", text="stale delta")
        yield AgentBridgeEvent(kind="done", text="stale final")


def _prompt_session(
    agent: _TestBridgeBase,
    *,
    journal: InMemoryRingBuffer | None = None,
) -> Session:
    return Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=agent,
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
            journal=journal,
        )
    )


@pytest.mark.asyncio
async def test_prompt_reclaims_turn_when_vad_races_initial_cancellation():
    bridge = _BlockingPromptBridge()
    bridge.release.set()
    session = _prompt_session(bridge)
    session._is_running = True
    await session.start_turn()
    original_cancel = session.cancel_turn

    async def cancel_then_race_vad(*, barge_in: bool = False) -> None:
        await original_cancel(barge_in=barge_in)
        await session.start_turn()

    session.cancel_turn = cancel_then_race_vad  # type: ignore[method-assign]
    try:
        assert (
            await session.prompt_agent("Classify this call.", role="user", speak=False)
            == "finished"
        )
        assert session._turn_manager.state is TurnManagerState.IDLE
    finally:
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_silent_prompt_owns_turn_until_voice_barge_in_drains_it():
    bridge = _BlockingPromptBridge()
    session = _prompt_session(bridge)
    session._is_running = True
    prompt = asyncio.create_task(
        session.prompt_agent("Classify this call.", role="user", speak=False)
    )
    try:
        await asyncio.wait_for(bridge.started.wait(), timeout=1)

        assert session._turn_manager.state is TurnManagerState.PROCESSING
        assert bridge.active == 1

        await session.start_turn()

        assert bridge.finished.is_set()
        assert bridge.cancelled.is_set()
        assert bridge.active == 0
        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
    finally:
        if not prompt.done():
            prompt.cancel()
        await asyncio.gather(prompt, return_exceptions=True)
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_voice_barge_in_does_not_wait_for_resistant_prompt_cleanup():
    bridge = _CancellationResistantPromptBridge()
    transport = FakeTransport()
    session = Session(
        SessionConfig(
            transport=transport,
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=bridge,
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
        )
    )
    session._is_running = True
    prompt = asyncio.create_task(
        session.prompt_agent("Classify this call.", role="user", speak=False)
    )
    try:
        await asyncio.wait_for(bridge.started.wait(), timeout=1)

        await asyncio.wait_for(session.start_turn(), timeout=0.5)

        assert bridge.cancelled.is_set()
        assert not bridge.finished.is_set()
        assert transport.clear_count == 1
        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
    finally:
        bridge.release_cleanup.set()
        await asyncio.gather(prompt, return_exceptions=True)
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_cancelled_silent_prompt_suppresses_late_raw_bridge_output():
    bridge = _CancellationIgnoringPromptBridge()
    session = _prompt_session(bridge)
    emitted: list[Event] = []
    session.event_bus.subscribe(AgentDelta, emitted.append)
    session.event_bus.subscribe(AgentFinal, emitted.append)
    prompt = asyncio.create_task(
        session.prompt_agent("Classify this call.", role="user", speak=False)
    )

    try:
        await asyncio.wait_for(bridge.started.wait(), timeout=1)
        token = session.cancel_token
        assert token is not None
        token.cancel()
        bridge.release.set()

        assert await asyncio.wait_for(prompt, timeout=1) == ""
        assert emitted == []
        assert session._agent_stage._history == []
    finally:
        bridge.release.set()
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_silent_prompt_suppresses_output_after_same_turn_republication():
    bridge = _CancellationIgnoringPromptBridge()
    session = _prompt_session(bridge)
    emitted: list[Event] = []
    session.event_bus.subscribe(AgentDelta, emitted.append)
    session.event_bus.subscribe(AgentFinal, emitted.append)
    prompt = asyncio.create_task(
        session.prompt_agent("Classify this call.", role="user", speak=False)
    )

    try:
        await asyncio.wait_for(bridge.started.wait(), timeout=1)
        turn = session._turn
        assert turn is not None
        session._turn = turn
        bridge.release.set()

        assert await asyncio.wait_for(prompt, timeout=1) == ""
        assert emitted == []
        assert session._agent_stage._history == []
    finally:
        bridge.release.set()
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_silent_prompt_finalizer_preserves_republished_activity():
    bridge = _CancellationIgnoringPromptBridge()
    session = _prompt_session(bridge)
    prompt = asyncio.create_task(
        session.prompt_agent("Classify this call.", role="user", speak=False)
    )

    try:
        await asyncio.wait_for(bridge.started.wait(), timeout=1)
        turn = session._turn
        assert turn is not None
        session._turn_manager._state = TurnManagerState.PROCESSING
        bridge.release.set()

        assert await asyncio.wait_for(prompt, timeout=1) == ""
        assert session._turn is turn
        assert session._turn_manager.state is TurnManagerState.PROCESSING
    finally:
        bridge.release.set()
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_cancelled_silent_prompt_drains_inflight_tool_without_stale_journal_output():
    bridge = _CancellationIgnoringToolPromptBridge()
    journal = InMemoryRingBuffer(capacity=100)
    session = _prompt_session(bridge, journal=journal)
    tool_started = asyncio.Event()
    lifecycle: list[Event] = []

    def record_event(event: Event) -> None:
        lifecycle.append(event)
        if isinstance(event, ToolCallStarted):
            tool_started.set()

    for event_type in (ToolCallStarted, ToolCallDelta, ToolCallResult, AgentDelta, AgentFinal):
        session.event_bus.subscribe(event_type, record_event)

    prompt = asyncio.create_task(
        session.prompt_agent("Classify this call.", role="user", speak=False)
    )
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=1)
        token = session.cancel_token
        assert token is not None
        token.cancel()
        bridge.release.set()

        assert await asyncio.wait_for(prompt, timeout=1) == ""
        assert [type(event) for event in lifecycle] == [
            ToolCallStarted,
            ToolCallDelta,
            ToolCallResult,
        ]
        agent_deltas = [record for record in journal.read() if record.name == "agent_delta"]
        assert [record.data["type"] for record in agent_deltas] == [
            "TOOL_STARTED",
            "TOOL_RESULT",
        ]
        complete = next(record for record in journal.read() if record.name == "stage_complete")
        assert complete.data["response"] == ""
    finally:
        bridge.release.set()
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_graceful_stop_drains_application_prompt():
    bridge = _BlockingPromptBridge()
    session = _prompt_session(bridge)
    prompt = asyncio.create_task(
        session.prompt_agent("Finish the workflow.", role="user", speak=False)
    )
    await asyncio.wait_for(bridge.started.wait(), timeout=1)

    stopping = asyncio.create_task(session.stop())
    await asyncio.sleep(0.01)

    assert not stopping.done()
    assert not bridge.cancelled.is_set()
    bridge.release.set()

    assert await prompt == "finished"
    await asyncio.wait_for(stopping, timeout=1)
    assert bridge.finished.is_set()


@pytest.mark.asyncio
async def test_graceful_stop_rejects_new_application_prompt_during_drain():
    bridge = _BlockingPromptBridge()
    session = _prompt_session(bridge)
    prompt = asyncio.create_task(
        session.prompt_agent("Finish the workflow.", role="user", speak=False)
    )
    await asyncio.wait_for(bridge.started.wait(), timeout=1)

    stopping = asyncio.create_task(session.stop())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="stopping"):
        await session.prompt_agent("Start another workflow.", role="user", speak=False)

    bridge.release.set()
    assert await prompt == "finished"
    await asyncio.wait_for(stopping, timeout=1)


@pytest.mark.asyncio
async def test_prompt_waiting_for_turn_lock_cannot_launch_after_stop():
    bridge = ContextCapturingBridge()
    session = _prompt_session(bridge)
    lock = session._turn_runner._agent_turn_lock
    await lock.acquire()
    prompt = asyncio.create_task(session.prompt_agent("late prompt", role="user", speak=False))
    await asyncio.sleep(0)

    await session.stop(force=True)
    lock.release()

    with pytest.raises(RuntimeError, match="stopping"):
        await prompt
    assert bridge.inputs == []


@pytest.mark.asyncio
async def test_force_stop_cancels_application_prompt():
    bridge = _BlockingPromptBridge()
    session = _prompt_session(bridge)
    prompt = asyncio.create_task(session.prompt_agent("Do not drain.", role="user", speak=False))
    await asyncio.wait_for(bridge.started.wait(), timeout=1)

    await asyncio.wait_for(session.stop(force=True), timeout=1)

    assert bridge.finished.is_set()
    assert bridge.cancelled.is_set()
    assert bridge.active == 0
    [outcome] = await asyncio.gather(prompt, return_exceptions=True)
    assert isinstance(outcome, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_force_stop_escalates_hung_concurrent_graceful_stop():
    bridge = _CancellationResistantPromptBridge()
    session = _prompt_session(bridge)
    prompt = asyncio.create_task(session.prompt_agent("Graceful work.", role="user", speak=False))
    await asyncio.wait_for(bridge.started.wait(), timeout=1)

    graceful = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    assert not graceful.done()

    try:
        await asyncio.wait_for(session.stop(force=True), timeout=0.5)

        assert session._closed
        assert bridge.cancelled.is_set()
        assert not bridge.finished.is_set()
    finally:
        bridge.release_cleanup.set()
        await asyncio.gather(prompt, graceful, return_exceptions=True)


@pytest.mark.asyncio
async def test_reset_state_bounded_drains_active_application_prompt():
    bridge = _CancellationResistantPromptBridge()
    session = _prompt_session(bridge)
    prompt = asyncio.create_task(session.prompt_agent("Temporary work.", role="user", speak=False))
    await asyncio.wait_for(bridge.started.wait(), timeout=1)

    try:
        await asyncio.wait_for(session.reset_state(), timeout=0.5)

        assert bridge.cancelled.is_set()
        assert not bridge.finished.is_set()
        assert session._turn_manager.state is TurnManagerState.IDLE
        assert session._agent_stage._history == []
    finally:
        bridge.release_cleanup.set()
        await asyncio.gather(prompt, return_exceptions=True)
        await session.stop(force=True)
    assert session._agent_stage._history == []


@pytest.mark.asyncio
@pytest.mark.parametrize("speak", [False, True])
async def test_application_prompt_emits_balanced_turn_lifecycle(speak: bool):
    session = _prompt_session(ContextCapturingBridge())
    lifecycle: list[type[Event]] = []
    session.event_bus.subscribe(TurnStarted, lambda event: lifecycle.append(type(event)))
    session.event_bus.subscribe(TurnEnded, lambda event: lifecycle.append(type(event)))
    if speak:
        await session.start()

    await session.prompt_agent("Run workflow.", role="user", speak=speak)

    assert lifecycle == [TurnStarted, TurnEnded]
    await session.stop(force=True)


@pytest.mark.asyncio
async def test_prompt_agent_can_run_silently_before_audio_start():
    bridge = ContextCapturingBridge(response_prefix="silent")
    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=bridge,
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
        )
    )

    response = await session.prompt_agent(
        "Classify this call.",
        role="user",
        speak=False,
    )

    assert response == "silent:Classify this call."
    assert bridge.contexts == [[]]
    assert tts.synthesized_texts == []


@pytest.mark.asyncio
async def test_prompt_agent_validates_surface_and_lifecycle():
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript=""),
            agent=ContextCapturingBridge(),
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
        )
    )

    with pytest.raises(ValueError, match="non-empty"):
        await session.prompt_agent(" ", speak=False)
    with pytest.raises(ValueError, match="role"):
        await session.prompt_agent("hello", role="assistant", speak=False)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="started"):
        await session.prompt_agent("hello")


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
    turn_finished = asyncio.Event()
    for et in [AgentDelta, AgentFinal, BotStartedSpeaking, BotStoppedSpeaking]:
        session.event_bus.subscribe(et, lambda e: events_received.append(e))
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _e: turn_finished.set())

    await session.start()
    await asyncio.wait_for(turn_finished.wait(), timeout=1.0)
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
async def test_agent_failure_fallback_uses_normal_tts_and_journals():
    seen_errors: list[Exception] = []
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    tts = FakeTTS()
    journal = InMemoryRingBuffer(capacity=1000)
    session = Session(
        SessionConfig(
            transport=transport,
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=FailingStreamingAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            journal=journal,
            on_agent_failure=lambda error: (
                seen_errors.append(error) or "I am having trouble. Please try again."
            ),
        )
    )
    bot_stopped = asyncio.Event()
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _event: bot_stopped.set())

    await session.start()
    await asyncio.wait_for(bot_stopped.wait(), timeout=1.0)
    await session.stop()

    assert len(seen_errors) == 1
    assert isinstance(seen_errors[0], RuntimeError)
    assert tts.synthesized_texts == ["I am having trouble. Please try again."]
    assert transport.sent
    [record] = [item for item in journal.read() if item.name == "agent_failure_fallback"]
    assert record.data == {
        "text": "I am having trouble. Please try again.",
        "error_type": "RuntimeError",
    }
    assert record.turn_id is not None


@pytest.mark.asyncio
async def test_agent_timeout_uses_spoken_failure_fallback():
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=transport,
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=TimeoutThenRecoverStreamingAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            timeout_config=TimeoutConfig(agent_timeout=0.01),
            on_agent_failure="The service is taking too long. Please try again.",
        )
    )
    errors: list[Error] = []
    bot_stopped = asyncio.Event()
    session.event_bus.subscribe(Error, lambda event: errors.append(event))
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _event: bot_stopped.set())

    await session.start()
    await asyncio.wait_for(bot_stopped.wait(), timeout=1.0)
    await session.stop()

    assert any(isinstance(event.exception, AgentTimeoutError) for event in errors)
    assert tts.synthesized_texts == ["The service is taking too long. Please try again."]
    assert transport.sent


@pytest.mark.asyncio
async def test_agent_timeout_drops_unfinished_fragment_before_fallback():
    class PartialTimeoutAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return text

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            yield AgentBridgeEvent(kind="text_delta", text="unfinished fragment")
            await asyncio.Event().wait()

    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=transport,
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=PartialTimeoutAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            timeout_config=TimeoutConfig(agent_timeout=0.01),
            on_agent_failure="Please try again.",
        )
    )
    bot_stopped = asyncio.Event()
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _event: bot_stopped.set())

    await session.start()
    await asyncio.wait_for(bot_stopped.wait(), timeout=1.0)
    await session.stop()

    assert tts.synthesized_texts == ["Please try again."]


@pytest.mark.asyncio
async def test_agent_failure_fallback_is_skipped_after_response_audio():
    class PartialResponseAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return text

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            yield AgentBridgeEvent(kind="text_delta", text="Partial answer.")
            await asyncio.sleep(0.02)
            raise RuntimeError("agent failed after playback began")

    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    tts = FakeTTS()
    journal = InMemoryRingBuffer(capacity=1000)
    session = Session(
        SessionConfig(
            transport=transport,
            vad=FakeVAD(),
            stt=FakeSTT(transcript="hello"),
            agent=PartialResponseAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            journal=journal,
            on_agent_failure="Please try again.",
        )
    )
    bot_stopped = asyncio.Event()
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _event: bot_stopped.set())

    await session.start()
    await asyncio.wait_for(bot_stopped.wait(), timeout=1.0)
    await session.stop()

    assert tts.synthesized_texts == ["Partial answer."]
    assert transport.sent
    assert all(record.name != "agent_failure_fallback" for record in journal.read())


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
            timeout_config=TimeoutConfig(agent_timeout=0.03),
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
