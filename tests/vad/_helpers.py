from __future__ import annotations

import struct

from easycat.audio_format import PCM16_MONO_16K, AudioChunk


def _make_chunk(value: int = 0, n_samples: int = 512) -> AudioChunk:
    """Create a PCM16 chunk filled with the given sample value."""
    data = struct.pack(f"<{n_samples}h", *([value] * n_samples))
    return AudioChunk(data=data, format=PCM16_MONO_16K)


def _assert_extra_hint(message: str, extra: str) -> None:
    assert f"uv add 'easycat[{extra}]'" in message
    assert f"uv sync --extra {extra} --group dev" in message
