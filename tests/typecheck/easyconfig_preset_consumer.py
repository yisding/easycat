"""Static downstream contract for the typed EasyConfig preset keywords."""

from pathlib import Path

from easycat import EasyConfig


class ThirdPartyAgent:
    """Opaque application-owned agent specification."""


class ThirdPartySTTConfig:
    """Config type that an extension can register after EasyCat ships."""


class ThirdPartyTTSConfig:
    """Config type that an extension can register after EasyCat ships."""


class ThirdPartyVADConfig:
    """Config type that an extension can register after EasyCat ships."""


class ThirdPartyNoiseReducerConfig:
    """Config type that an extension can register after EasyCat ships."""


class ThirdPartyEchoCancellerConfig:
    """Config type that an extension can register after EasyCat ships."""


def capture_audio() -> bool:
    return True


def fallback_message(_exc: Exception) -> str:
    return "Please try again."


def build_presets() -> tuple[EasyConfig, EasyConfig, EasyConfig]:
    """Every preset exposes precise policies without closing provider slots."""
    mic = EasyConfig.mic(
        agent=ThirdPartyAgent(),
        stt=ThirdPartySTTConfig(),
        tts=ThirdPartyTTSConfig(),
        vad=ThirdPartyVADConfig(),
        noise_reduction=ThirdPartyNoiseReducerConfig(),
        echo_cancellation=ThirdPartyEchoCancellerConfig(),
        debug="full",
        handler_error_policy="raise",
        journal_backend="sqlite",
        journal_redaction="pii",
        journal_retention="delete",
        capture_audio=capture_audio,
        caller_id_exposure="tools_only",
        on_agent_failure=fallback_message,
        data_dir=Path(".easycat"),
        record_to=Path("bundles"),
    )
    browser = EasyConfig.browser(
        agent=ThirdPartyAgent(),
        stt=ThirdPartySTTConfig(),
        tts=ThirdPartyTTSConfig(),
        enable_echo_cancellation=True,
        smart_turn_sensitivity=0.75,
        warmup=False,
    )
    phone = EasyConfig.phone(
        agent=ThirdPartyAgent(),
        stt=ThirdPartySTTConfig(),
        tts=ThirdPartyTTSConfig(),
        mcp_servers=["stdio://tool"],
        greeting="Hello",
        journal_capacity=1_000,
    )
    return mic, browser, phone
