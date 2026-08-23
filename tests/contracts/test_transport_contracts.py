from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.testing import TransportContractSuite
from easycat.transports.telnyx_media import TelnyxConnectionTransport
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

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "contract-transport",
            "model": "unknown",
            "api_version": "v1",
            "sdk_version": "none",
        }


def test_transport_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "transport"]

    assert {row.provider for row in rows} == {
        "local",
        "websocket",
        "twilio",
        "telnyx",
        "webrtc",
        "webtransport",
    }
    assert all(row.contract_path == "tests/contracts/test_transport_contracts.py" for row in rows)


class TestTransportContractSuite(TransportContractSuite):
    """Run the shipped provider-author kit suite against the offline fake.

    The protocol-semantics assertions live in
    :class:`easycat.testing.TransportContractSuite` so this file and the
    installable kit cannot drift; only fake-specific delivery checks are
    added below.
    """

    provider_factory = _ContractTransport

    async def test_fake_delivers_pushed_audio_until_disconnect(
        self, provider: _ContractTransport
    ) -> None:
        chunk = AudioChunk(data=b"\0" * 320, format=PCM16_MONO_16K)

        assert await provider.send_audio(chunk) is False
        await provider.connect()
        assert await provider.send_audio(chunk) is True
        await provider.push_audio(chunk)
        await provider.disconnect()
        received = [item async for item in provider.receive_audio()]

        assert provider.sent == [chunk]
        assert received == [chunk]
        assert provider.connected is False

    async def test_fake_counts_clear_audio_calls(self, provider: _ContractTransport) -> None:
        await provider.clear_audio()
        await provider.clear_audio()

        assert provider.clear_calls == 2


class _FakeTelnyxWebSocket:
    """Scripted accepted socket that blocks its receive stream until closed."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_calls = 0
        self._closed = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *_args: object) -> None:
        self.close_calls += 1
        self._closed.set()

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        await self._closed.wait()
        raise StopAsyncIteration


class TestTelnyxTransportContractSuite(TransportContractSuite):
    """Run the shipped kit suite against the offline Telnyx connection transport.

    Mirrors the Twilio accepted-socket pattern (scripted WebSocket, no live
    listener); no start frame is staged, so sends legally drop and the kit's
    ``expects_send_accepted_after_connect`` override applies.
    """

    pytestmark: ClassVar[list[Any]] = [pytest.mark.provider("telnyx")]

    expects_send_accepted_after_connect = False

    @staticmethod
    def provider_factory() -> TelnyxConnectionTransport:
        return TelnyxConnectionTransport(
            _FakeTelnyxWebSocket(),  # type: ignore[arg-type]
        )
