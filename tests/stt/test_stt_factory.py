"""Tests for the STT provider factory's registry wiring."""

import pytest

from easycat.providers import STTProvider
from easycat.stt.cartesia_provider import CartesiaSTT
from easycat.stt.deepgram_provider import DeepgramSTT
from easycat.stt.elevenlabs_provider import ElevenLabsSTT
from easycat.stt.factory import (
    STTProviderConfig,
    available_stt_providers,
    create_stt_provider,
)
from easycat.stt.openai_provider import OpenAISTT
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT

_PROVIDERS = [
    ("openai", OpenAISTT),
    ("openai-realtime", OpenAIRealtimeSTT),
    ("deepgram", DeepgramSTT),
    ("elevenlabs", ElevenLabsSTT),
    ("cartesia", CartesiaSTT),
]


@pytest.mark.parametrize(("name", "provider_type"), _PROVIDERS)
def test_factory_creates_registered_provider(name: str, provider_type: type) -> None:
    provider = create_stt_provider(STTProviderConfig(provider=name, api_key="test-key"))

    assert type(provider) is provider_type
    assert isinstance(provider, STTProvider)


def test_available_stt_providers_matches_registry() -> None:
    assert available_stt_providers() == sorted(name for name, _ in _PROVIDERS)
