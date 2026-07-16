"""Exercise Chapter 1's transport contract without audio hardware.

uv run python docs/teaching/01-echo/transport_contract_probe.py
"""

from __future__ import annotations

import asyncio
import json

from main import echo

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.providers import Transport, TransportLike


def _chunk() -> AudioChunk:
    # One 20 ms PCM16 mono frame at 24 kHz.
    return AudioChunk(data=b"\x00\x00" * 480, format=PCM16_MONO_24K)


class ScriptedTransport:
    """Full Transport whose second outbound chunk is rejected."""

    def __init__(self) -> None:
        self._acceptance = iter((True, False, True))

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self):
        for _ in range(3):
            yield _chunk()

    async def send_audio(self, _chunk: AudioChunk) -> bool:
        return next(self._acceptance)

    def version_info(self) -> dict[str, str]:
        return {"provider": "scripted"}


class LegacyTransportLike(ScriptedTransport):
    version_info = None  # type: ignore[assignment]


async def probe() -> dict[str, int | bool]:
    transport = ScriptedTransport()
    accepted, rejected = await echo(transport)
    legacy = LegacyTransportLike()
    return {
        "accepted": accepted,
        "rejected": rejected,
        "full_transport": isinstance(transport, Transport),
        "legacy_transport_like": isinstance(legacy, TransportLike),
        "legacy_full_transport": isinstance(legacy, Transport),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
