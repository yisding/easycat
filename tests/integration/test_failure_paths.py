"""Tests for production error-handling paths using enhanced harness fakes."""

from __future__ import annotations

import asyncio

import pytest

from easycat import create_session
from easycat.events import (
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    Interruption,
)
from easycat.turn_manager import TurnManagerConfig

from .harness import (
    EventCollector,
    FailingNoiseReducer,
    QueueTransport,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    make_chunk,
    make_test_config,
    patch_provider_factories,
)

pytestmark = pytest.mark.integration_local

FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


# ── Test agents ──────────────────────────────────────────────────────


class UpperAgent:
    """Simple agent that uppercases input."""

    async def run(self, text: str) -> str:
        return text.upper()


class FailingAgent:
    """Agent that always raises."""

    async def run(self, text: str) -> str:
        raise RuntimeError("agent exploded")


class InterruptibleAgent:
    """Agent with a notify_interruption method that raises."""

    async def run(self, text: str) -> str:
        return text.upper()

    def notify_interruption(self, text_spoken: str, *, mode: str = "truncate") -> None:
        raise RuntimeError("notify boom")


# ── STT start failure ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stt_start_failure_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When STT.start_stream raises, an Error event should be emitted
    and the session should stay running."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"], fail_on_start=RuntimeError("stt boom"))
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error, AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        error_event = await collector.wait_for(Error, timeout=3.0)
        assert "stt" in error_event.context
        assert isinstance(error_event.exception, RuntimeError)
        assert session.is_running

        # Agent should NOT have run since STT never started
        await asyncio.sleep(0.2)
        agent_finals = [e for e in collector.events if isinstance(e, AgentFinal)]
        assert len(agent_finals) == 0
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_stt_start_failure_allows_next_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """After an STT start failure, replacing the STT provider allows the
    next turn to complete normally."""
    transport = QueueTransport()
    failing_stt = ScriptedSTT(["hello"], fail_on_start=RuntimeError("stt boom"))
    tts = RecordingTTS(chunk_sizes=(640,))
    # Four VAD actions: start/stop for first turn, start/stop for second turn.
    # Pad with noops between turns so second turn audio triggers events 3 and 4.
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=failing_stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error, AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        # First turn: STT fails on start
        await transport.push_audio(make_chunk(), make_chunk())
        error_event = await collector.wait_for(Error, timeout=3.0)
        assert "stt" in error_event.context

        # Wait for first turn to settle
        await asyncio.sleep(0.2)

        # Replace session's STT with a working one for the second turn
        working_stt = ScriptedSTT(["second turn"])
        session.stt = working_stt

        # Second turn: push audio to trigger next VAD start/stop
        for _ in range(4):
            await transport.push_audio(make_chunk())

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "SECOND TURN"
    finally:
        await transport.finish_input()
        await session.stop()


# ── Transport send failure ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_transport_send_failure_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """When transport.send_audio raises, the turn should still complete
    (AgentFinal fires) and the session should not crash."""
    transport = QueueTransport(fail_on_send=OSError("socket closed"))
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO"
        assert session.is_running
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_transport_intermittent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport that fails after N successful sends should not crash the
    session. The session should survive and can be stopped cleanly."""
    transport = QueueTransport(fail_on_send=OSError("transient"), fail_after_n_sends=2)
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO"
        assert session.is_running
        # First 2 sends succeeded before the failure mode kicks in
        assert len(transport.sent) == 2
    finally:
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)


# ── Noise reducer failure ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noise_reducer_failure_stops_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """When noise reduction raises on the first chunk, the pipeline should
    emit an Error event with context 'pipeline' and the session can be stopped."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    noise_reducer = FailingNoiseReducer(fail_on_chunk=0)
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad, noise_reducer=noise_reducer)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk())
        error_event = await collector.wait_for(Error, timeout=3.0)
        assert "pipeline" in error_event.context
        assert isinstance(error_event.exception, RuntimeError)
    finally:
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)


# ── VAD failure ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vad_failure_stops_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """When VAD raises on the second chunk, the pipeline should emit an
    Error event and the session can be stopped."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(
        ["start"],
        fail_on_chunk=1,
        fail_with=RuntimeError("vad crash"),
    )
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        error_event = await collector.wait_for(Error, timeout=3.0)
        assert "pipeline" in error_event.context
        assert isinstance(error_event.exception, RuntimeError)
        assert "vad crash" in str(error_event.exception)
    finally:
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)


# ── TTS failure ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tts_synthesize_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TTS.synthesize raises, the error is logged and the session
    continues. AgentFinal still fires because the agent completed
    before TTS attempted synthesis."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(fail_on_synthesize=RuntimeError("tts boom"))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error, AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        # Agent should complete even though TTS fails
        await collector.wait_for(AgentFinal, timeout=3.0)

        # Wait a bit for the TTS error path to complete
        await asyncio.sleep(0.3)

        # The TTS error is logged (not emitted as Error event) and the
        # session survives. No audio should have been sent to transport.
        assert session.is_running
        assert len(transport.sent) == 0
    finally:
        await transport.finish_input()
        await session.stop()


# ── TTS cancel failure on barge-in ───────────────────────────────────


@pytest.mark.asyncio
async def test_tts_cancel_failure_on_barge_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TTS.cancel() raises during barge-in, the session should not
    hang and should be stoppable. The TTSSynthesizer.cancel() wraps the
    call in try/except, so the error is swallowed."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first", "second"])
    tts = RecordingTTS(
        chunk_sizes=(640, 640, 640),
        chunk_delay_s=0.05,
        fail_on_cancel=RuntimeError("cancel boom"),
    )
    # First turn: start, stop. Then barge-in: start, stop.
    vad = ScriptedVAD(["start", "stop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(BotStartedSpeaking, Interruption, Error, AgentFinal)

    await session.start()
    try:
        # First turn
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Barge-in during bot speaking
        await transport.push_audio(make_chunk(), make_chunk())

        # The cancel error should be swallowed by TTSSynthesizer.cancel()
        interruption = await collector.wait_for(Interruption, timeout=3.0)
        assert interruption is not None

        # Session should still be alive
        assert session.is_running
    finally:
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)


# ── Agent notify_interruption failure ────────────────────────────────


@pytest.mark.asyncio
async def test_agent_notify_interruption_failure_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When agent.notify_interruption raises, the exception should be
    swallowed (session.py:1886) and the session should continue."""
    transport = QueueTransport()
    stt = ScriptedSTT(["first", "second"])
    tts = RecordingTTS(
        chunk_sizes=(640, 640, 640),
        chunk_delay_s=0.05,
    )
    # First turn: start, stop. Then barge-in: start, stop.
    vad = ScriptedVAD(["start", "stop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport, agent=InterruptibleAgent(), turn_taking=FAST_TURN
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(BotStartedSpeaking, Interruption, AgentFinal)

    await session.start()
    try:
        # First turn
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)

        # Barge-in triggers notify_interruption which raises
        await transport.push_audio(make_chunk(), make_chunk())

        interruption = await collector.wait_for(Interruption, timeout=3.0)
        assert interruption is not None

        # Session should survive despite the exception in notify_interruption
        assert session.is_running
    finally:
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)


# ── Multiple error types ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_error_types_session_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session should survive multiple different error types across turns:
    first turn has transport send failure, second turn has agent failure."""
    transport = QueueTransport(fail_on_send=OSError("socket closed"))
    stt = ScriptedSTT(["first", "trigger error"])
    tts = RecordingTTS(chunk_sizes=(640,))
    # Two turns: start/stop, noop noop, start/stop
    vad = ScriptedVAD(["start", "stop", "noop", "noop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    # First turn uses UpperAgent (succeeds), then we swap to FailingAgent
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(Error, AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        # First turn: agent succeeds but transport send fails (swallowed)
        await transport.push_audio(make_chunk(), make_chunk())
        first_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert first_final.text == "FIRST"

        # Wait for first turn to fully finish
        await asyncio.sleep(0.3)
        assert session.is_running

        # Swap agent to a failing one for the second turn
        session.agent = FailingAgent()

        # Second turn: agent fails
        for _ in range(4):
            await transport.push_audio(make_chunk())

        error_event = await collector.wait_for(
            Error,
            predicate=lambda e: "agent" in e.context,
            timeout=3.0,
        )
        assert isinstance(error_event.exception, RuntimeError)
        assert "agent" in error_event.context
        assert session.is_running
    finally:
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)
