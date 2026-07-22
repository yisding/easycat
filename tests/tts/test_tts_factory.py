"""Tests for the TTS provider factory's registry wiring."""

import pytest

from easycat.events import EventBus
from easycat.tts.cartesia_tts import CartesiaTTS
from easycat.tts.deepgram_tts import DeepgramTTS
from easycat.tts.elevenlabs_tts import ElevenLabsTTS
from easycat.tts.factory import (
    TTSProviderConfig,
    available_tts_providers,
    create_tts_provider,
)
from easycat.tts.openai_tts import OpenAITTS

_PROVIDERS = [
    ("openai", OpenAITTS),
    ("deepgram", DeepgramTTS),
    ("elevenlabs", ElevenLabsTTS),
    ("cartesia", CartesiaTTS),
]


@pytest.mark.parametrize(("name", "provider_type"), _PROVIDERS)
def test_factory_creates_registered_provider(name: str, provider_type: type) -> None:
    provider = create_tts_provider(TTSProviderConfig(provider=name, api_key="test-key"))

    assert type(provider) is provider_type


def test_available_tts_providers_matches_registry() -> None:
    assert available_tts_providers() == sorted(name for name, _ in _PROVIDERS)


def test_factory_no_event_bus_by_default() -> None:
    provider = create_tts_provider(TTSProviderConfig(provider="deepgram", api_key="test-key"))

    assert provider._config.event_bus is None


@pytest.mark.parametrize(("name", "provider_type"), _PROVIDERS)
def test_factory_injects_event_bus(name: str, provider_type: type) -> None:
    event_bus = EventBus()

    provider = create_tts_provider(
        TTSProviderConfig(provider=name, api_key="test-key"), event_bus=event_bus
    )

    assert type(provider) is provider_type
    assert provider._config.event_bus is event_bus


def test_factory_preserves_event_bus_from_params() -> None:
    configured_event_bus = EventBus()

    provider = create_tts_provider(
        TTSProviderConfig(
            provider="cartesia",
            api_key="test-key",
            params={"event_bus": configured_event_bus},
        ),
        event_bus=EventBus(),
    )

    assert provider._config.event_bus is configured_event_bus
