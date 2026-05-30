"""Tests for the STT provider factory."""

from __future__ import annotations

import pytest

from easycat.errors import EasyCatError
from easycat.stt.cartesia_provider import CartesiaSTT
from easycat.stt.deepgram_provider import DeepgramSTT
from easycat.stt.elevenlabs_provider import ElevenLabsSTT
from easycat.stt.factory import STTProviderConfig, available_providers, create_stt_provider
from easycat.stt.openai_provider import OpenAISTT

# ── Factory creates correct provider types ───────────────────────


def test_factory_creates_openai():
    config = STTProviderConfig(provider="openai", api_key="test-key")
    provider = create_stt_provider(config)
    assert isinstance(provider, OpenAISTT)


def test_factory_creates_deepgram():
    config = STTProviderConfig(provider="deepgram", api_key="test-key")
    provider = create_stt_provider(config)
    assert isinstance(provider, DeepgramSTT)


def test_factory_creates_elevenlabs():
    config = STTProviderConfig(provider="elevenlabs", api_key="test-key")
    provider = create_stt_provider(config)
    assert isinstance(provider, ElevenLabsSTT)


def test_factory_creates_cartesia():
    config = STTProviderConfig(provider="cartesia", api_key="test-key")
    provider = create_stt_provider(config)
    assert isinstance(provider, CartesiaSTT)


def test_factory_passes_cartesia_params():
    config = STTProviderConfig(
        provider="cartesia",
        api_key="c-key",
        params={"model": "ink-whisper", "language": "fr", "sample_rate": 8000},
    )
    provider = create_stt_provider(config)
    assert isinstance(provider, CartesiaSTT)
    assert provider._config.model == "ink-whisper"
    assert provider._config.language == "fr"
    assert provider._config.sample_rate == 8000


# ── Provider-specific params ─────────────────────────────────────


def test_factory_passes_openai_params():
    config = STTProviderConfig(
        provider="openai",
        api_key="sk-key",
        params={"model": "whisper-1", "language": "en"},
    )
    provider = create_stt_provider(config)
    assert isinstance(provider, OpenAISTT)
    assert provider._config.model == "whisper-1"
    assert provider._config.language == "en"


def test_factory_passes_deepgram_params():
    config = STTProviderConfig(
        provider="deepgram",
        api_key="dg-key",
        params={"model": "nova-2-general", "punctuate": False},
    )
    provider = create_stt_provider(config)
    assert isinstance(provider, DeepgramSTT)
    assert provider._config.model == "nova-2-general"
    assert provider._config.punctuate is False


def test_factory_passes_elevenlabs_params():
    config = STTProviderConfig(
        provider="elevenlabs",
        api_key="el-key",
        params={"mode": "batch", "language": "fr"},
    )
    provider = create_stt_provider(config)
    assert isinstance(provider, ElevenLabsSTT)
    assert provider._config.mode == "batch"
    assert provider._config.language == "fr"


# ── Validation ───────────────────────────────────────────────────


def test_factory_rejects_non_string_provider():
    config = STTProviderConfig(provider=None, api_key="k")  # type: ignore[arg-type]
    with pytest.raises(EasyCatError) as exc_info:
        create_stt_provider(config)
    assert exc_info.value.code == "EASYCAT_E104"


def test_factory_rejects_unknown_provider():
    config = STTProviderConfig(provider="unknown", api_key="k")
    with pytest.raises(EasyCatError) as exc_info:
        create_stt_provider(config)
    assert exc_info.value.code == "EASYCAT_E104"


def test_factory_rejects_empty_api_key():
    config = STTProviderConfig(provider="openai", api_key="")
    with pytest.raises(ValueError, match="API key is required"):
        create_stt_provider(config)


def test_factory_error_message_lists_providers():
    config = STTProviderConfig(provider="bad", api_key="k")
    with pytest.raises(EasyCatError, match="deepgram") as exc_info:
        create_stt_provider(config)
    assert exc_info.value.code == "EASYCAT_E104"


def test_unknown_provider_suggests_close_match():
    # 'deepgrm' is close enough to 'deepgram' for the fuzzy hint.
    config = STTProviderConfig(provider="deepgrm", api_key="k")
    with pytest.raises(EasyCatError, match="Did you mean 'deepgram'"):
        create_stt_provider(config)


def test_factory_rejects_invalid_params():
    # Mirrors the TTS factory: an unknown param surfaces as a ValueError
    # rather than a raw TypeError from the config constructor.
    config = STTProviderConfig(provider="openai", api_key="k", params={"not_a_real_field": True})
    with pytest.raises(ValueError, match="Invalid params for 'openai' STT provider"):
        create_stt_provider(config)


# ── Symmetry with TTSProviderConfig ──────────────────────────────


def test_factory_accepts_nested_params_api_key():
    # An ``api_key`` nested in ``params`` is honored, mirroring the TTS
    # factory, so the two sibling configs accept the same shapes.
    config = STTProviderConfig(provider="openai", params={"api_key": "nested-key"})
    provider = create_stt_provider(config)
    assert isinstance(provider, OpenAISTT)
    assert provider._config.api_key == "nested-key"


def test_factory_top_level_api_key_takes_precedence_over_params():
    config = STTProviderConfig(
        provider="openai", api_key="top-key", params={"api_key": "nested-key"}
    )
    provider = create_stt_provider(config)
    assert provider._config.api_key == "top-key"


def test_factory_accepts_deprecated_settings_alias():
    # ``settings`` is a deprecated alias for ``params``; it must keep
    # working so it stays symmetric with TTSProviderConfig.
    config = STTProviderConfig(
        provider="openai", settings={"api_key": "settings-key", "model": "whisper-1"}
    )
    provider = create_stt_provider(config)
    assert isinstance(provider, OpenAISTT)
    assert provider._config.api_key == "settings-key"
    assert provider._config.model == "whisper-1"


def test_available_providers_lists_registered_names():
    names = available_providers()
    # Core providers must be registered; new providers may be added freely.
    assert set(names) >= {"openai", "deepgram", "elevenlabs", "cartesia"}
    # Names should be unique and returned in sorted order for stable listing.
    assert len(names) == len(set(names))
    assert names == sorted(names)


# ── Protocol conformance via factory ─────────────────────────────


def test_factory_produced_providers_are_stt_providers():
    from easycat.providers import STTProvider

    for name in ("openai", "openai-realtime", "deepgram", "elevenlabs", "cartesia"):
        config = STTProviderConfig(provider=name, api_key="test-key")
        provider = create_stt_provider(config)
        assert isinstance(provider, STTProvider), f"{name} not an STTProvider"
