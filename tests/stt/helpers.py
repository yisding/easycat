"""Shared test helpers for STT provider tests."""

from __future__ import annotations

import asyncio
import math
import struct

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import STTEvent


def generate_pcm_sine(
    duration_ms: int = 500,
    sample_rate: int = 16000,
    frequency: float = 440.0,
) -> bytes:
    """Generate PCM16 mono sine wave audio."""
    num_samples = int(sample_rate * duration_ms / 1000)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(16383 * math.sin(2 * math.pi * frequency * t))
        samples.append(struct.pack("<h", value))
    return b"".join(samples)


def generate_pcm_silence(
    duration_ms: int = 500,
    sample_rate: int = 16000,
) -> bytes:
    """Generate silent PCM16 mono audio."""
    num_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * num_samples


def generate_pcm_noise(
    duration_ms: int = 500,
    sample_rate: int = 16000,
    seed: int = 42,
) -> bytes:
    """Generate pseudo-random noise PCM16 mono audio."""
    import random

    rng = random.Random(seed)
    num_samples = int(sample_rate * duration_ms / 1000)
    samples = []
    for _ in range(num_samples):
        value = rng.randint(-8000, 8000)
        samples.append(struct.pack("<h", value))
    return b"".join(samples)


def make_audio_chunks(
    pcm_data: bytes,
    fmt: AudioFormat = PCM16_MONO_16K,
    chunk_duration_ms: int = 100,
) -> list[AudioChunk]:
    """Split PCM data into AudioChunks of specified duration."""
    bytes_per_chunk = int(fmt.bytes_per_second * chunk_duration_ms / 1000)
    chunks = []
    for i in range(0, len(pcm_data), bytes_per_chunk):
        chunk_data = pcm_data[i : i + bytes_per_chunk]
        if chunk_data:
            chunks.append(AudioChunk(data=chunk_data, format=fmt))
    return chunks


async def collect_stt_events(
    provider: object,
    audio_chunks: list[AudioChunk],
) -> list[STTEvent]:
    """Feed audio through an STT provider and collect all emitted events.

    Test harness that works with both
    streaming providers (Deepgram, ElevenLabs realtime) and turn-based
    providers (OpenAI, ElevenLabs batch).
    """
    collected: list[STTEvent] = []

    await provider.start_stream()  # type: ignore[union-attr]

    async def _collect() -> None:
        async for event in provider.events():  # type: ignore[union-attr]
            collected.append(event)

    collect_task = asyncio.create_task(_collect())

    for chunk in audio_chunks:
        await provider.send_audio(chunk)  # type: ignore[union-attr]

    await provider.end_stream()  # type: ignore[union-attr]
    await collect_task

    return collected
