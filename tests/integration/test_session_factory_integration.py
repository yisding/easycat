from __future__ import annotations

import pytest

from easycat import TelephonyConfig, create_session
from easycat.config import OutboundCallConfig
from easycat.events import AgentFinal, BotStartedSpeaking
from easycat.telephony.call_state import OutboundCallStateMachine
from easycat.telephony.number_health import CallDispositionTracker, NumberHealthMonitor

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


class UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


def test_create_session_exposes_outbound_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = QueueTransport()
    patch_provider_factories(
        monkeypatch,
        stt=ScriptedSTT(["unused"]),
        tts=RecordingTTS(chunk_sizes=(640,)),
        vad=ScriptedVAD([]),
    )

    session = create_session(
        make_test_config(
            transport=transport,
            agent=UpperAgent(),
            telephony=TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(
                    from_number="+15551234567",
                    enable_screening_detection=False,
                ),
            ),
        )
    )

    assert isinstance(session.telephony.outbound_call_state_machine, OutboundCallStateMachine)
    assert (
        session.get_helper(OutboundCallStateMachine)
        is session.telephony.outbound_call_state_machine
    )
    assert isinstance(session.telephony.number_health_monitor, NumberHealthMonitor)
    assert isinstance(session.telephony.call_disposition_tracker, CallDispositionTracker)
    assert session.telephony.outbound_call_manager is None


@pytest.mark.asyncio
@pytest.mark.integration_local
async def test_create_session_replays_gated_audio_after_human_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = QueueTransport()
    stt = ScriptedSTT(["banana"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(
        transport=transport,
        agent=UpperAgent(),
        telephony=TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(
                from_number="+15551234567",
                classification_gate=True,
                classification_gate_timeout_s=2.0,
                enable_screening_detection=False,
            ),
        ),
    )
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStartedSpeaking)

    await session.start()
    try:
        outbound_sm = session.telephony.outbound_call_state_machine
        assert isinstance(outbound_sm, OutboundCallStateMachine)

        outbound_sm.gate.close()
        assert outbound_sm.gate.is_buffering
        assert session._is_gated

        await transport.push_audio(make_chunk(), make_chunk())
        agent_final = await collector.wait_for(AgentFinal, timeout=2.0)

        await wait_for_condition(lambda: len(outbound_sm.gate.buffer) >= 1, timeout=2.0)
        assert transport.sent == []
        assert tts.payloads[0].text == agent_final.text

        await outbound_sm.gate.flush_and_release()
        await wait_for_condition(lambda: len(transport.sent) >= 1, timeout=2.0)
        await collector.wait_for(BotStartedSpeaking, timeout=2.0)

        assert not outbound_sm.gate.is_buffering
        assert transport.sent
    finally:
        await transport.finish_input()
        await session.stop()
