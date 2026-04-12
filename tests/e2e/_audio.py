"""Audio helpers for E2E tests.

Provides tone generation, energy/quality checks, and reference ASR for
verifying that outbound audio actually contains the expected speech.
"""

from __future__ import annotations

import math
import os
import struct
from collections.abc import Iterable

from easycat.audio_format import PCM16_MONO_16K, AudioFormat


def sine_pcm16(
    *,
    freq_hz: float = 440.0,
    duration_s: float = 0.5,
    sample_rate: int = 16000,
    amplitude: float = 0.5,
) -> bytes:
    """Generate a mono PCM16 little-endian sine tone."""
    n = int(duration_s * sample_rate)
    amp = int(32767 * max(0.0, min(1.0, amplitude)))
    samples = [int(amp * math.sin(2 * math.pi * freq_hz * (i / sample_rate))) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def silence_pcm16(*, duration_s: float, sample_rate: int = 16000) -> bytes:
    """Produce PCM16 silence of the given duration."""
    return bytes(int(duration_s * sample_rate) * 2)


def scale_pcm16(pcm: bytes, factor: float) -> bytes:
    """Multiply PCM16 samples by a linear factor (with saturation)."""
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm)
    scaled = [max(-32768, min(32767, int(s * factor))) for s in samples]
    return struct.pack(f"<{n}h", *scaled)


def measure_rms(pcm: bytes) -> float:
    """Compute RMS energy of PCM16 samples as a fraction of full-scale."""
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm)
    ss = sum(s * s for s in samples) / n
    return math.sqrt(ss) / 32768.0


def detect_clipping(pcm: bytes, *, threshold_pct: float = 0.5) -> bool:
    """True if more than ``threshold_pct`` of samples are at ±full-scale."""
    if not pcm:
        return False
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm)
    at_limit = sum(1 for s in samples if s >= 32767 or s <= -32768)
    return (at_limit / n) * 100.0 > threshold_pct


def compare_audio_bytes(a: bytes, b: bytes) -> bool:
    """Byte-identical comparison (for replay fidelity tests)."""
    return a == b


def concat_pcm16(chunks: Iterable[bytes]) -> bytes:
    return b"".join(chunks)


async def decode_and_asr(pcm_bytes: bytes, *, sample_rate: int) -> str:
    """Transcribe PCM16 audio via a reference STT (OpenAI Whisper).

    Requires ``OPENAI_API_KEY``. Returns the transcript lowercased.
    Raises ``RuntimeError`` if no API key is available — callers should
    gate on ``OPENAI_API_KEY`` before calling.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("decode_and_asr requires OPENAI_API_KEY")

    from easycat.audio_utils import pcm_to_wav

    fmt = AudioFormat(sample_rate=sample_rate, channels=1, sample_width=2)
    wav_bytes = pcm_to_wav(pcm_bytes, fmt)

    import httpx

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={"model": "whisper-1", "response_format": "text"},
        )
        resp.raise_for_status()
        return resp.text.strip().lower()


async def render_tts_pcm(text: str, *, voice: str = "alloy") -> bytes:
    """Render text to PCM16 @ 24 kHz using OpenAI TTS (for voice fixtures).

    Requires ``OPENAI_API_KEY``.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("render_tts_pcm requires OPENAI_API_KEY")

    import httpx

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "tts-1",
                "voice": voice,
                "input": text,
                "response_format": "pcm",
            },
        )
        resp.raise_for_status()
        return resp.content


__all__ = [
    "PCM16_MONO_16K",
    "compare_audio_bytes",
    "concat_pcm16",
    "decode_and_asr",
    "detect_clipping",
    "measure_rms",
    "render_tts_pcm",
    "scale_pcm16",
    "silence_pcm16",
    "sine_pcm16",
]
