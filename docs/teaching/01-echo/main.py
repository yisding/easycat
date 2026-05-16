"""Chapter 1 — Echo.

Mic → speaker, continuously, through EasyCat's ``Transport`` protocol.
Runs until Ctrl-C.

Dependency:
    uv sync --extra quickstart --group dev
"""

from __future__ import annotations

import asyncio

from easycat import LocalTransportConfig
from easycat.transports.local import LocalTransport


async def echo(transport) -> None:
    """Pipe every inbound audio chunk straight to the outbound side.

    ``transport`` is deliberately untyped. Any object that matches
    the ``Transport`` protocol (the four methods in
    ``easycat.providers.Transport``) will work — that is the whole
    point of duck-typed protocols. Chapter 13 swaps in a different
    transport without changing this function.

    ``transport.receive_audio()`` is an *async generator* of audio
    chunks. ``await transport.send_audio(chunk)`` hands the chunk to
    the speaker. No buffer, no turn detection, no STT — the point
    of this chapter is the shape of the loop itself.
    """
    async for chunk in transport.receive_audio():
        await transport.send_audio(chunk)


async def main() -> None:
    transport = LocalTransport(LocalTransportConfig())
    await transport.connect()
    print("Echoing mic to speakers. Ctrl-C to stop.")
    try:
        await echo(transport)
    finally:
        await transport.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
