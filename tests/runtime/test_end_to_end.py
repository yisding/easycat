"""End-to-end session turn verification.

Runs a full session turn with stub providers and verifies that the session
completes without error. Legacy strangler-fig journal records (EVENT,
SPAN_START, SPAN_END, METRIC) are no longer produced after the WS5 migration
to no-op shims.
"""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import BotStoppedSpeaking
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.stubs import scripted_turn_providers
from easycat.turn_manager import TurnManagerConfig


@pytest.mark.asyncio
async def test_full_turn_completes_successfully():
    """One turn with stub providers completes without error."""
    providers = scripted_turn_providers(transcript="hello")
    config = SessionConfig(
        transport=providers.transport,
        vad=providers.vad,
        stt=providers.stt,
        agent=providers.agent,
        tts=providers.tts,
        turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
        session_id="e2e-test",
    )
    session = Session(config)
    pipeline_done = asyncio.Event()
    session.event_bus.subscribe(BotStoppedSpeaking, lambda event: pipeline_done.set())

    await session.start()
    await asyncio.wait_for(pipeline_done.wait(), timeout=1.0)
    await session.stop()

    # The transport should have received at least one audio chunk from TTS
    assert providers.transport.sent
