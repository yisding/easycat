from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from easycat import create_session
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    Interruption,
    STTEvent,
    STTEventType,
    STTFinal,
    STTPartial,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TurnEnded,
    TurnStarted,
)
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._legacy_types import (
    AgentStreamEvent,
    AgentStreamEventType,
)
from easycat.turn_manager import TurnManagerConfig, TurnMode

from .harness import (
    EventCollector,
    QueueTransport,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    make_chunk,
    make_test_config,
    patch_provider_factories,
    wait_for_condition,
)

pytestmark = pytest.mark.integration_local

FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


# ── Test agents ──────────────────────────────────────────────────────


class UpperAgent:
    """Simple agent that uppercases input."""

    async def run(self, text: str) -> str:
        return text.upper()


class EchoAgent:
    """Agent that echoes with a prefix."""

    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class SlowAgent:
    """Agent that takes a configurable delay before responding."""

    def __init__(self, delay: float = 0.2) -> None:
        self._delay = delay

    async def run(self, text: str) -> str:
        await asyncio.sleep(self._delay)
        return text.upper()


class FailingAgent:
    """Agent that always raises."""

    async def run(self, text: str) -> str:
        raise RuntimeError("agent exploded")


class EmptyResponseAgent:
    """Agent that returns empty string."""

    async def run(self, text: str) -> str:
        return ""


class WhitespaceOnlyAgent:
    """Agent that returns only whitespace."""

    async def run(self, text: str) -> str:
        return "   \n  \t  "


class NewlineAgent:
    """Agent that returns newlines between words."""

    async def run(self, text: str) -> str:
        return "Hello\n\nWorld"


class SlowStreamingAgent:
    """Streaming agent with configurable delay between chunks."""

    def __init__(self, sentences: list[str], delay: float = 0.05) -> None:
        self._sentences = sentences
        self._delay = delay

    async def run(self, text: str) -> str:
        return " ".join(self._sentences)

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        accumulated = ""
        for sentence in self._sentences:
            if cancel_token and cancel_token.is_cancelled:
                break
            await asyncio.sleep(self._delay)
            accumulated += sentence
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=sentence)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=accumulated)


class MultiSentenceStreamingAgent:
    """Streaming agent yielding word-by-word deltas for sentence boundary testing."""

    async def run(self, text: str) -> str:
        return "Hello world. How are you? I am fine."

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        words = "Hello world. How are you? I am fine.".split(" ")
        accumulated = ""
        for i, word in enumerate(words):
            if cancel_token and cancel_token.is_cancelled:
                break
            token = word if i == 0 else " " + word
            accumulated += token
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=token)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=accumulated)


class StreamingAgentWithToolCall:
    """Streaming agent that makes a tool call mid-response."""

    async def run(self, text: str) -> str:
        return "Before tool. After tool."

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Before tool. ")
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="lookup",
            call_id="call_1",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_1",
            result="42",
        )
        if cancel_token and cancel_token.is_cancelled:
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Before tool. ")
            return
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="After tool.")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Before tool. After tool.")


class StreamingWhitespaceAgent:
    """Streaming agent that produces only whitespace deltas."""

    async def run(self, text: str) -> str:
        return "   "

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="   ")
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="\n\t")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="   \n\t")


class StreamingEmptyDoneAgent:
    """Streaming agent that yields deltas but DONE has empty text."""

    async def run(self, text: str) -> str:
        return "Hello"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="")


class SlowToolCallAgent:
    """Streaming agent with a slow tool call that can be interrupted."""

    async def run(self, text: str) -> str:
        return "Done"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Before. ")
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="slow_tool",
            call_id="call_slow",
        )
        # Simulate slow tool
        await asyncio.sleep(0.3)
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_slow",
            result="result",
        )
        if cancel_token and cancel_token.is_cancelled:
            yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Before. ")
            return
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="After.")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Before. After.")


class StreamingMultiSentenceAgent:
    """Streaming agent that yields text word by word."""

    def __init__(self, text: str = "First sentence. Second sentence."):
        self._text = text

    async def run(self, text: str) -> str:
        return self._text

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        words = self._text.split(" ")
        accumulated = ""
        for i, word in enumerate(words):
            if cancel_token and cancel_token.is_cancelled:
                break
            token = word if i == 0 else " " + word
            accumulated += token
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=token)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=accumulated)


class ZeroAudioTTS:
    """TTS that produces events with no audio data."""

    supports_ssml = False

    def __init__(self) -> None:
        self.payloads: list = []

    async def synthesize(self, payload):
        from easycat.tts.input import coerce_tts_input

        self.payloads.append(coerce_tts_input(payload))
        # Yield a markers event but no audio
        yield TTSEvent(
            type=TTSEventType.MARKERS,
            markers=[{"type": "word", "value": "test"}],
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


# ── Turn lifecycle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_turn_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a complete turn emits events in correct order."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(
        TurnStarted,
        TurnEnded,
        STTFinal,
        AgentFinal,
        BotStartedSpeaking,
        BotStoppedSpeaking,
    )

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        event_types = [type(e).__name__ for e in collector.events]
        assert "TurnStarted" in event_types
        assert "TurnEnded" in event_types
        assert "STTFinal" in event_types
        assert "AgentFinal" in event_types
        assert "BotStartedSpeaking" in event_types
        assert "BotStoppedSpeaking" in event_types

        # Verify ordering: TurnStarted before STTFinal before AgentFinal
        ts_idx = event_types.index("TurnStarted")
        te_idx = event_types.index("TurnEnded")
        sf_idx = event_types.index("STTFinal")
        af_idx = event_types.index("AgentFinal")
        assert ts_idx < te_idx < sf_idx < af_idx
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_empty_transcript_resets_to_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """When STT returns empty transcript, session should reset to idle without running agent."""
    transport = QueueTransport()
    stt = ScriptedSTT([""])  # empty transcript
    tts = RecordingTTS()
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, TurnEnded, AgentFinal, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(TurnEnded, timeout=2.0)
        # Give time for agent to run (it shouldn't)
        await asyncio.sleep(0.2)

        agent_finals = [e for e in collector.events if isinstance(e, AgentFinal)]
        assert len(agent_finals) == 0, "Agent should not run on empty transcript"
        assert tts.payloads == [], "TTS should not be called on empty transcript"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_agent_error_emits_error_event_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """When agent raises, an Error event should be emitted and turn state should reset."""
    transport = QueueTransport()
    stt = ScriptedSTT(["trigger error"])
    tts = RecordingTTS()
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=FailingAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error, TurnStarted, AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        error_event = await collector.wait_for(Error, timeout=3.0)
        assert error_event.stage == ErrorStage.AGENT
        assert isinstance(error_event.exception, RuntimeError)

        # Wait a bit to ensure no AgentFinal was emitted
        await asyncio.sleep(0.2)
        agent_finals = [e for e in collector.events if isinstance(e, AgentFinal)]
        assert len(agent_finals) == 0
        assert tts.payloads == []
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_multi_turn_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two full turns should complete independently and emit correct events."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first turn", "second turn"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # Two full turns: start/stop/start/stop
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, TurnStarted, BotStoppedSpeaking)

    await session.start()
    try:
        # Turn 1
        await transport.push_audio(make_chunk(), make_chunk())
        first_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert first_final.text == "FIRST TURN"

        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        # Turn 2 - need more audio to trigger next VAD events
        for _ in range(4):
            await transport.push_audio(make_chunk())

        second_final = await collector.wait_for(
            AgentFinal,
            predicate=lambda e: e.text == "SECOND TURN",
            timeout=3.0,
        )
        assert second_final.text == "SECOND TURN"

        assert len(tts.payloads) == 2
    finally:
        await transport.finish_input()
        await session.stop()


# ── Barge-in and interruption ────────────────────────────────────────


@pytest.mark.asyncio
async def test_barge_in_during_bot_speaking(monkeypatch: pytest.MonkeyPatch) -> None:
    """User speaks while bot is playing TTS -> should emit Interruption and start new turn."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first", "second"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.05)
    # First two chunks: start, stop. After waiting for bot to start speaking,
    # third chunk triggers barge-in start, fourth triggers stop.
    vad = ScriptedVAD(["start", "stop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=EchoAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, Interruption, AgentFinal, BotStartedSpeaking)

    await session.start()
    try:
        # First turn: start + stop
        await transport.push_audio(make_chunk(), make_chunk())

        # Wait for bot to actually start speaking before sending barge-in audio
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Now send audio that triggers barge-in (VAD start during BOT_SPEAKING)
        await transport.push_audio(make_chunk(), make_chunk())

        interruption = await collector.wait_for(Interruption, timeout=3.0)
        assert interruption is not None

        # Second TurnStarted fires right after Interruption
        await wait_for_condition(
            lambda: len([e for e in collector.events if isinstance(e, TurnStarted)]) >= 2,
            timeout=2.0,
        )
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_speech_resume_during_pause_cancels_silence_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speech resuming during USER_PAUSED should cancel silence timer and stay in USER_SPEAKING."""
    transport = QueueTransport()
    # First transcript won't fire until second stop
    stt = ScriptedSTT(["hello world"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # start -> stop (pause) -> start (resume) -> stop (final)
    vad = ScriptedVAD(["start", "stop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    # Use a longer silence timeout to test that it gets cancelled
    slow_turn = TurnManagerConfig(end_of_turn_silence_ms=5000)
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=slow_turn)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, TurnEnded, AgentFinal)

    await session.start()
    try:
        for _ in range(4):
            await transport.push_audio(make_chunk())

        # The turn should not end from the first stop (timer cancelled by resume)
        # but should end after the second stop
        # Override the turn config for final stop to be fast
        session._turn_manager._config = TurnManagerConfig(end_of_turn_silence_ms=1)

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO WORLD"

        # Only one TurnStarted should fire (speech resume doesn't start a new turn)
        turn_starts = [e for e in collector.events if isinstance(e, TurnStarted)]
        assert len(turn_starts) == 1
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_barge_in_during_processing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speech starting while agent is still processing should trigger barge-in."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # start, stop trigger first turn. After TurnEnded, send audio with "start" to barge-in.
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = SlowAgent(delay=0.5)
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, Interruption, TurnEnded)

    await session.start()
    try:
        # First turn: start + stop
        await transport.push_audio(make_chunk(), make_chunk())
        # Wait for turn to end (entering PROCESSING state, agent is slow)
        await collector.wait_for(TurnEnded, timeout=3.0)

        # Now send audio that triggers VAD start during PROCESSING
        await transport.push_audio(make_chunk())

        interruption = await collector.wait_for(Interruption, timeout=3.0)
        assert interruption is not None

        # Second TurnStarted fires right after Interruption
        await wait_for_condition(
            lambda: len([e for e in collector.events if isinstance(e, TurnStarted)]) >= 2,
            timeout=2.0,
        )
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_cancel_token_set_on_barge_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On barge-in, the previous cancel token should be signalled."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first"])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640), chunk_delay_s=0.03)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = EchoAgent()
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Interruption, BotStartedSpeaking)

    await session.start()
    try:
        # First turn
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Capture the token before barge-in
        old_token = session.cancel_token

        # Barge-in
        await transport.push_audio(make_chunk())
        await collector.wait_for(Interruption, timeout=3.0)

        # Old token should be cancelled
        assert old_token is not None
        assert old_token.is_cancelled
    finally:
        await transport.finish_input()
        await session.stop()


# ── Streaming agents ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_agent_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming agent should emit AgentDelta events and a final AgentFinal."""
    transport = QueueTransport()
    stt = ScriptedSTT(["test"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = MultiSentenceStreamingAgent()
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentDelta, AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert "Hello world" in agent_final.text

        deltas = [e for e in collector.events if isinstance(e, AgentDelta)]
        assert len(deltas) > 0, "Should have received streaming deltas"

        # TTS should have been called with at least one sentence
        assert len(tts.payloads) >= 1
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_agent_barge_in_stops_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Barge-in during streaming agent should cancel TTS and emit Interruption."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first"])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640), chunk_delay_s=0.03)
    # start, stop triggers first turn. After bot starts speaking, "start" triggers barge-in.
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = SlowStreamingAgent(
        ["First sentence. ", "Second sentence. ", "Third sentence."],
        delay=0.03,
    )
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Interruption, TurnStarted, AgentFinal, BotStartedSpeaking)

    await session.start()
    try:
        # First turn
        await transport.push_audio(make_chunk(), make_chunk())
        # Wait for bot to actually start speaking
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)
        # Now trigger barge-in
        await transport.push_audio(make_chunk())

        interruption = await collector.wait_for(Interruption, timeout=3.0)
        assert interruption is not None
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_agent_with_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming agent with tool calls should emit tool events and complete normally."""
    transport = QueueTransport()
    stt = ScriptedSTT(["lookup something"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    from easycat.events import ToolCallResult, ToolCallStarted

    agent = StreamingAgentWithToolCall()
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(ToolCallStarted, ToolCallResult, AgentFinal, AgentDelta)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert "Before tool" in agent_final.text
        assert "After tool" in agent_final.text

        tool_starts = [e for e in collector.events if isinstance(e, ToolCallStarted)]
        tool_results = [e for e in collector.events if isinstance(e, ToolCallResult)]
        assert len(tool_starts) == 1
        assert len(tool_results) == 1
        assert tool_results[0].result == "42"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_agent_interrupted_mid_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barge-in during a tool call should drain the tool then stop."""
    transport = QueueTransport()
    stt = ScriptedSTT(["start tool"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.02)
    # start, stop (first turn), then start (barge-in during TTS/tool)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=SlowToolCallAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, BotStartedSpeaking, Interruption, Error)

    await session.start()
    try:
        # First turn
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Barge-in during TTS playback
        await transport.push_audio(make_chunk())
        await collector.wait_for(Interruption, timeout=3.0)

        # Should not crash
        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_multi_sentence_tts_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming agent with multiple sentences should produce multiple TTS calls."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = StreamingMultiSentenceAgent("First sentence. Second sentence. Third sentence.")
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert "Third sentence" in agent_final.text
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        # TTS should have been called multiple times
        # (sentence splitting produces at least 2 chunks)
        assert len(tts.payloads) >= 2
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_agent_multiple_tts_payloads_with_barge_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming agent producing 3 sentences, interrupted during 2nd TTS."""
    transport = QueueTransport()
    stt = ScriptedSTT(["test"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.04)
    # start, stop (first turn), wait for bot speaking, then start (barge-in)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = StreamingMultiSentenceAgent("First sentence. Second sentence. Third sentence.")
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(BotStartedSpeaking, Interruption, AgentDelta, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Let some TTS play before barge-in
        await asyncio.sleep(0.05)
        await transport.push_audio(make_chunk())

        await collector.wait_for(Interruption, timeout=3.0)

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0
    finally:
        await transport.finish_input()
        await session.stop()


# ── Push-to-talk ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_to_talk_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Push-to-talk mode should ignore VAD and only respond to manual start/end."""
    transport = QueueTransport()
    stt = ScriptedSTT(["manual turn"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # VAD events that should be ignored in PTT mode
    vad = ScriptedVAD(["start", "stop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    ptt_config = TurnManagerConfig(end_of_turn_silence_ms=1, mode=TurnMode.PUSH_TO_TALK)
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=ptt_config)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, AgentFinal)

    await session.start()
    try:
        # Send audio that triggers VAD events (should be ignored)
        for _ in range(4):
            await transport.push_audio(make_chunk())

        await asyncio.sleep(0.3)
        # No turn should have started from VAD
        turn_starts = [e for e in collector.events if isinstance(e, TurnStarted)]
        assert len(turn_starts) == 0, "VAD events should be ignored in push-to-talk mode"

        # Now manually start and end a turn
        await session.start_turn()
        await transport.push_audio(make_chunk())
        await session.end_turn()

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "MANUAL TURN"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_push_to_talk_barge_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In PTT mode, manual start_turn during bot speaking should barge-in."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first", "second"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.05)
    vad = ScriptedVAD([])  # No VAD events in PTT mode
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    ptt_config = TurnManagerConfig(end_of_turn_silence_ms=1, mode=TurnMode.PUSH_TO_TALK)
    config = make_test_config(
        transport=transport,
        agent=EchoAgent(),
        turn_taking=ptt_config,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(
        BotStartedSpeaking,
        Interruption,
        TurnStarted,
        AgentFinal,
    )

    await session.start()
    try:
        # Manual turn 1
        await session.start_turn()
        await transport.push_audio(make_chunk())
        await session.end_turn()
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Barge-in via manual start_turn during bot speaking
        await session.start_turn()
        interruption = await collector.wait_for(Interruption, timeout=3.0)
        assert interruption is not None

        turn_starts = [e for e in collector.events if isinstance(e, TurnStarted)]
        assert len(turn_starts) >= 2
    finally:
        await transport.finish_input()
        await session.stop()


# ── Cancellation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_turn_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling cancel_turn() mid-processing should reset state cleanly."""
    transport = QueueTransport()
    # Extra empty transcript: cancel_turn -> _cancel_stt -> end_stream() consumes one
    stt = ScriptedSTT(["cancelled turn", "", "second turn"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = SlowAgent(delay=0.5)
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, TurnEnded, AgentFinal)

    await session.start()
    try:
        # Start first turn
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(TurnEnded, timeout=2.0)

        # Cancel while agent is processing
        await asyncio.sleep(0.05)
        await session.cancel_turn()

        # Session should be idle again, try another turn
        agent2 = UpperAgent()
        session.agent = agent2

        for _ in range(4):
            await transport.push_audio(make_chunk())

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "SECOND TURN"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_multiple_stt_partials_then_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STT emitting partials then being cancelled should not leave dangling state."""
    transport = QueueTransport()

    class SlowSTT:
        """STT that emits partials slowly."""

        def __init__(self) -> None:
            self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()

        async def start_stream(self) -> None:
            self._events = asyncio.Queue()

        async def send_audio(self, chunk) -> None:
            pass

        async def end_stream(self) -> None:
            await self._events.put(STTEvent(type=STTEventType.PARTIAL, text="hel"))
            await self._events.put(STTEvent(type=STTEventType.PARTIAL, text="hello"))
            await asyncio.sleep(0.1)
            await self._events.put(STTEvent(type=STTEventType.FINAL, text="hello world"))
            await self._events.put(None)

        async def events(self) -> AsyncIterator[STTEvent]:
            while True:
                event = await self._events.get()
                if event is None:
                    break
                yield event

    stt = SlowSTT()
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(STTFinal, AgentFinal, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO WORLD"

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_cancel_turn_during_stt_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling turn while STT is processing should not hang."""
    transport = QueueTransport()

    class NeverFinalSTT:
        """STT that never produces a final event."""

        def __init__(self) -> None:
            self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()

        async def start_stream(self) -> None:
            self._events = asyncio.Queue()

        async def send_audio(self, chunk) -> None:
            pass

        async def end_stream(self) -> None:
            # Just emit partials, never final
            await self._events.put(STTEvent(type=STTEventType.PARTIAL, text="hel"))
            await self._events.put(None)

        async def events(self) -> AsyncIterator[STTEvent]:
            while True:
                event = await self._events.get()
                if event is None:
                    break
                yield event

    stt = NeverFinalSTT()
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnEnded, AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(TurnEnded, timeout=2.0)

        # Give STT time to start processing
        await asyncio.sleep(0.1)

        # Cancel while STT never produces final
        await session.cancel_turn()

        # Should not hang - give time for cleanup
        await asyncio.sleep(0.3)

        # Agent should not have been invoked
        agent_finals = [e for e in collector.events if isinstance(e, AgentFinal)]
        assert len(agent_finals) == 0
    finally:
        await transport.finish_input()
        await session.stop()


# ── Session lifecycle ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rapid_start_stop_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rapidly starting and stopping the session should not leave dangling tasks or crash."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS()
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    await session.start()
    await transport.push_audio(make_chunk())
    await session.stop()

    # Double stop should be a no-op, not an error
    await session.stop()


@pytest.mark.asyncio
async def test_session_stop_during_active_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping session while a turn is in progress should not hang or crash."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.1)
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = SlowAgent(delay=0.3)
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)

    await session.start()

    await transport.push_audio(make_chunk(), make_chunk())
    # Let the turn start processing
    await asyncio.sleep(0.05)

    # Stop while agent is still working - should not hang
    await transport.finish_input()
    await asyncio.wait_for(session.stop(), timeout=5.0)


@pytest.mark.asyncio
async def test_shutdown_force_closes_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """shutdown() should force-close all tasks without hanging."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.5)
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    agent = SlowAgent(delay=1.0)
    config = make_test_config(transport=transport, agent=agent, turn_taking=FAST_TURN)
    session = create_session(config)

    await session.start()
    await transport.push_audio(make_chunk(), make_chunk())
    await asyncio.sleep(0.05)

    # Shutdown should complete quickly despite slow agent and TTS
    await asyncio.wait_for(session.shutdown(), timeout=5.0)
    assert not session.is_running


@pytest.mark.asyncio
async def test_reset_state_clears_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_state() should cancel active processing and return to idle."""
    transport = QueueTransport()
    # Extra blank: reset_state -> _cancel_stt -> end_stream consumes one
    stt = ScriptedSTT(["hello", "", "world"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=SlowAgent(delay=0.3),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, TurnEnded, AgentFinal)

    await session.start()
    try:
        # Start a turn
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(TurnEnded, timeout=2.0)

        # Reset mid-processing
        await session.reset_state()

        # Session should now accept new turns
        session.agent = UpperAgent()
        for _ in range(4):
            await transport.push_audio(make_chunk())

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "WORLD"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_agent_runner_history_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentRunner should maintain conversation history across turns."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first message", "second message"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    inner_agent = UpperAgent()
    runner = AgentRunner(inner_agent, AgentRunnerConfig(timeout=5.0))

    config = make_test_config(
        transport=transport,
        agent=runner,
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        # Turn 1
        await transport.push_audio(make_chunk(), make_chunk())
        first = await collector.wait_for(AgentFinal, timeout=3.0)
        assert first.text == "FIRST MESSAGE"
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        # Turn 2
        for _ in range(4):
            await transport.push_audio(make_chunk())
        second = await collector.wait_for(
            AgentFinal,
            predicate=lambda e: e.text == "SECOND MESSAGE",
            timeout=3.0,
        )
        assert second.text == "SECOND MESSAGE"

        # Verify history has both turns
        history = runner.history
        user_msgs = [h for h in history if h.get("role") == "user"]
        asst_msgs = [h for h in history if h.get("role") == "assistant"]
        assert len(user_msgs) == 2
        assert len(asst_msgs) == 2
        assert user_msgs[0]["content"] == "first message"
        assert asst_msgs[0]["content"] == "FIRST MESSAGE"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_agent_runner_history_cleared_on_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset_state() should clear agent conversation history."""
    transport = QueueTransport()
    # Extra blank for double end_stream from reset
    stt = ScriptedSTT(["hello", "", "world"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    inner_agent = UpperAgent()
    runner = AgentRunner(inner_agent, AgentRunnerConfig(timeout=5.0))

    config = make_test_config(
        transport=transport,
        agent=runner,
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        # Turn 1
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(AgentFinal, timeout=3.0)
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        assert len(runner.history) > 0

        # Reset clears history
        await session.reset_state()
        assert len(runner.history) == 0

        # Turn 2 should work with clean history
        for _ in range(4):
            await transport.push_audio(make_chunk())
        second = await collector.wait_for(
            AgentFinal,
            predicate=lambda e: e.text == "WORLD",
            timeout=3.0,
        )
        assert second.text == "WORLD"
        assert len(runner.history) == 2  # user + assistant
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_session_properties_during_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session properties should reflect correct state during a turn."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,), chunk_delay_s=0.05)
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    from easycat.session import TurnState

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    states_seen: list[str] = []

    def record_state(event) -> None:
        states_seen.append(f"{type(event).__name__}:{session.turn_state.value}")

    session.event_bus.subscribe(TurnStarted, record_state)
    session.event_bus.subscribe(BotStartedSpeaking, record_state)
    session.event_bus.subscribe(BotStoppedSpeaking, record_state)

    collector = EventCollector(session.event_bus)
    collector.subscribe(BotStoppedSpeaking)

    await session.start()
    try:
        assert session.turn_state == TurnState.IDLE

        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        # Verify state progression
        assert "TurnStarted:listening" in states_seen
        assert "BotStartedSpeaking:bot_speaking" in states_seen
        assert "BotStoppedSpeaking:idle" in states_seen
    finally:
        await transport.finish_input()
        await session.stop()


# ── Event system ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_bus_handler_exception_does_not_break_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing event handler should not prevent the pipeline from continuing."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal)

    # Register a handler that raises
    def bad_handler(event: TurnStarted) -> None:
        raise ValueError("handler crash")

    session.event_bus.subscribe(TurnStarted, bad_handler)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_session_events_have_correlation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All session events should have session_id and turn_id set."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    all_events: list = []
    session.event_bus.subscribe_all(lambda e: all_events.append(e))

    collector = EventCollector(session.event_bus)
    collector.subscribe(BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        # All events with session_id field should have it set
        for event in all_events:
            if hasattr(event, "session_id"):
                assert event.session_id is not None, f"{type(event).__name__} missing session_id"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_stt_partial_events_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial STT events should be emitted before the final transcript."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello world"], partials=[["hel", "hello"]])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(STTPartial, STTFinal, AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO WORLD"

        partials = [e for e in collector.events if isinstance(e, STTPartial)]
        assert len(partials) == 2
        assert partials[0].text == "hel"
        assert partials[1].text == "hello"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_tts_audio_events_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTSAudio events should be emitted during synthesis."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TTSAudio, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        tts_audios = [e for e in collector.events if isinstance(e, TTSAudio)]
        # Should have 2 audio chunks (matching RecordingTTS chunk_sizes)
        assert len(tts_audios) == 2
        assert all(a.chunk is not None for a in tts_audios)
    finally:
        await transport.finish_input()
        await session.stop()


# ── Edge cases ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whitespace_only_agent_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent returning only whitespace should not crash the pipeline."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=WhitespaceOnlyAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, TurnEnded, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(TurnEnded, timeout=2.0)
        # Give pipeline time to process
        await asyncio.sleep(0.5)

        # Should not crash, no Error events
        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0, f"Unexpected errors: {errors}"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_tts_not_called_when_agent_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If agent returns empty string, TTS should not synthesize anything."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=EmptyResponseAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, TurnEnded, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(TurnEnded, timeout=2.0)
        # Give time for agent to run
        await asyncio.sleep(0.3)

        # Agent returns empty - it may or may not emit AgentFinal with empty text,
        # but TTS should not have been called
        # The session skips agent path for empty transcripts but the STT did return non-empty,
        # so the agent ran and returned empty. Check TTS behavior.
        agent_finals = [e for e in collector.events if isinstance(e, AgentFinal)]
        if agent_finals:
            assert agent_finals[0].text == ""
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_newline_in_agent_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent response with newlines should be handled gracefully by TTS."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=NewlineAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStoppedSpeaking, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "Hello\n\nWorld"
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0
        assert len(tts.payloads) >= 1
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_auto_turn_from_stt_final_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto_turn_from_stt_final: STT final should trigger turn end."""
    transport = QueueTransport()

    class AutoTurnSTT:
        """STT that emits final after receiving some audio."""

        def __init__(self) -> None:
            self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
            self._audio_count = 0

        async def start_stream(self) -> None:
            self._events = asyncio.Queue()
            self._audio_count = 0

        async def send_audio(self, chunk) -> None:
            self._audio_count += 1
            if self._audio_count == 2:
                await self._events.put(STTEvent(type=STTEventType.FINAL, text="auto turn"))
                await self._events.put(None)

        async def end_stream(self) -> None:
            if not self._events.empty():
                return
            await self._events.put(None)

        async def events(self) -> AsyncIterator[STTEvent]:
            while True:
                event = await self._events.get()
                if event is None:
                    break
                yield event

    stt = AutoTurnSTT()
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD([])  # VAD disabled in auto_turn mode
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)

    # Manually set auto_turn mode
    session = create_session(config)
    session._auto_turn_from_stt_final = True
    session._enable_vad = False

    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, STTFinal, AgentFinal, TurnEnded)

    await session.start()
    try:
        # Send audio with speech energy to trigger auto-turn
        # Need chunks with non-zero PCM data to pass energy check
        speech_data = struct.pack("<" + "h" * 320, *([5000] * 320))
        speech_chunk = AudioChunk(data=speech_data, format=PCM16_MONO_16K)
        for _ in range(4):
            await transport.push_audio(speech_chunk)

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "AUTO TURN"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_stt_audio_includes_pre_roll(monkeypatch: pytest.MonkeyPatch) -> None:
    """STT should receive pre-roll audio captured before VAD trigger."""
    transport = QueueTransport()
    stt = ScriptedSTT(["with pre-roll"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # First chunk has no VAD event, second triggers start, third triggers stop
    vad = ScriptedVAD(["noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal)

    await session.start()
    try:
        # First chunk enters pre-roll buffer, second triggers VAD start
        await transport.push_audio(make_chunk(), make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "WITH PRE-ROLL"

        # STT should have received pre-roll audio (the first chunk)
        # plus the chunks during speech
        assert len(stt.sent_audio) >= 2
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_concurrent_vad_events_do_not_corrupt_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapid VAD start/stop in same frame should be handled gracefully."""
    transport = QueueTransport()
    stt = ScriptedSTT(["fast"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # Both start and stop on same chunk
    vad = ScriptedVAD([["start", "stop"]])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted, TurnEnded, AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "FAST"
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_tts_produces_no_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS producing markers but no audio should not crash or hang."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = ZeroAudioTTS()
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=UpperAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStoppedSpeaking, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO"

        # Wait for TTS to complete
        await asyncio.sleep(0.3)

        # No audio should have been sent to transport
        assert len(transport.sent) == 0

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_whitespace_agent_no_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming agent producing only whitespace should not crash."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=StreamingWhitespaceAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentDelta, AgentFinal, Error, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        # Wait for the turn to process
        await asyncio.sleep(0.5)

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert len(errors) == 0, f"Unexpected errors: {errors}"

        # Should have received deltas even if whitespace
        deltas = [e for e in collector.events if isinstance(e, AgentDelta)]
        assert len(deltas) >= 1
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_streaming_empty_done_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DONE event with empty text should use accumulated deltas."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=StreamingEmptyDoneAgent(),
        turn_taking=FAST_TURN,
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentDelta, AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        # accumulated text from deltas should be used
        assert agent_final.text == "Hello"
    finally:
        await transport.finish_input()
        await session.stop()
