import pytest

from easycat.events import STTEventType, TTSEventType, VADStartSpeaking, VADStopSpeaking
from easycat.stubs import (
    ScriptedAgent,
    ScriptedTransport,
    ScriptedTTS,
    scripted_turn_providers,
)


@pytest.mark.asyncio
async def test_scripted_turn_providers_drive_one_local_turn() -> None:
    providers = scripted_turn_providers(
        transcript="hello",
        reply=lambda text: f"reply: {text}",
        input_chunks=3,
    )

    chunks = [chunk async for chunk in providers.transport.receive_audio()]
    assert len(chunks) == 3

    vad_events = []
    for chunk in chunks:
        vad_events.extend([event async for event in providers.vad.process(chunk)])
    assert [type(event) for event in vad_events] == [VADStartSpeaking, VADStopSpeaking]

    await providers.stt.end_stream()
    stt_events = [event async for event in providers.stt.events()]
    assert [(event.type, event.text) for event in stt_events] == [(STTEventType.FINAL, "hello")]

    assert await providers.agent.run("hello") == "reply: hello"
    tts_events = [event async for event in providers.tts.synthesize("reply: hello")]
    assert [event.type for event in tts_events] == [TTSEventType.AUDIO]
    assert tts_events[0].audio is not None

    await providers.transport.send_audio(tts_events[0].audio)
    assert providers.transport.sent == [tts_events[0].audio]


def test_scripted_turn_providers_are_active_not_noop() -> None:
    providers = scripted_turn_providers()

    assert not getattr(providers.transport, "is_passthrough_provider", False)
    assert not getattr(providers.vad, "is_passthrough_provider", False)
    assert not getattr(providers.stt, "is_passthrough_provider", False)
    assert not getattr(providers.tts, "is_passthrough_provider", False)


def test_scripted_turn_providers_reject_empty_input() -> None:
    with pytest.raises(ValueError, match="input_chunks must contain at least one chunk"):
        scripted_turn_providers(input_chunks=0)


def test_scripted_providers_reject_empty_direct_chunk_scripts() -> None:
    with pytest.raises(ValueError, match="chunks must contain at least one chunk"):
        ScriptedTransport(0)
    with pytest.raises(ValueError, match="chunks must contain at least one chunk"):
        ScriptedTTS(())


@pytest.mark.asyncio
async def test_scripted_agent_supports_fixed_and_echo_replies() -> None:
    assert await ScriptedAgent().run("hi") == "Echo: hi"
    assert await ScriptedAgent("fixed").run("hi") == "fixed"
