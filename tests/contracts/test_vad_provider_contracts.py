from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import Event, VADStartSpeaking, VADStopSpeaking
from easycat.providers import VADProvider
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [pytest.mark.contract, pytest.mark.surface_vad, pytest.mark.provider("offline-fake")]


class _ContractVAD:
    def __init__(self) -> None:
        self.configured: dict[str, object] = {}
        self.processed = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self.processed += 1
        assert chunk.format == PCM16_MONO_16K
        yield VADStartSpeaking()
        yield VADStopSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        self.configured = {
            "min_speech_duration_ms": min_speech_duration_ms,
            "min_silence_duration_ms": min_silence_duration_ms,
            "sensitivity": sensitivity,
            "pre_roll_ms": pre_roll_ms,
            "post_roll_ms": post_roll_ms,
        }

    def version_info(self) -> dict[str, str]:
        return {"provider": "contract-vad"}


def test_vad_provider_contract_matrix_has_rows() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "vad"]

    assert {row.provider for row in rows} == {"silero", "funasr", "ten", "krisp"}
    assert all(
        row.contract_path == "tests/contracts/test_vad_provider_contracts.py" for row in rows
    )


async def test_vad_contract_configure_and_normalized_events() -> None:
    provider = _ContractVAD()
    chunk = AudioChunk(data=b"\0" * 320, format=PCM16_MONO_16K)

    assert isinstance(provider, VADProvider)
    provider.configure(sensitivity=0.7)
    events = [event async for event in provider.process(chunk)]

    assert provider.configured["sensitivity"] == 0.7
    assert provider.processed == 1
    assert [type(event) for event in events] == [VADStartSpeaking, VADStopSpeaking]
