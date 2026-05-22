from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.providers import Transport
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [
    pytest.mark.contract,
    pytest.mark.surface_transport,
    pytest.mark.provider("offline-fake"),
]


class _ContractTransport:
    def __init__(self) -> None:
        self.connected = False
        self.clear_calls = 0
        self.sent: list[AudioChunk] = []
        self._incoming: asyncio.Queue[AudioChunk | None] = asyncio.Queue()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        await self._incoming.put(None)

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        while True:
            chunk = await self._incoming.get()
            if chunk is None:
                break
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if not self.connected:
            return False
        self.sent.append(chunk)
        return True

    async def clear_audio(self) -> None:
        self.clear_calls += 1

    async def push_audio(self, chunk: AudioChunk) -> None:
        await self._incoming.put(chunk)


def test_transport_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "transport"]

    assert {row.provider for row in rows} == {
        "local",
        "websocket",
        "twilio",
        "webrtc",
        "webtransport",
    }
    assert all(row.contract_path == "tests/contracts/test_transport_contracts.py" for row in rows)


async def test_transport_contract_lifecycle_and_delivery_semantics() -> None:
    transport = _ContractTransport()
    chunk = AudioChunk(data=b"\0" * 320, format=PCM16_MONO_16K)

    assert isinstance(transport, Transport)
    assert await transport.send_audio(chunk) is False
    await transport.connect()
    assert await transport.send_audio(chunk) is True
    await transport.push_audio(chunk)
    await transport.disconnect()
    received = [item async for item in transport.receive_audio()]

    assert transport.sent == [chunk]
    assert received == [chunk]
    assert transport.connected is False


async def test_transport_contract_clear_audio_is_idempotent() -> None:
    transport = _ContractTransport()

    await transport.clear_audio()
    await transport.clear_audio()

    assert transport.clear_calls == 2
