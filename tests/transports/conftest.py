"""Shared test helpers for transport tests."""

from __future__ import annotations

from easycat.audio_format import AudioChunk, AudioFormat


def make_chunk(n_bytes: int = 320, sample_rate: int = 16000) -> AudioChunk:
    """Create a test audio chunk of silence."""
    fmt = AudioFormat(sample_rate=sample_rate, channels=1, sample_width=2)
    return AudioChunk(data=bytes(n_bytes), format=fmt)
