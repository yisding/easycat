"""Conformance tests: EnergyVAD satisfies EasyCat's ``VADProvider`` Protocol.

Run with ``uv run pytest test_custom_vad.py``. The protocol check is
structural (``@runtime_checkable``); the behavior tests pin the event
grammar the Session relies on — events only on speech-state transitions.
"""

from __future__ import annotations

import asyncio

from custom_vad import EnergyVAD, EnergyVADConfig

from easycat import PCM16_MONO_16K, AudioChunk, VADProvider, VADStartSpeaking, VADStopSpeaking

LOUD = AudioChunk(data=b"\xe8\x03" * 160, format=PCM16_MONO_16K)  # 1000-amplitude PCM16
QUIET = AudioChunk(data=b"\x00\x00" * 160, format=PCM16_MONO_16K)


def events_for(vad: EnergyVAD, chunk: AudioChunk) -> list[object]:
    async def collect() -> list[object]:
        return [event async for event in vad.process(chunk)]

    return asyncio.run(collect())


def test_conforms_to_vad_provider_protocol() -> None:
    assert isinstance(EnergyVAD(), VADProvider)


def test_version_info_reports_journal_fields() -> None:
    info = EnergyVAD().version_info()
    assert {"provider", "model", "api_version", "sdk_version"} <= set(info)


def test_emits_events_only_on_speech_transitions() -> None:
    vad = EnergyVAD(EnergyVADConfig(threshold=100.0))

    started = events_for(vad, LOUD)
    assert len(started) == 1 and isinstance(started[0], VADStartSpeaking)
    assert events_for(vad, LOUD) == []  # still speaking — no new event

    stopped = events_for(vad, QUIET)
    assert len(stopped) == 1 and isinstance(stopped[0], VADStopSpeaking)
    assert events_for(vad, QUIET) == []  # still silent — no new event


def test_ignores_incomplete_pcm16_sample() -> None:
    vad = EnergyVAD(EnergyVADConfig(threshold=100.0))

    odd_loud = AudioChunk(data=LOUD.data + b"\xff", format=PCM16_MONO_16K)
    started = events_for(vad, odd_loud)
    assert len(started) == 1 and isinstance(started[0], VADStartSpeaking)

    trailing_byte_only = AudioChunk(data=b"\xff", format=PCM16_MONO_16K)
    assert events_for(EnergyVAD(), trailing_byte_only) == []


def test_configure_scales_threshold_with_sensitivity() -> None:
    vad = EnergyVAD()
    vad.configure(sensitivity=1.0)  # most sensitive: any energy is speech
    assert isinstance(events_for(vad, LOUD)[0], VADStartSpeaking)
