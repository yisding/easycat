"""Shared test helpers for transport tests."""

from __future__ import annotations

from easycat.audio_format import AudioChunk, AudioFormat


def find_free_port() -> int:
    """Find a free TCP port on localhost."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_chunk(n_bytes: int = 320, sample_rate: int = 16000) -> AudioChunk:
    """Create a test audio chunk of silence."""
    fmt = AudioFormat(sample_rate=sample_rate, channels=1, sample_width=2)
    return AudioChunk(data=bytes(n_bytes), format=fmt)
