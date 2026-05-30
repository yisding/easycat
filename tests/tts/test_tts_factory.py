"""Tests for TTS provider factory."""

from __future__ import annotations

import pytest

from easycat.errors import EasyCatError
from easycat.events import EventBus
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsStreamMode, ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.factory import (
    TTSProviderConfig,
    available_providers,
    create_tts_provider,
    create_tts_provider_from_config,
)
from easycat.tts.openai_tts import OpenAITTS


class TestTTSProviderConfig:
    def test_basic_config(self):
        config = TTSProviderConfig(provider="openai")
        assert config.provider == "openai"
        assert config.params is None
        assert config.api_key is None

    def test_config_with_top_level_api_key(self):
        config = TTSProviderConfig(provider="openai", api_key="test")
        assert config.api_key == "test"

    def test_config_with_params(self):
        config = TTSProviderConfig(
            provider="openai",
            params={"api_key": "test", "model": "tts-1-hd"},
        )
        assert config.params["api_key"] == "test"

    def test_settings_alias_folds_into_params(self):
        # ``settings`` is a deprecated alias for ``params``.
        config = TTSProviderConfig(
            provider="openai",
            settings={"api_key": "test", "model": "tts-1-hd"},
        )
        assert config.settings is None
        assert config.params == {"api_key": "test", "model": "tts-1-hd"}


class TestCreateTTSProvider:
    def test_create_openai(self):
        config = TTSProviderConfig(
            provider="openai",
            settings={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, OpenAITTS)

    def test_create_deepgram(self):
        config = TTSProviderConfig(
            provider="deepgram",
            settings={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, DeepgramTTS)

    def test_create_elevenlabs(self):
        config = TTSProviderConfig(
            provider="elevenlabs",
            settings={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, ElevenLabsTTS)

    def test_create_cartesia(self):
        config = TTSProviderConfig(
            provider="cartesia",
            settings={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, CartesiaTTS)

    def test_cartesia_with_custom_settings(self):
        config = TTSProviderConfig(
            provider="cartesia",
            settings={
                "api_key": "c-test",
                "model_id": "sonic-turbo",
                "voice_id": "voice-custom",
                "sample_rate": 16000,
            },
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, CartesiaTTS)
        assert provider._config.model_id == "sonic-turbo"
        assert provider._config.voice_id == "voice-custom"
        assert provider._config.sample_rate == 16000

    def test_case_insensitive_provider_name(self):
        config = TTSProviderConfig(
            provider="OpenAI",
            settings={"api_key": "test"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, OpenAITTS)

    def test_unknown_provider_raises(self):
        config = TTSProviderConfig(provider="unknown_provider")
        with pytest.raises(EasyCatError) as exc_info:
            create_tts_provider(config)
        assert exc_info.value.code == "EASYCAT_E104"

    def test_rejects_non_string_provider(self):
        config = TTSProviderConfig(provider=None, params={"api_key": "test"})  # type: ignore[arg-type]
        with pytest.raises(EasyCatError) as exc_info:
            create_tts_provider(config)
        assert exc_info.value.code == "EASYCAT_E104"

    def test_rejects_empty_provider(self):
        config = TTSProviderConfig(provider="", params={"api_key": "test"})
        with pytest.raises(EasyCatError) as exc_info:
            create_tts_provider(config)
        assert exc_info.value.code == "EASYCAT_E104"

    def test_error_message_lists_available(self):
        config = TTSProviderConfig(provider="bad")
        with pytest.raises(EasyCatError, match="deepgram.*elevenlabs.*openai") as exc_info:
            create_tts_provider(config)
        assert exc_info.value.code == "EASYCAT_E104"

    def test_unknown_provider_suggests_close_match(self):
        config = TTSProviderConfig(provider="deepgrm")
        with pytest.raises(EasyCatError, match="Did you mean 'deepgram'"):
            create_tts_provider(config)

    def test_invalid_params_raises(self):
        config = TTSProviderConfig(
            provider="openai",
            params={"nonexistent_param": "value"},
        )
        with pytest.raises(ValueError, match="Invalid params"):
            create_tts_provider(config)

    def test_create_with_top_level_api_key(self):
        config = TTSProviderConfig(provider="openai", api_key="test-key")
        provider = create_tts_provider(config)
        assert isinstance(provider, OpenAITTS)
        assert provider._config.api_key == "test-key"

    def test_create_with_top_level_api_key_and_params(self):
        config = TTSProviderConfig(
            provider="openai",
            api_key="test-key",
            params={"model": "tts-1-hd"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, OpenAITTS)
        assert provider._config.api_key == "test-key"
        assert provider._config.model == "tts-1-hd"

    def test_empty_settings_rejects_missing_api_key(self):
        config = TTSProviderConfig(provider="openai", settings={})
        with pytest.raises(ValueError, match="API key is required"):
            create_tts_provider(config)

    def test_none_settings_rejects_missing_api_key(self):
        config = TTSProviderConfig(provider="openai")
        with pytest.raises(ValueError, match="API key is required"):
            create_tts_provider(config)

    def test_rejects_empty_api_key(self):
        config = TTSProviderConfig(provider="openai", settings={"api_key": ""})
        with pytest.raises(ValueError, match="API key is required"):
            create_tts_provider(config)

    def test_openai_with_custom_settings(self):
        config = TTSProviderConfig(
            provider="openai",
            settings={
                "api_key": "sk-test",
                "model": "tts-1-hd",
                "voice": "nova",
                "speed": 1.5,
            },
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, OpenAITTS)
        assert provider._config.model == "tts-1-hd"
        assert provider._config.voice == "nova"
        assert provider._config.speed == 1.5

    def test_deepgram_with_custom_settings(self):
        config = TTSProviderConfig(
            provider="deepgram",
            settings={
                "api_key": "dg-test",
                "model": "aura-orpheus-en",
                "sample_rate": 16000,
            },
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, DeepgramTTS)
        assert provider._config.model == "aura-orpheus-en"
        assert provider._config.sample_rate == 16000

    def test_elevenlabs_with_custom_settings(self):
        config = TTSProviderConfig(
            provider="elevenlabs",
            settings={
                "api_key": "el-test",
                "voice_id": "custom-voice",
                "stability": 0.9,
            },
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, ElevenLabsTTS)
        assert provider._config.voice_id == "custom-voice"
        assert provider._config.stability == 0.9


def test_available_providers_lists_registered_names():
    assert available_providers() == ["cartesia", "deepgram", "elevenlabs", "openai"]


class TestCreateTTSProviderFromConfig:
    def test_injects_event_bus_for_deepgram_when_missing(self):
        config = DeepgramTTSConfig(api_key="test")
        event_bus = EventBus()

        provider = create_tts_provider_from_config(config, event_bus)

        assert isinstance(provider, DeepgramTTS)
        assert provider._config.event_bus is event_bus

    def test_injects_event_bus_for_elevenlabs_when_missing(self):
        config = ElevenLabsTTSConfig(
            api_key="test",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
        )
        event_bus = EventBus()

        provider = create_tts_provider_from_config(config, event_bus)

        assert isinstance(provider, ElevenLabsTTS)
        assert provider._config.event_bus is event_bus

    def test_keeps_existing_event_bus_for_elevenlabs(self):
        existing_event_bus = EventBus()
        config = ElevenLabsTTSConfig(
            api_key="test",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            event_bus=existing_event_bus,
        )
        session_event_bus = EventBus()

        provider = create_tts_provider_from_config(config, session_event_bus)

        assert isinstance(provider, ElevenLabsTTS)
        assert provider._config.event_bus is existing_event_bus

    def test_injects_event_bus_for_cartesia_when_missing(self):
        config = CartesiaTTSConfig(api_key="test")
        event_bus = EventBus()

        provider = create_tts_provider_from_config(config, event_bus)

        assert isinstance(provider, CartesiaTTS)
        assert provider._config.event_bus is event_bus

    def test_keeps_existing_event_bus_for_cartesia(self):
        existing_event_bus = EventBus()
        config = CartesiaTTSConfig(api_key="test", event_bus=existing_event_bus)
        session_event_bus = EventBus()

        provider = create_tts_provider_from_config(config, session_event_bus)

        assert isinstance(provider, CartesiaTTS)
        assert provider._config.event_bus is existing_event_bus
