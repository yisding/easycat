from __future__ import annotations

import logging

import pytest

from easycat import (
    EasyConfig,
)
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.openai_tts import OpenAITTSConfig


@pytest.fixture
def _restore_easycat_logger():
    """Snapshot/restore easycat logger state so debug-mode tests stay isolated."""
    logger = logging.getLogger("easycat")
    handlers = logger.handlers[:]
    level = logger.level
    propagate = logger.propagate
    try:
        yield logger
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_easycat_config_requires_stt_tts(monkeypatch: pytest.MonkeyPatch):
    # No key resolved and no stt/tts configured now routes through the
    # error catalog as EASYCAT_E203 (an EasyCatError, not a ValueError).
    from easycat.errors import EasyCatError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig()
    assert excinfo.value.code == "EASYCAT_E203"


def test_debug_mode_defaults_easycat_logger_to_info(_restore_easycat_logger):
    EasyConfig(openai_api_key="test-key", debug="full")
    # H4: EASYCAT_LOG_LEVEL has a single meaning — INFO by default, DEBUG only
    # when the env var explicitly requests it (mirrors run()).
    assert _restore_easycat_logger.level == logging.INFO


def test_debug_mode_honors_env_debug_level(
    monkeypatch: pytest.MonkeyPatch, _restore_easycat_logger
):
    monkeypatch.setenv("EASYCAT_LOG_LEVEL", "debug")
    EasyConfig(openai_api_key="test-key", debug="full")
    assert _restore_easycat_logger.level == logging.DEBUG


def test_debug_bool_true_rejected():
    with pytest.raises(ValueError, match="Invalid debug=True"):
        EasyConfig(openai_api_key="test-key", debug=True)


def test_debug_bool_false_rejected():
    with pytest.raises(ValueError, match="Invalid debug=False"):
        EasyConfig(openai_api_key="test-key", debug=False)


def test_missing_api_key_error_uses_catalog_name_for_deepgram():
    with pytest.raises(ValueError, match=r"deepgram STT requires an API key"):
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
        )


def test_missing_api_key_error_uses_catalog_name_for_openai_tts():
    with pytest.raises(ValueError, match=r"openai TTS requires an API key"):
        EasyConfig(
            stt=OpenAIRealtimeSTTConfig(api_key="stt-key"),
            tts=OpenAITTSConfig(api_key=""),
        )


def test_missing_openai_key_with_no_stt_tts_raises_e203(monkeypatch: pytest.MonkeyPatch):
    from easycat.errors import EasyCatError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig()
    assert excinfo.value.code == "EASYCAT_E203"
    assert excinfo.value.context == {"var": "OPENAI_API_KEY"}


def test_whitespace_openai_env_key_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch):
    from easycat.errors import EasyCatError

    monkeypatch.setenv("OPENAI_API_KEY", " \t ")
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig()
    assert excinfo.value.code == "EASYCAT_E203"


@pytest.mark.parametrize(
    ("stt_key", "tts_key", "provider_name"),
    [
        (" \t ", "tts-key", "openai-realtime STT"),
        ("stt-key", " \t ", "openai TTS"),
    ],
)
def test_whitespace_concrete_provider_key_is_rejected(
    stt_key: str,
    tts_key: str,
    provider_name: str,
) -> None:
    with pytest.raises(ValueError, match=rf"{provider_name} requires an API key"):
        EasyConfig(
            stt=OpenAIRealtimeSTTConfig(api_key=stt_key),
            tts=OpenAITTSConfig(api_key=tts_key),
        )


def test_easyconfig_is_keyword_only(monkeypatch: pytest.MonkeyPatch):
    """Positional construction must fail loudly, never silently mis-bind.

    Regression guard for the ``_AgentSessionConfig`` base extraction: a base
    dataclass injects its fields before the subclass's in the generated
    ``__init__``, so without ``kw_only=True`` a positional
    ``EasyConfig("sk-...")`` would bind the key to ``agent`` instead of
    ``openai_api_key``. ``kw_only`` turns that into a ``TypeError``.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(TypeError):
        EasyConfig("sk-test")  # type: ignore[misc]  # positional is rejected
    # The keyword form still works and resolves the key correctly.
    cfg = EasyConfig(openai_api_key="sk-test")
    assert cfg.openai_api_key == "sk-test"
    assert cfg.agent is None
