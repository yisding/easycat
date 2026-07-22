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
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig


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
        # ``settings`` is a deprecated alias for ``params``; folding it emits
        # a ``DeprecationWarning`` (PEP 702 / QW8).
        with pytest.warns(DeprecationWarning):
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
            params={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, OpenAITTS)

    def test_create_deepgram(self):
        config = TTSProviderConfig(
            provider="deepgram",
            params={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, DeepgramTTS)

    def test_create_elevenlabs(self):
        config = TTSProviderConfig(
            provider="elevenlabs",
            params={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, ElevenLabsTTS)

    def test_create_cartesia(self):
        config = TTSProviderConfig(
            provider="cartesia",
            params={"api_key": "test-key"},
        )
        provider = create_tts_provider(config)
        assert isinstance(provider, CartesiaTTS)

    def test_cartesia_with_custom_settings(self):
        config = TTSProviderConfig(
            provider="cartesia",
            params={
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
            params={"api_key": "test"},
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
        config = TTSProviderConfig(provider="openai", params={})
        with pytest.raises(ValueError, match="API key is required"):
            create_tts_provider(config)

    def test_none_settings_rejects_missing_api_key(self):
        config = TTSProviderConfig(provider="openai")
        with pytest.raises(ValueError, match="API key is required"):
            create_tts_provider(config)

    def test_rejects_empty_api_key(self):
        config = TTSProviderConfig(provider="openai", params={"api_key": ""})
        with pytest.raises(ValueError, match="API key is required"):
            create_tts_provider(config)

    def test_openai_with_custom_settings(self):
        config = TTSProviderConfig(
            provider="openai",
            params={
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
            params={
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
            params={
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


class TestCreateTTSProviderEventBus:
    """create_tts_provider (string/params path) wires event_bus the same
    way create_tts_provider_from_config does, so providers built from the
    public API keep reconnect/error observability."""

    def test_no_event_bus_by_default(self):
        config = TTSProviderConfig(provider="deepgram", api_key="dg-test")
        provider = create_tts_provider(config)
        assert provider._config.event_bus is None

    @pytest.mark.parametrize(
        ("provider_name", "api_key", "params", "provider_cls"),
        [
            ("deepgram", "dg-test", None, DeepgramTTS),
            (
                "elevenlabs",
                "el-test",
                {"stream_mode": ElevenLabsStreamMode.WEBSOCKET},
                ElevenLabsTTS,
            ),
            ("openai", "sk-test", None, OpenAITTS),
            ("cartesia", "c-test", None, CartesiaTTS),
        ],
    )
    def test_injects_event_bus_when_given(self, provider_name, api_key, params, provider_cls):
        config = TTSProviderConfig(provider=provider_name, api_key=api_key, params=params)
        event_bus = EventBus()

        provider = create_tts_provider(config, event_bus=event_bus)

        assert isinstance(provider, provider_cls)
        assert provider._config.event_bus is event_bus

    def test_keeps_existing_event_bus_from_params(self):
        existing_event_bus = EventBus()
        config = TTSProviderConfig(
            provider="cartesia",
            api_key="c-test",
            params={"event_bus": existing_event_bus},
        )
        session_event_bus = EventBus()

        provider = create_tts_provider(config, event_bus=session_event_bus)

        assert isinstance(provider, CartesiaTTS)
        assert provider._config.event_bus is existing_event_bus


class TestCreateTTSProviderFromConfig:
    @pytest.mark.parametrize(
        ("provider_name", "config", "provider_cls"),
        [
            ("deepgram", DeepgramTTSConfig(api_key="test"), DeepgramTTS),
            (
                "elevenlabs",
                ElevenLabsTTSConfig(
                    api_key="test",
                    stream_mode=ElevenLabsStreamMode.WEBSOCKET,
                ),
                ElevenLabsTTS,
            ),
            # OpenAI declares an ``event_bus`` field so the structural detection
            # in ``create_tts_provider_from_config`` auto-wires it for provider
            # Errors.
            ("openai", OpenAITTSConfig(api_key="test"), OpenAITTS),
            ("cartesia", CartesiaTTSConfig(api_key="test"), CartesiaTTS),
        ],
    )
    def test_injects_event_bus_when_missing(self, provider_name, config, provider_cls):
        event_bus = EventBus()

        provider = create_tts_provider_from_config(config, event_bus)

        assert isinstance(provider, provider_cls)
        assert provider._config.event_bus is event_bus

    @pytest.mark.parametrize(
        ("provider_name", "make_config", "provider_cls"),
        [
            (
                "elevenlabs",
                lambda event_bus: ElevenLabsTTSConfig(
                    api_key="test",
                    stream_mode=ElevenLabsStreamMode.WEBSOCKET,
                    event_bus=event_bus,
                ),
                ElevenLabsTTS,
            ),
            (
                "cartesia",
                lambda event_bus: CartesiaTTSConfig(api_key="test", event_bus=event_bus),
                CartesiaTTS,
            ),
        ],
    )
    def test_keeps_existing_event_bus(self, provider_name, make_config, provider_cls):
        existing_event_bus = EventBus()
        config = make_config(existing_event_bus)
        session_event_bus = EventBus()

        provider = create_tts_provider_from_config(config, session_event_bus)

        assert isinstance(provider, provider_cls)
        assert provider._config.event_bus is existing_event_bus
