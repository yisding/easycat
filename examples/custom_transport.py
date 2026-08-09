"""Inject a custom Transport via EasyConfig.

The mirror of ``custom_stt_provider.py`` / ``custom_tts_provider.py`` /
``custom_vad_provider.py`` for the transport stage.  Transports implement
the ``Transport`` Protocol structurally (connect / disconnect /
receive_audio / send_audio / version_info) — no base class required.

Here ``CountingTransport`` wraps the built-in ``LocalTransport`` and
tallies the audio bytes flowing each way, then is passed straight to
``EasyConfig(transport=...)``.  To build a transport from scratch instead
of wrapping one, inherit ``easycat.transports.AudioQueueMixin`` for the
inbound queue and ``TransportDegraded`` plumbing — see
``docs/extending/transport.md``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/custom_transport.py
  uv run --env-file .env python examples/custom_transport.py  # if keys live in .env
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from easycat import AudioChunk, AudioFormat, EasyConfig, require_env, run
from easycat.transports import LocalTransport


class CountingTransport:
    """Wraps a ``LocalTransport`` and counts the audio bytes flowing each way.

    Implements the ``Transport`` Protocol structurally — no base class.
    The ``audio_format`` / ``clear_audio`` / ``default_echo_cancellation_enabled``
    members are optional transport capabilities, delegated so the wrapper
    behaves exactly like the local transport it instruments.
    """

    default_echo_cancellation_enabled = True

    def __init__(self, inner: LocalTransport) -> None:
        self._inner = inner
        self.bytes_in = 0
        self.bytes_out = 0

    async def connect(self) -> None:
        await self._inner.connect()

    async def disconnect(self) -> None:
        await self._inner.disconnect()
        print(f"[transport] mic in: {self.bytes_in} bytes, bot out: {self.bytes_out} bytes")

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        async for chunk in self._inner.receive_audio():
            self.bytes_in += len(chunk.data)
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.bytes_out += len(chunk.data)
        return await self._inner.send_audio(chunk)

    async def clear_audio(self) -> None:
        await self._inner.clear_audio()

    @property
    def audio_format(self) -> AudioFormat:
        return self._inner.audio_format

    def version_info(self) -> dict[str, str]:
        return {**self._inner.version_info(), "wrapper": "counting"}


def main() -> None:
    require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    transport = CountingTransport(LocalTransport())
    run(
        EasyConfig(
            transport=transport,
            agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        )
    )


if __name__ == "__main__":
    main()
