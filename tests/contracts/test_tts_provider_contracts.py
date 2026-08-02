from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.testing import TTSProviderContractSuite
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import (
    ElevenLabsStreamMode,
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
)
from easycat.tts.factory import _CATALOG as _TTS_CATALOG
from easycat.tts.input import TTSInput, coerce_tts_input
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [pytest.mark.contract, pytest.mark.surface_tts, pytest.mark.provider("matrix")]


class _ContractTTS:
    def __init__(self) -> None:
        self.payloads: list[TTSInput] = []
        self.stop_calls = 0
        self.cancel_calls = 0

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        self.payloads.append(coerce_tts_input(payload))
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\0" * 320, format=PCM16_MONO_24K),
        )
        yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": "hello"}])

    async def stop(self) -> None:
        self.stop_calls += 1

    async def cancel(self) -> None:
        self.cancel_calls += 1

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "contract-tts",
            "model": "offline",
            "api_version": "v1",
            "sdk_version": "none",
        }


class _StaticByteStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data

    async def aclose(self) -> None:
        return None


def _offline_http_client() -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_StaticByteStream(b"\0" * 9_600),
            request=request,
        )

    return httpx.AsyncClient(
        base_url="https://contract.invalid",
        transport=httpx.MockTransport(handle),
    )


class _ScriptedTTSWebSocket:
    """Minimal ReconnectingWebSocket seam for real TTS implementations."""

    def __init__(self, messages: list[str | bytes]) -> None:
        self._messages = messages
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def send(self, _message: str | bytes) -> None:
        return None

    async def recv_iter(self) -> AsyncIterator[str | bytes]:
        for message in self._messages:
            yield message

    async def close(self) -> None:
        self._connected = False


class _OfflineDeepgramTTS(DeepgramTTS):
    def _create_ws(self) -> Any:
        return _ScriptedTTSWebSocket(
            [
                b"\0" * 960,
                json.dumps({"type": "Flushed"}),
            ]
        )


class _OfflineCartesiaTTS(CartesiaTTS):
    def _create_ws(self) -> Any:
        return _ScriptedTTSWebSocket(
            [
                json.dumps(
                    {
                        "type": "chunk",
                        "data": base64.b64encode(b"\0" * 960).decode("ascii"),
                        "done": True,
                    }
                )
            ]
        )


async def _openai_tts() -> OpenAITTS:
    provider = OpenAITTS(OpenAITTSConfig(api_key="contract-key"))
    await provider._client.aclose()
    provider._client = _offline_http_client()
    return provider


async def _deepgram_tts() -> DeepgramTTS:
    return _OfflineDeepgramTTS(DeepgramTTSConfig(api_key="contract-key", persistent_ws=False))


async def _elevenlabs_tts() -> ElevenLabsTTS:
    provider = ElevenLabsTTS(
        ElevenLabsTTSConfig(
            api_key="contract-key",
            stream_mode=ElevenLabsStreamMode.HTTP,
        )
    )
    provider._client = _offline_http_client()
    return provider


async def _cartesia_tts() -> CartesiaTTS:
    return _OfflineCartesiaTTS(CartesiaTTSConfig(api_key="contract-key", persistent_ws=False))


_BUILT_IN_TTS_FACTORIES_BY_NAME = {
    "openai": _openai_tts,
    "deepgram": _deepgram_tts,
    "elevenlabs": _elevenlabs_tts,
    "cartesia": _cartesia_tts,
}
_BUILT_IN_TTS_FACTORIES = tuple(
    pytest.param(factory, id=name) for name, factory in _BUILT_IN_TTS_FACTORIES_BY_NAME.items()
)


def test_tts_provider_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "tts"]

    assert rows
    assert all(
        row.contract_path == "tests/contracts/test_tts_provider_contracts.py" for row in rows
    )


def test_real_tts_contract_factories_cover_catalog() -> None:
    assert set(_BUILT_IN_TTS_FACTORIES_BY_NAME) == set(_TTS_CATALOG.specs)


class TestTTSContractSuite(TTSProviderContractSuite):
    """Run the shipped provider-author kit suite against the offline fake.

    The protocol-semantics assertions live in
    :class:`easycat.testing.TTSProviderContractSuite` so this file and the
    installable kit cannot drift; only fake-specific bookkeeping checks are
    added below.
    """

    provider_factory = _ContractTTS

    async def test_fake_normalizes_payloads_and_marker_events(
        self, provider: _ContractTTS
    ) -> None:
        events = [event async for event in provider.synthesize("hello")]

        assert provider.payloads[0].text == "hello"
        assert [event.type for event in events] == [TTSEventType.AUDIO, TTSEventType.MARKERS]
        assert events[0].audio is not None
        assert events[0].audio.format == PCM16_MONO_24K
        assert events[1].markers == [{"word": "hello"}]

    async def test_fake_counts_stop_and_cancel_calls(self, provider: _ContractTTS) -> None:
        await provider.stop()
        await provider.stop()
        await provider.cancel()
        await provider.cancel()

        assert provider.stop_calls == 2
        assert provider.cancel_calls == 2


class TestBuiltInTTSContractSuite(TTSProviderContractSuite):
    """Run the provider-author contract kit against every real built-in TTS."""

    @pytest.fixture(params=_BUILT_IN_TTS_FACTORIES)
    async def provider(self, request: pytest.FixtureRequest) -> AsyncIterator[Any]:
        provider = await request.param()
        try:
            yield provider
        finally:
            await provider.close()
