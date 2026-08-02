"""Offline contracts for the scaffolded STT provider."""

from __future__ import annotations

from easycat import STTProviderConfig, available_stt_providers, create_stt_provider
from easycat.planning import build_provider_plan
from easycat.project.schema import VoiceProfile
from easycat.stt.factory import parse_stt_string
from easycat.testing import STTProviderContractSuite

from custom_stt import ScriptedSTT, ScriptedSTTConfig, register


class TestScriptedSTTContract(STTProviderContractSuite):
    provider_factory = ScriptedSTT


def test_registers_named_stt_shortcut() -> None:
    register()
    assert "scripted" in available_stt_providers()
    assert isinstance(
        create_stt_provider(STTProviderConfig(provider="scripted")),
        ScriptedSTT,
    )


def test_version_info_reports_all_journal_fields() -> None:
    assert set(ScriptedSTT().version_info()) >= {
        "provider",
        "model",
        "api_version",
        "sdk_version",
    }


def test_shortcut_config_and_capabilities_are_visible_to_the_planner() -> None:
    register()
    config = parse_stt_string("scripted/custom-model")
    plan = build_provider_plan(
        VoiceProfile(
            name="test",
            transport="websocket",
            stt="scripted/custom-model",
            tts="openai",
        ),
        environ={"OPENAI_API_KEY": "test"},
    )

    assert config == ScriptedSTTConfig(model="custom-model")
    assert plan.selected["stt"].capabilities == frozenset({"offline"})


# LIVE TODO: add a separate test class marked ``integration_live``,
# ``provider_custom``, and ``surface_stt``; set ``live = True`` and
# ``credential_env_var``, and use a real SDK factory.
