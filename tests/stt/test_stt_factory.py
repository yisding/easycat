"""Tests for the STT provider factory's registry wiring."""

import pytest

from easycat.events import EventBus
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


def test_factory_no_event_bus_by_default() -> None:
    provider = create_stt_provider(STTProviderConfig(provider="deepgram", api_key="test-key"))

    assert provider._config.event_bus is None


@pytest.mark.parametrize(
    ("name", "provider_type"),
    [provider for provider in _PROVIDERS if provider[0] != "openai"],
)
def test_factory_injects_event_bus(name: str, provider_type: type) -> None:
    event_bus = EventBus()

    provider = create_stt_provider(
        STTProviderConfig(provider=name, api_key="test-key"), event_bus=event_bus
    )

    assert type(provider) is provider_type
    assert provider._config.event_bus is event_bus


def test_factory_ignores_event_bus_for_config_without_field() -> None:
    provider = create_stt_provider(
        STTProviderConfig(provider="openai", api_key="test-key"), event_bus=EventBus()
    )

    assert isinstance(provider, OpenAISTT)
    assert not hasattr(provider._config, "event_bus")


def test_factory_preserves_event_bus_from_params() -> None:
    configured_event_bus = EventBus()

    provider = create_stt_provider(
        STTProviderConfig(
            provider="cartesia",
            api_key="test-key",
            params={"event_bus": configured_event_bus},
        ),
        event_bus=EventBus(),
    )

    assert provider._config.event_bus is configured_event_bus
