"""Tests for no-op stub providers."""

from __future__ import annotations

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.stubs import NoopAgent, NoopSTT, NoopTransport, NoopTTS, NoopVAD

_CHUNK = AudioChunk(data=bytes(640), format=PCM16_MONO_16K)


@pytest.mark.asyncio
async def test_noop_stt_lifecycle() -> None:
    stt = NoopSTT()
    await stt.start_stream()
    await stt.send_audio(_CHUNK)
    await stt.end_stream()


@pytest.mark.asyncio
async def test_noop_stt_events_empty() -> None:
    stt = NoopSTT()
    events = [e async for e in stt.events()]
    assert events == []


@pytest.mark.asyncio
async def test_noop_tts_synthesize_empty() -> None:
    tts = NoopTTS()
    events = [e async for e in tts.synthesize("hello")]
    assert events == []


def test_noop_tts_supports_ssml() -> None:
    assert NoopTTS().supports_ssml is True


@pytest.mark.asyncio
async def test_noop_tts_stop_and_cancel() -> None:
    tts = NoopTTS()
    await tts.stop()
    await tts.cancel()


@pytest.mark.asyncio
async def test_noop_vad_process_empty() -> None:
    vad = NoopVAD()
    events = [e async for e in vad.process(_CHUNK)]
    assert events == []


def test_noop_vad_configure_accepts_kwargs() -> None:
    vad = NoopVAD()
    vad.configure(min_speech_duration_ms=100, sensitivity=0.8)


@pytest.mark.asyncio
async def test_noop_transport_lifecycle() -> None:
    transport = NoopTransport()
    await transport.connect()
    chunks = [c async for c in transport.receive_audio()]
    assert chunks == []
    await transport.send_audio(_CHUNK)
    await transport.clear_audio()
    await transport.disconnect()


@pytest.mark.asyncio
async def test_noop_agent_echoes() -> None:
    agent = NoopAgent()
    assert await agent.run("hello") == "hello"
