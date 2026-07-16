"""Exercise ``transcribe_file`` provider ownership without an API.

uv run python docs/teaching/02-transcribe/transcribe_ownership_probe.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.recipes import transcribe_file


class ScriptedSTT:
    def __init__(self) -> None:
        self.closed = False
        self.ended = False

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, _chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        self.ended = True

    async def events(self) -> AsyncIterator[STTEvent]:
        yield STTEvent(type=STTEventType.FINAL, text="provider-free transcript")

    async def close(self) -> None:
        self.closed = True


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 320)


async def probe() -> dict[str, bool | str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "probe.wav"
        _write_wav(path)

        owned = ScriptedSTT()
        with patch("easycat.recipes.create_stt_provider", return_value=owned):
            owned_text = await transcribe_file(path, api_key="placeholder")

        caller = ScriptedSTT()
        caller_text = await transcribe_file(path, stt=caller)

    return {
        "owned_transcript": owned_text,
        "owned_stream_ended": owned.ended,
        "owned_provider_closed": owned.closed,
        "caller_transcript": caller_text,
        "caller_stream_ended": caller.ended,
        "caller_provider_closed": caller.closed,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
