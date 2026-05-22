from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.providers import STTProvider
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [pytest.mark.contract, pytest.mark.surface_stt, pytest.mark.provider("matrix")]


class _ContractSTT:
    def __init__(self) -> None:
        self.started = 0
        self.ended = 0
        self.committed = 0
        self.sent: list[AudioChunk] = []
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        self.started += 1
        self._events = asyncio.Queue()

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

    async def commit_segment(self) -> bool:
        self.committed += 1
        return True

    async def end_stream(self) -> None:
        self.ended += 1
        await self._events.put(STTEvent(type=STTEventType.PARTIAL, text="hel"))
        await self._events.put(STTEvent(type=STTEventType.FINAL, text="hello"))
        await self._events.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event


def test_stt_provider_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "stt"]

    assert rows
    assert all(
        row.contract_path == "tests/contracts/test_stt_provider_contracts.py" for row in rows
    )


async def test_stt_contract_lifecycle_and_normalized_events() -> None:
    provider = _ContractSTT()
    chunk = AudioChunk(data=b"\0" * 320, format=PCM16_MONO_16K)

    assert isinstance(provider, STTProvider)
    await provider.start_stream()
    await provider.send_audio(chunk)
    assert await provider.commit_segment() is True
    await provider.end_stream()
    events = [event async for event in provider.events()]

    assert provider.started == 1
    assert provider.ended == 1
    assert provider.committed == 1
    assert provider.sent == [chunk]
    assert [event.type for event in events] == [STTEventType.PARTIAL, STTEventType.FINAL]
    assert events[-1].text == "hello"


async def test_stt_contract_stop_semantics_are_idempotent() -> None:
    provider = _ContractSTT()

    await provider.start_stream()
    await provider.end_stream()
    await provider.end_stream()

    assert provider.ended == 2
