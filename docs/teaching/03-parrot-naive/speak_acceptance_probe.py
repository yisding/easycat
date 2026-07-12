"""Exercise ``recipes.speak`` transport acceptance without providers.

uv run python docs/teaching/03-parrot-naive/speak_acceptance_probe.py
"""

from __future__ import annotations

import asyncio
import json

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.recipes import speak
from easycat.tts.input import TTSInput


class ScriptedTTS:
    async def synthesize(self, _input: TTSInput):
        for _ in range(3):
            yield TTSEvent(
                type=TTSEventType.AUDIO,
                audio=AudioChunk(data=b"\x00\x00" * 480, format=PCM16_MONO_24K),
            )


class ScriptedTransport:
    def __init__(self) -> None:
        self._acceptance = iter((True, False, True))

    async def send_audio(self, _audio: AudioChunk) -> bool:
        return next(self._acceptance)


async def probe() -> dict[str, int]:
    accepted, rejected = await speak(
        ScriptedTransport(),
        "provider-free",
        tts=ScriptedTTS(),
    )
    return {
        "produced_chunks": accepted + rejected,
        "accepted_chunks": accepted,
        "rejected_chunks": rejected,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
