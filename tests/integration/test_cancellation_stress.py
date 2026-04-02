"""Stress tests for cancellation, barge-in, and shutdown under load."""

from __future__ import annotations

import asyncio

import pytest

from easycat import create_session
from easycat.events import (
    Error,
    TurnEnded,
    TurnStarted,
)
from easycat.turn_manager import TurnManagerConfig

from .harness import (
    EventCollector,
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
    async def run(self, text: str) -> str:
        return text.upper()


class SlowAgent:
    """Agent that takes a configurable delay before responding."""

    def __init__(self, delay: float = 0.5) -> None:
        self._delay = delay

    async def run(self, text: str) -> str:
        await asyncio.sleep(self._delay)
        return text.upper()


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rapid_barge_in_stress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rapid alternating VAD start/stop with slow TTS should not crash the session.

    Pushes 20 audio chunks through a VAD script of 20 alternating start/stop
    events and verifies the session stays alive with no unhandled errors.
    """
    transport = QueueTransport()
    # Provide enough transcripts for however many turns complete.
    stt = ScriptedSTT([f"turn{i}" for i in range(10)])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640), chunk_delay_s=0.03)

    # 20 alternating start/stop events — some will land during BOT_SPEAKING
    # and trigger barge-in; others will start normal turns.
    vad_script: list[str] = []
    for _ in range(10):
        vad_script.extend(["start", "stop"])
    vad = ScriptedVAD(vad_script)

    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    collector = EventCollector(session.event_bus)
    collector.subscribe(Error, TurnStarted)

    await session.start()
    try:
        # Push 20 audio chunks — one per VAD script entry.
        for _ in range(20):
            await transport.push_audio(make_chunk())
            # Small sleep to let the pipeline process each chunk.
            await asyncio.sleep(0.01)

        # Give the pipeline time to settle after all chunks.
        await asyncio.sleep(0.5)

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert errors == [], f"unexpected errors: {errors}"
        assert session.is_running
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_concurrent_cancel_and_new_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling cancel_turn() while new audio triggers a VAD start should not corrupt state.

    Runs 3 iterations: complete a turn, then race cancel_turn() against a new
    VAD start.  The session must remain stable after each iteration.
    """
    transport = QueueTransport()
    stt = ScriptedSTT([f"iter{i}" for i in range(6)])
    tts = RecordingTTS(chunk_sizes=(640,), chunk_delay_s=0.01)

    # Each iteration needs: start, stop (completes a turn), start (new turn
    # that races with cancel_turn), stop (finish the interrupted turn).
    vad_script: list[str] = []
    for _ in range(3):
        vad_script.extend(["start", "stop", "start", "stop"])
    vad = ScriptedVAD(vad_script)

    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnEnded, Error)

    await session.start()
    try:
        for i in range(3):
            # Push audio to trigger start + stop (one complete turn).
            await transport.push_audio(make_chunk(), make_chunk())

            # Wait for the turn to end.
            await collector.wait_for(
                TurnEnded,
                predicate=lambda e, _idx=i: True,
                timeout=3.0,
            )

            # Now race: cancel_turn() concurrently with new audio.
            cancel_task = asyncio.create_task(session.cancel_turn())
            await transport.push_audio(make_chunk(), make_chunk())
            await cancel_task

            # Let the pipeline settle before the next iteration.
            await asyncio.sleep(0.1)

        errors = [e for e in collector.events if isinstance(e, Error)]
        assert errors == [], f"unexpected errors: {errors}"
        assert session.is_running
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_shutdown_during_active_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling session.stop() while an agent is processing should shut down cleanly."""
    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])

    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)
    config = make_test_config(
        transport=transport, agent=SlowAgent(delay=0.5), turn_taking=FAST_TURN
    )
    session = create_session(config)

    collector = EventCollector(session.event_bus)
    collector.subscribe(TurnStarted)

    await session.start()
    try:
        # Push audio to trigger a turn.
        await transport.push_audio(make_chunk(), make_chunk())

        # Wait until the turn has started (agent is now processing).
        await collector.wait_for(TurnStarted, timeout=3.0)

        # Give the slow agent a moment to begin sleeping.
        await asyncio.sleep(0.05)
    finally:
        # Stop should complete within a reasonable timeout even though the
        # agent is mid-execution.
        await transport.finish_input()
        await asyncio.wait_for(session.stop(), timeout=5.0)

    assert not session.is_running
