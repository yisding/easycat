"""Chapter 1 — Echo.

Mic → speaker, continuously, through EasyCat's ``Transport`` protocol.
Runs until Ctrl-C.

Dependency:
    uv sync --extra local --group dev
"""

from __future__ import annotations

import asyncio

from easycat import LocalTransportConfig
from easycat.transports.local import LocalTransport


async def echo(transport) -> tuple[int, int]:
    """Pipe every inbound audio chunk straight to the outbound side.

    ``transport`` is deliberately untyped. Any object that matches
    the inbound/outbound audio shape of ``easycat.providers.Transport``
    will work — that is the whole point of duck-typed protocols.
    Chapter 13 swaps in a different transport without changing this
    function.

    ``transport.receive_audio()`` is an *async generator* of audio
    chunks. ``await transport.send_audio(chunk)`` returns whether the
    transport accepted each chunk for delivery; it does not prove speaker
    playback. No turn detection or STT — the point of this chapter is the
    shape of the loop itself.
    """
    accepted = rejected = 0
    async for chunk in transport.receive_audio():
        if await transport.send_audio(chunk):
            accepted += 1
        else:
            rejected += 1
    return accepted, rejected


async def main() -> None:
    transport = LocalTransport(LocalTransportConfig())
    await transport.connect()
    print("Echoing mic to speakers. Ctrl-C to stop.")
    try:
        accepted, rejected = await echo(transport)
        print(f"Echo stream ended: accepted={accepted}, rejected={rejected}")
    finally:
        await transport.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
