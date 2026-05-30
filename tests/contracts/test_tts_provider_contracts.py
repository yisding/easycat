from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.providers import TTSProvider
from easycat.tts.input import TTSInput, coerce_tts_input
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [pytest.mark.contract, pytest.mark.surface_tts, pytest.mark.provider("matrix")]


class _ContractTTS:
    supports_ssml = False

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
        return {"provider": "contract-tts"}


def test_tts_provider_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "tts"]

    assert rows
    assert all(
        row.contract_path == "tests/contracts/test_tts_provider_contracts.py" for row in rows
    )


async def test_tts_contract_lifecycle_and_normalized_events() -> None:
    provider = _ContractTTS()

    assert isinstance(provider, TTSProvider)
    events = [event async for event in provider.synthesize("hello")]

    assert provider.payloads[0].text == "hello"
    assert [event.type for event in events] == [TTSEventType.AUDIO, TTSEventType.MARKERS]
    assert events[0].audio is not None
    assert events[0].audio.format == PCM16_MONO_24K
    assert events[1].markers == [{"word": "hello"}]


async def test_tts_contract_stop_and_cancel_are_idempotent() -> None:
    provider = _ContractTTS()

    await provider.stop()
    await provider.stop()
    await provider.cancel()
    await provider.cancel()

    assert provider.stop_calls == 2
    assert provider.cancel_calls == 2
