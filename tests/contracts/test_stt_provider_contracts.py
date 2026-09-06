from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.factory import _CATALOG as _STT_CATALOG
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stt.openai_realtime_provider import (
    OpenAIRealtimeSTT,
    OpenAIRealtimeSTTConfig,
)
from easycat.testing import STTProviderContractSuite
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
        await self._events.put(STTEvent(type=STTEventType.PARTIAL, text="hel"))
        await self._events.put(STTEvent(type=STTEventType.FINAL, text="hello"))
        return True

    async def end_stream(self) -> None:
        self.ended += 1
        await self._events.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "contract-stt",
            "model": "offline",
            "api_version": "v1",
            "sdk_version": "none",
        }


class _QueueWebSocket:
    """Queue-backed protocol seam used by the real streaming providers."""

    _STOP = object()

    def __init__(
        self,
        responder: Callable[[str | bytes], tuple[str | None, bool]],
    ) -> None:
        self._responder = responder
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._closed = False

    async def send(self, message: str | bytes) -> None:
        response, terminal = self._responder(message)
        if response is not None:
            await self._queue.put(response)
        if terminal:
            await self._queue.put(self._STOP)
        await asyncio.sleep(0)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(self._STOP)

    def __aiter__(self) -> _QueueWebSocket:
        return self

    async def __anext__(self) -> str:
        message = await self._queue.get()
        if message is self._STOP:
            raise StopAsyncIteration
        assert isinstance(message, str)
        return message


def _ws_connect(
    responder: Callable[[str | bytes], tuple[str | None, bool]],
) -> Callable[..., Any]:
    async def connect(_url: str, **_kwargs: Any) -> _QueueWebSocket:
        return _QueueWebSocket(responder)

    return connect


def _json_message(message: str | bytes) -> dict[str, Any]:
    return json.loads(message) if isinstance(message, str) else {}


def _openai_realtime_response(message: str | bytes) -> tuple[str | None, bool]:
    message_type = _json_message(message).get("type")
    if message_type == "session.update":
        return (json.dumps({"type": "transcription_session.updated", "session": {}}), False)
    if message_type == "input_audio_buffer.commit":
        return (
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "hello from OpenAI Realtime",
                }
            ),
            True,
        )
    return (None, False)


def _deepgram_response(message: str | bytes) -> tuple[str | None, bool]:
    message_type = _json_message(message).get("type")
    if message_type == "CloseStream":
        return (None, True)
    if message_type != "Finalize":
        return (None, False)
    return (
        json.dumps(
            {
                "type": "Results",
                "channel": {"alternatives": [{"transcript": "hello from Deepgram"}]},
                "is_final": True,
                "from_finalize": True,
            }
        ),
        True,
    )


def _elevenlabs_response(message: str | bytes) -> tuple[str | None, bool]:
    payload = _json_message(message)
    if payload.get("message_type") != "input_audio_chunk" or not payload.get("commit"):
        return (None, False)
    return (
        json.dumps(
            {
                "message_type": "committed_transcript",
                "text": "hello from ElevenLabs",
            }
        ),
        True,
    )


def _cartesia_response(message: str | bytes) -> tuple[str | None, bool]:
    # Cartesia's client commands are raw text messages, not JSON envelopes
    # (gh 1065); audio arrives as binary frames. ``finalize`` flushes the
    # buffered audio and leaves the session open; ``close`` ends it.
    if not isinstance(message, str):
        return (None, False)
    if message == "close":
        return (json.dumps({"type": "done"}), True)
    if message != "finalize":
        return (None, False)
    return (
        json.dumps(
            {
                "type": "transcript",
                "text": "hello from Cartesia",
                "is_final": True,
            }
        ),
        False,
    )


def _openai_stt() -> OpenAISTT:
    body = (
        'data: {"delta": "hello"}\n'
        'data: {"text": "hello from OpenAI", "is_final": true}\n'
        "data: [DONE]\n"
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return OpenAISTT(OpenAISTTConfig(api_key="contract-key", http_client=client))


def _openai_realtime_stt() -> OpenAIRealtimeSTT:
    return OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(
            api_key="contract-key",
            persistent_ws=False,
            ws_connect=_ws_connect(_openai_realtime_response),
        )
    )


def _deepgram_stt() -> DeepgramSTT:
    return DeepgramSTT(
        DeepgramSTTConfig(
            api_key="contract-key",
            persistent_ws=False,
            ws_connect=_ws_connect(_deepgram_response),
        )
    )


def _elevenlabs_stt() -> ElevenLabsSTT:
    return ElevenLabsSTT(
        ElevenLabsSTTConfig(
            api_key="contract-key",
            mode="realtime",
            realtime_commit_strategy="manual",
            ws_connect=_ws_connect(_elevenlabs_response),
        )
    )


def _cartesia_stt() -> CartesiaSTT:
    return CartesiaSTT(
        CartesiaSTTConfig(
            api_key="contract-key",
            ws_connect=_ws_connect(_cartesia_response),
        )
    )


_BUILT_IN_STT_FACTORIES_BY_NAME = {
    "openai": _openai_stt,
    "openai-realtime": _openai_realtime_stt,
    "deepgram": _deepgram_stt,
    "elevenlabs": _elevenlabs_stt,
    "cartesia": _cartesia_stt,
}
_BUILT_IN_STT_FACTORIES = tuple(
    pytest.param(factory, id=name) for name, factory in _BUILT_IN_STT_FACTORIES_BY_NAME.items()
)


def test_stt_provider_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "stt"]

    assert rows
    assert all(
        row.contract_path == "tests/contracts/test_stt_provider_contracts.py" for row in rows
    )


def test_real_stt_contract_factories_cover_catalog() -> None:
    assert set(_BUILT_IN_STT_FACTORIES_BY_NAME) == set(_STT_CATALOG.specs)


class TestSTTContractSuite(STTProviderContractSuite):
    """Run the shipped provider-author kit suite against the offline fake.

    The protocol-semantics assertions live in
    :class:`easycat.testing.STTProviderContractSuite` so this file and the
    installable kit cannot drift; only fake-specific bookkeeping checks are
    added below.
    """

    provider_factory = _ContractSTT

    async def test_fake_observes_lifecycle_calls_and_payloads(
        self, provider: _ContractSTT
    ) -> None:
        chunk = AudioChunk(data=b"\0" * 320, format=PCM16_MONO_16K)

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


class TestBuiltInSTTContractSuite(STTProviderContractSuite):
    """Run the provider-author contract kit against every real built-in STT."""

    @pytest.fixture(params=_BUILT_IN_STT_FACTORIES)
    async def provider(self, request: pytest.FixtureRequest) -> AsyncIterator[Any]:
        provider = request.param()
        try:
            yield provider
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
            client = getattr(getattr(provider, "_config", None), "http_client", None)
            if client is not None:
                await client.aclose()

    def sample_audio_chunks(self) -> tuple[AudioChunk, ...]:
        # OpenAI Realtime rejects commits shorter than 100 ms (4,800 bytes at
        # its 24 kHz PCM16 input rate), so exercise every provider above that
        # shared floor.
        return (AudioChunk(data=b"\0" * 4_800, format=PCM16_MONO_16K),)
