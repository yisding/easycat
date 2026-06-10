from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.testing import TTSProviderContractSuite
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
