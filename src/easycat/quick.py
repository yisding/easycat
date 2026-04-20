"""Minimal one-call helpers for teaching examples and quick one-offs.

These helpers intentionally wrap only the most common teaching-chapter
cases (``batch transcribe a WAV``, ``speak a string through a transport``)
so example code can stay at 5 lines instead of 25.  For production use,
reach for :func:`easycat.create_stt_provider` /
:func:`easycat.create_tts_provider` / :func:`easycat.create_session`.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEventType, TTSEventType
from easycat.stt.factory import STTProviderConfig, create_stt_provider
from easycat.tts.factory import TTSProviderConfig, create_tts_provider
from easycat.tts.input import TTSInput

if TYPE_CHECKING:
    from easycat.providers import Transport, TTSProvider

_ENV_VAR_FOR_PROVIDER: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "openai-realtime": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}


def _resolve_api_key(provider: str, api_key: str | None) -> str:
    if api_key:
        return api_key
    env_var = _ENV_VAR_FOR_PROVIDER.get(provider, "OPENAI_API_KEY")
    resolved = os.getenv(env_var, "")
    if not resolved:
        raise RuntimeError(
            f"No API key for provider {provider!r}; pass api_key=... or set ${env_var}."
        )
    return resolved


async def transcribe_file(
    path: str | Path,
    *,
    provider: str = "openai",
    api_key: str | None = None,
) -> str:
    """Transcribe a 16-bit PCM WAV file and return the final transcript.

    Streams the file through the named provider in one segment and joins
    every ``FINAL`` event's text with spaces.  Raises ``ValueError`` if
    the file is not 16-bit PCM, and ``RuntimeError`` if no API key can
    be resolved.
    """
    resolved_key = _resolve_api_key(provider, api_key)
    stt = create_stt_provider(STTProviderConfig(provider=provider, api_key=resolved_key))

    with wave.open(str(Path(path)), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError("transcribe_file expects a 16-bit PCM WAV file")
        audio_format = AudioFormat(
            sample_rate=wf.getframerate(),
            channels=wf.getnchannels(),
            sample_width=2,
        )
        pcm_bytes = wf.readframes(wf.getnframes())

    await stt.start_stream()
    try:
        chunk_size = max(audio_format.bytes_per_second // 10, audio_format.frame_size)
        for i in range(0, len(pcm_bytes), chunk_size):
            await stt.send_audio(
                AudioChunk(data=pcm_bytes[i : i + chunk_size], format=audio_format)
            )
    finally:
        await stt.end_stream()

    parts: list[str] = []
    async for event in stt.events():
        if event.type == STTEventType.FINAL:
            parts.append(event.text)
    return " ".join(p for p in parts if p).strip()


async def speak(
    transport: Transport,
    text: str,
    *,
    tts: TTSProvider | None = None,
    provider: str = "openai",
    api_key: str | None = None,
) -> None:
    """Synthesize *text* via TTS and stream audio chunks to *transport*.

    If *tts* is ``None``, constructs an OpenAI TTS provider (or the
    named *provider*) via :func:`create_tts_provider`.  The transport
    must already be connected.
    """
    if tts is None:
        resolved_key = _resolve_api_key(provider, api_key)
        tts = create_tts_provider(
            TTSProviderConfig(provider=provider, settings={"api_key": resolved_key})
        )

    async for event in tts.synthesize(TTSInput(text=text)):
        if event.type == TTSEventType.AUDIO and event.audio is not None:
            await transport.send_audio(event.audio)
