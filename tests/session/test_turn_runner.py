"""Focused unit tests for :class:`TurnRunner`.

These tests exercise the runner via its host Session and verify the
load-bearing per-turn agent loop invariants that survived the Phase 5
decomposition.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

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
from easycat.session._turn_context import TurnContext
from easycat.session._turn_runner import TurnRunner
from easycat.session._types import SessionConfig
from easycat.session.actions import SessionActions
from easycat.timeouts import TimeoutConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig
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
