"""Test harness utilities for TTS providers."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

from easycat.audio_format import AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.providers import TTSProvider


async def collect_tts_output(provider: TTSProvider, text: str) -> list[TTSEvent]:
    """Send text to a TTS provider and collect all emitted events."""
    events: list[TTSEvent] = []
    async for event in provider.synthesize(text):
        events.append(event)
    return events


def extract_audio_chunks(events: list[TTSEvent]) -> list[AudioChunk]:
    """Extract AudioChunk objects from a list of TTSEvents."""
    return [e.audio for e in events if e.type == TTSEventType.AUDIO and e.audio is not None]


def concatenate_audio(chunks: list[AudioChunk]) -> bytes:
    """Concatenate audio data from multiple AudioChunks."""
    return b"".join(c.data for c in chunks)


def write_wav(chunks: list[AudioChunk], path: str | Path) -> Path:
    """Write collected audio chunks to a WAV file for manual listening tests.

    All chunks must share the same AudioFormat. Returns the path written to.
    """
    path = Path(path)
    if not chunks:
        raise ValueError("No audio chunks to write")

    fmt = chunks[0].format
    raw = concatenate_audio(chunks)

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(fmt.channels)
        wf.setsampwidth(fmt.sample_width)
        wf.setframerate(fmt.sample_rate)
        wf.writeframes(raw)

    return path


def verify_pcm16_audio(chunks: list[AudioChunk]) -> bool:
    """Verify that all chunks contain valid PCM16 mono audio."""
    for chunk in chunks:
        if chunk.format.sample_width != 2:
            return False
        if chunk.format.channels != 1:
            return False
        if chunk.format.encoding != "pcm":
            return False
        if len(chunk.data) % 2 != 0:
            return False
        # Verify samples are in int16 range
        n_samples = len(chunk.data) // 2
        samples = struct.unpack(f"<{n_samples}h", chunk.data)
        for s in samples:
            if s < -32768 or s > 32767:
                return False
    return True
