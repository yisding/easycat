"""Focused unit tests for :class:`TurnRunner`.

These tests exercise the runner via its host Session and verify the
load-bearing per-turn agent loop invariants that survived the Phase 5
decomposition.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from easycat._log_context import CorrelationFilter, bind_turn
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
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSEvent,
    TTSEventType,
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
from easycat.session._session import Session
from easycat.session._turn_runner import TurnRunner
from easycat.session._types import SessionConfig
from easycat.session.actions import SessionActions
from easycat.timeouts import TimeoutConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig, TurnManagerState
from tests._bridge_helpers import _TestBridgeBase

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


# ── Test fakes (mirrors test_session_streaming.py) ────────────────


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
    def __init__(
        self,
        transcript: str = "hello world",
        *,
        fail_on_start: bool = False,
        fail_on_send: bool = False,
    ) -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._fail_on_start = fail_on_start
        self._fail_on_send = fail_on_send
        self.end_stream_calls = 0
        self.stream_open = False

    async def start_stream(self) -> None:
        if self._fail_on_start:
            raise RuntimeError("STT start_stream failed")
        self.stream_open = True

    async def send_audio(self, chunk: AudioChunk) -> None:
        if self._fail_on_send:
            raise RuntimeError("STT send_audio failed")

    async def end_stream(self) -> None:
        self.end_stream_calls += 1
        self.stream_open = False
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


class _SimpleStreamingAgent(_TestBridgeBase):
    """Streams one sentence followed by done."""

    async def run(self, text: str) -> str:
        return "Reply."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder
        yield AgentBridgeEvent(kind="text_delta", text="Reply.")
        yield AgentBridgeEvent(kind="done", text="Reply.")


def _config(**overrides) -> SessionConfig:
    base = dict(
        transport=FakeTransport(),
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hello"),
        agent=_SimpleStreamingAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
    )
    base.update(overrides)
    return SessionConfig(**base)


def _current_turn_log_context() -> str:
    record = logging.LogRecord(
        name="easycat.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )
    CorrelationFilter().filter(record)
    return str(record.turn_id)


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_runner_constructed_with_session() -> None:
    """Session installs a TurnRunner instance under ``_turn_runner``."""
    session = Session(_config())
    assert isinstance(session._turn_runner, TurnRunner)
    # The runner's subscription handlers are bound to the bus.
    handlers = session.event_bus._handlers.get(TurnStarted, [])
    bound_names = [getattr(h, "__qualname__", "") for h in handlers]
    assert any("TurnRunner.on_turn_started" in name for name in bound_names)


@pytest.mark.asyncio
async def test_on_turn_started_creates_turn_context() -> None:
    """``on_turn_started`` should install a fresh TurnContext on Session."""
    session = Session(_config())
    # Mark the session as 'running' so the gate passes.
    session._is_running = True
    runner = session._turn_runner
    assert session._turn is None

    await runner.on_turn_started(TurnStarted(turn_id="t-1"))

    assert session._turn is not None
    assert session._turn.id == "t-1"
    assert session._stt_committer.is_active


@pytest.mark.asyncio
async def test_on_turn_started_does_not_leave_task_bound_to_turn() -> None:
    """Turn startup tags its own logs but does not leak into later task logs."""
    bind_turn(None)
    session = Session(_config())
    session._is_running = True

    await session._turn_runner.on_turn_started(TurnStarted(turn_id="t-context"))

    assert session._turn is not None
    assert session._turn.id == "t-context"
    assert _current_turn_log_context() == "-"


@pytest.mark.asyncio
async def test_on_turn_started_start_failure_tears_down_turn() -> None:
    """If ``start_stream`` fails, the turn is fully torn down.

    The turn pointer is reset, STT is marked inactive, no consumer task
    is left pending, and the FSM is returned to IDLE — instead of leaking
    a half-started turn that only the silence timeout could unstick.
    """
    stt = FakeSTT(transcript="hello", fail_on_start=True)
    session = Session(_config(stt=stt))
    session._is_running = True
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    await session._turn_runner.on_turn_started(TurnStarted(turn_id="t-fail"))

    assert session._turn is None
    assert not session._stt_committer.is_active
    assert session._stt_committer.stt_task is None
    assert session._turn_manager.state == TurnManagerState.IDLE
    assert any(e.stage == ErrorStage.STT for e in errors)


@pytest.mark.asyncio
async def test_on_turn_started_preroll_failure_tears_down_and_closes_stream() -> None:
    """A failure while priming pre-roll closes the (open) stream and tears down.

    ``start_stream`` succeeds and opens the stream; the failure occurs
    while feeding pre-roll frames. The except path must close the stream
    via ``end_stream`` and leave no orphaned consumer task — the consumer
    loop is now started only *after* priming succeeds.
    """
    stt = FakeSTT(transcript="hello", fail_on_send=True)
    session = Session(_config(stt=stt))

    # Populate pre-roll while not running so the bus-driven on_turn_started
    # short-circuits; start_turn() flushes pre-roll into turn_audio.
    session._is_running = False
    session._turn_manager.on_audio_frame(_chunk())
    await session._turn_manager.start_turn()
    assert session._turn_manager.turn_audio  # pre-roll captured

    session._is_running = True
    await session._turn_runner.on_turn_started(TurnStarted(turn_id="t-preroll"))

    assert session._turn is None
    assert not session._stt_committer.is_active
    assert session._stt_committer.stt_task is None
    # The stream that start_stream() opened must be closed on teardown.
    assert stt.end_stream_calls >= 1
    assert stt.stream_open is False
    assert session._turn_manager.state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_handle_end_of_speech_empty_transcript_resets() -> None:
    """An empty transcript should skip agent dispatch and reset the turn."""
    session = Session(_config(stt=FakeSTT(transcript="")))
    session._is_running = True
    runner = session._turn_runner
    await runner.on_turn_started(TurnStarted(turn_id="t-empty"))
    turn = session._turn
    assert turn is not None
    # Simulate user finished speaking with no transcript.
    await runner.handle_end_of_speech(turn=turn)
    # Turn pointer is reset.
    assert session._turn is None


@pytest.mark.asyncio
async def test_handle_end_of_speech_dispatches_agent_with_transcript() -> None:
    """A non-empty transcript should produce AgentFinal via the streaming path."""
    session = Session(_config())
    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    runner = session._turn_runner
    session._turn = TurnContext("turn-x", CancelToken())
    session._turn.append_stt_segment("hello world")
    await runner.handle_end_of_speech(turn=session._turn)

    assert len(finals) == 1
    assert finals[0].text == "Reply."


@pytest.mark.asyncio
async def test_run_streaming_agent_happy_path_emits_final_and_synthesizes() -> None:
    tts = FakeTTS()
    session = Session(_config(tts=tts))
    finals: list[AgentFinal] = []
    deltas: list[AgentDelta] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))
    session.event_bus.subscribe(AgentDelta, lambda e: deltas.append(e))

    session._turn = TurnContext("turn-happy", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert [delta.text for delta in deltas] == ["Reply."]
    assert len(finals) == 1
    assert finals[0].text == "Reply."
    assert tts.synthesized_texts == ["Reply."]


@pytest.mark.asyncio
async def test_run_streaming_agent_action_drain_triggers_stop() -> None:
    """A drained EndCallAction must call ``session.stop()`` once."""
    actions = SessionActions()
    actions.end_call(reason="done")
    session = Session(_config(session_actions=actions))
    session.stop = AsyncMock()
    session._turn = TurnContext("turn-stop", CancelToken())

    await session._turn_runner.run_streaming_agent("hello", token=None)

    session.stop.assert_awaited_once()


class _Gate:
    """Stateful classification gate: buffering until flushed."""

    def __init__(self) -> None:
        self.buffering = True

    def __call__(self) -> bool:
        return self.buffering


class _GateFlushingTTS(FakeTTS):
    """Flips the gate to flushed right after synthesizing a payload.

    Models the answering-machine/human classifier completing mid-turn:
    ``_is_gated()`` is True when the first TTS payload is snapshotted but
    False by the time the post-loop branch runs.
    """

    def __init__(self, gate: _Gate) -> None:
        super().__init__()
        self._gate = gate

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        async for event in super().synthesize(payload):
            yield event
        self._gate.buffering = False


@pytest.mark.asyncio
async def test_gated_opener_preserves_turn_when_gate_flushes_mid_synthesis() -> None:
    """Regression: a gated opener whose gate flushes mid-synthesis must
    keep ``session._turn`` alive for gated-replay mark accounting.

    ``_process_tts`` snapshots ``gated`` at first-payload time; re-reading
    ``_is_gated()`` live in the post-loop branch would (after the gate
    flushed) take the ``_reset_turn_state()`` path and null the turn
    pointer the gated replay still needs.
    """
    gate = _Gate()
    tts = _GateFlushingTTS(gate)
    session = Session(_config(tts=tts, audio_gate=gate))
    turn = TurnContext("turn-gated", CancelToken())
    session._turn = turn

    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert tts.synthesized_texts == ["Reply."]  # synthesis ran while gated
    assert gate.buffering is False  # gate flushed mid/post synthesis
    # The turn pointer must survive for gated replay mark accounting.
    assert session._turn is turn


class _CancelMidStreamAgent(_TestBridgeBase):
    """Streams a delta and waits forever — barge-in is observed when cancelled."""

    async def run(self, text: str) -> str:
        return ""

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder
        yield AgentBridgeEvent(kind="text_delta", text="Hello. ")
        try:
            while True:
                if cancel_token and cancel_token.is_cancelled:
                    return
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise


@pytest.mark.asyncio
async def test_barge_in_cancels_streaming_agent() -> None:
    """A cancel-token cancel mid-stream tears down both agent and TTS tasks."""
    tts = FakeTTS()
    session = Session(_config(agent=_CancelMidStreamAgent(), tts=tts))
    token = CancelToken()
    session._turn = TurnContext("turn-barge", token)

    task = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=token))
    # Let the agent stream a bit.
    await asyncio.sleep(0.05)
    token.cancel()
    await task
    # No exception, and the agent task is no longer pending.


class _RaisingAgent(_TestBridgeBase):
    async def run(self, text: str) -> str:
        raise RuntimeError("boom")

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        raise RuntimeError("boom")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_agent_failure_emits_error_event() -> None:
    session = Session(_config(agent=_RaisingAgent()))
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    session._turn = TurnContext("turn-err", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert any(e.stage == ErrorStage.AGENT for e in errors)


@pytest.mark.asyncio
async def test_send_text_runs_agent_without_audio() -> None:
    """``send_text`` runs the agent loop without touching audio I/O."""
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
        )
    )
    response = await session.send_text("hello")
    assert response == "Reply."


@pytest.mark.asyncio
async def test_send_text_dispatches_tool_events_with_correlation() -> None:
    """The text path translates tool events via the shared dispatch.

    Regression guard for the de-duplication of the bridge-event switch:
    ``_execute_text_turn`` now routes tool_started/tool_delta/tool_result
    through ``emit_tool_event`` (the same translation the voice path uses),
    so the text path must still surface ``ToolCallStarted`` /
    ``ToolCallDelta`` / ``ToolCallResult`` stamped with the text turn's
    session_id and turn_id.
    """

    class _ToolStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Done."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            yield AgentBridgeEvent(kind="tool_started", tool_name="lookup", call_id="c1")
            yield AgentBridgeEvent(kind="tool_delta", text="partial", call_id="c1")
            yield AgentBridgeEvent(kind="tool_result", result="42", call_id="c1")
            yield AgentBridgeEvent(kind="text_delta", text="Done.")
            yield AgentBridgeEvent(kind="done", text="Done.")

    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_ToolStreamingAgent(),
        )
    )
    started: list[ToolCallStarted] = []
    deltas: list[ToolCallDelta] = []
    results: list[ToolCallResult] = []
    session.event_bus.subscribe(ToolCallStarted, lambda e: started.append(e))
    session.event_bus.subscribe(ToolCallDelta, lambda e: deltas.append(e))
    session.event_bus.subscribe(ToolCallResult, lambda e: results.append(e))

    response = await session.send_text("hello")
    assert response == "Done."

    assert len(started) == 1
    assert started[0].tool_name == "lookup"
    assert started[0].call_id == "c1"
    assert len(deltas) == 1
    assert deltas[0].call_id == "c1"
    assert deltas[0].delta == "partial"
    assert len(results) == 1
    assert results[0].call_id == "c1"
    assert results[0].result == "42"

    # Every tool event must carry the text turn's correlation ids — the
    # text path runs outside the TurnManager's active-turn window, so the
    # ids are stamped by the dispatch rather than by Session._with_correlation.
    for event in (started[0], deltas[0], results[0]):
        assert event.session_id == session.session_id
        assert event.turn_id is not None
        assert event.turn_id.startswith("turn-")


@pytest.mark.asyncio
async def test_send_text_clears_turn_log_context_after_turn() -> None:
    """The caller task should not keep the text turn id after send_text returns."""
    bind_turn(None)
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
        )
    )

    assert await session.send_text("hello") == "Reply."
    assert _current_turn_log_context() == "-"


@pytest.mark.asyncio
async def test_tts_consumer_starts_before_agent_consumer() -> None:
    """The TTS consumer must observe ``BotStartedSpeaking`` before AgentFinal."""
    started: list[str] = []

    session = Session(_config())
    session.event_bus.subscribe(BotStartedSpeaking, lambda _e: started.append("bot"))
    session.event_bus.subscribe(AgentFinal, lambda _e: started.append("final"))

    session._turn = TurnContext("turn-ord", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)
    # The TTS consumer fires bot_started_speaking on the first non-cancelled
    # payload, which happens before the agent's final event is emitted.
    assert "bot" in started
    assert started.index("bot") < started.index("final")


@pytest.mark.asyncio
async def test_run_streaming_agent_emits_bot_stopped_after_drain() -> None:
    """``bot_stopped_speaking`` must be emitted after drain when not stopping."""
    stopped: list[BotStoppedSpeaking] = []
    session = Session(_config())
    session.event_bus.subscribe(BotStoppedSpeaking, lambda e: stopped.append(e))
    session._turn = TurnContext("turn-stop2", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)
    assert len(stopped) == 1


@pytest.mark.asyncio
async def test_agent_timeout_emits_error() -> None:
    """Hitting the agent timeout emits an Error event."""

    class _SlowAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return ""

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            await asyncio.sleep(0.5)
            yield AgentBridgeEvent(kind="done", text="")

    session = Session(
        _config(
            agent=AgentRunner(_SlowAgent()),
            timeout_config=TimeoutConfig(agent_timeout=0.05),
        )
    )
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    session._turn = TurnContext("turn-timeout", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)
    assert any(e.stage == ErrorStage.AGENT for e in errors)
