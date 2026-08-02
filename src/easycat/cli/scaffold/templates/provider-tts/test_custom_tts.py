"""Offline contracts for the scaffolded TTS provider."""

from __future__ import annotations

from easycat import TTSProviderConfig, available_tts_providers, create_tts_provider
from easycat.planning import build_provider_plan
from easycat.project.schema import VoiceProfile
from easycat.testing import TTSProviderContractSuite
from easycat.tts.factory import parse_tts_string

from custom_tts import ToneTTS, ToneTTSConfig, register


class TestToneTTSContract(TTSProviderContractSuite):
    provider_factory = ToneTTS


def test_registers_named_tts_shortcut() -> None:
    register()
    assert "tone" in available_tts_providers()
    assert isinstance(
        create_tts_provider(TTSProviderConfig(provider="tone")),
        ToneTTS,
    )


def test_version_info_reports_all_journal_fields() -> None:
    assert set(ToneTTS().version_info()) >= {
        "provider",
        "model",
        "api_version",
        "sdk_version",
    }


def test_shortcut_config_and_capabilities_are_visible_to_the_planner() -> None:
    register()
    config = parse_tts_string("tone/custom-model")
    plan = build_provider_plan(
        VoiceProfile(
            name="test",
            transport="websocket",
            stt="openai",
            tts="tone/custom-model",
        ),
        environ={"OPENAI_API_KEY": "test"},
    )

    assert config == ToneTTSConfig(model="custom-model")
    assert plan.selected["tts"].capabilities == frozenset({"offline"})


# LIVE TODO: add a separate test class marked ``integration_live``,
# ``provider_custom``, and ``surface_tts``; set ``live = True`` and
# ``credential_env_var``, and use a real SDK factory.
