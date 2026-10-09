"""Typed STT/TTS configs honor a programmatic ``EasyConfig.openai_api_key``.

Regression: a keyless typed config such as ``OpenAISTTConfig()`` only fell back
to the ``OPENAI_API_KEY`` environment variable, so
``EasyConfig(openai_api_key=k, stt=OpenAISTTConfig())`` raised "openai STT
requires an API key" even though string specs and named wrappers picked up the
explicit key.  The programmatic key must override ``OPENAI_API_KEY`` for typed
configs too, and only for providers whose catalog env var is ``OPENAI_API_KEY``.
"""

from __future__ import annotations

from typing import Any

import pytest

from easycat import EasyConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig


@pytest.mark.parametrize(
    "stt_factory",
    [OpenAISTTConfig, OpenAIRealtimeSTTConfig],
    ids=["openai", "openai-realtime"],
)
def test_typed_openai_configs_use_programmatic_key_without_env(
    monkeypatch: pytest.MonkeyPatch, stt_factory: Any
) -> None:
    """With OPENAI_API_KEY unset, keyless typed OpenAI configs get the explicit key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = EasyConfig(
        openai_api_key="sk-prog",
        stt=stt_factory(),
        tts=OpenAITTSConfig(),
        debug="off",
    )

    assert cfg.stt.api_key == "sk-prog"
    assert cfg.tts.api_key == "sk-prog"


def test_typed_openai_configs_prefer_programmatic_key_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit key beats OPENAI_API_KEY, matching string and wrapper specs."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    cfg = EasyConfig(
        openai_api_key="sk-prog",
        stt=OpenAISTTConfig(),
        tts=OpenAITTSConfig(),
        debug="off",
    )
    string_cfg = EasyConfig(
        openai_api_key="sk-prog",
        stt="openai/whisper-1",
        tts=OpenAITTSConfig(),
        debug="off",
    )

    assert cfg.stt.api_key == "sk-prog"
    assert cfg.tts.api_key == "sk-prog"
    assert string_cfg.stt.api_key == cfg.stt.api_key


def test_typed_openai_config_explicit_api_key_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed config's own api_key always wins over openai_api_key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    cfg = EasyConfig(
        openai_api_key="sk-prog",
        stt=OpenAISTTConfig(api_key="sk-stt-own"),
        tts=OpenAITTSConfig(api_key="sk-tts-own"),
        debug="off",
    )

    assert cfg.stt.api_key == "sk-stt-own"
    assert cfg.tts.api_key == "sk-tts-own"


def test_programmatic_openai_key_is_not_given_to_deepgram_stt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai_api_key only overrides OPENAI_API_KEY, never another provider's key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    with pytest.raises(ValueError, match=r"deepgram STT requires an API key"):
        EasyConfig(
            openai_api_key="sk-prog",
            stt=DeepgramSTTConfig(api_key=""),
            tts=OpenAITTSConfig(),
            debug="off",
        )


def test_programmatic_openai_key_is_not_given_to_elevenlabs_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless ElevenLabs config still needs its own env var."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    with pytest.raises(ValueError, match=r"elevenlabs TTS requires an API key"):
        EasyConfig(
            openai_api_key="sk-prog",
            stt=OpenAISTTConfig(),
            tts=ElevenLabsTTSConfig(),
            debug="off",
        )


def test_non_openai_typed_config_still_uses_its_own_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless Deepgram config resolves DEEPGRAM_API_KEY, not the OpenAI key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-env")

    cfg = EasyConfig(
        openai_api_key="sk-prog",
        stt=DeepgramSTTConfig(api_key=""),
        tts=OpenAITTSConfig(),
        debug="off",
    )

    assert cfg.stt.api_key == "dg-env"
    assert cfg.tts.api_key == "sk-prog"


def test_typed_openai_config_without_any_key_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no openai_api_key and no env var, a typed OpenAI config still fails."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match=r"openai STT requires an API key"):
        EasyConfig(stt=OpenAISTTConfig(), tts=OpenAITTSConfig(api_key="tts-key"), debug="off")
